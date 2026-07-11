from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pythinker_core.message import Message

from pythinker_code.soul.request_assembly import (
    FragmentBudgetClass,
    FragmentPersistence,
    FragmentRequirement,
    FragmentStatus,
    FragmentTruncation,
    RequestFragment,
    RequestSourceResult,
    SourceApplicability,
    SourceResultStatus,
    TrustedSourcePolicy,
    admit_source_results,
)
from pythinker_code.soul.request_primitives import estimate_injection_tokens
from pythinker_code.soul.request_primitives import normalize_history as normalize_history

if TYPE_CHECKING:
    from pythinker_code.soul.agent import Runtime
    from pythinker_code.soul.pythinkersoul import PythinkerSoul


@dataclass(frozen=True, slots=True)
class DynamicInjection:
    """A dynamic prompt content to be injected before an LLM step."""

    type: str  # identifier, e.g. "plan_mode"
    content: str  # text content (will be wrapped in <system-reminder> tags)


@dataclass(frozen=True, slots=True)
class InjectionCandidate:
    """Budgetable dynamic prompt content candidate."""

    type: str
    content: str
    priority: int = 100
    token_estimate: int | None = None
    rearm_key: str | None = None


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Token budget for dynamic injections."""

    max_context_tokens: int
    reserved_context_tokens: int
    injection_ceiling_tokens: int = 2048

    @property
    def injection_budget_tokens(self) -> int:
        available = max(0, self.max_context_tokens - self.reserved_context_tokens)
        return max(0, min(self.injection_ceiling_tokens, available))


def injection_budget_from_runtime(runtime: Runtime) -> ContextBudget:
    """Build a dynamic-injection budget from runtime model/config values."""
    llm = getattr(runtime, "llm", None)
    model_config = getattr(llm, "model_config", None)
    loop_control = getattr(getattr(runtime, "config", None), "loop_control", None)
    reserved = int(getattr(loop_control, "reserved_context_size", 1000) or 1000)
    memory_config = getattr(getattr(runtime, "config", None), "memory", None)
    ceiling = int(getattr(memory_config, "injection_ceiling_tokens", 2048) or 2048)
    fallback_context = reserved + ceiling
    model_max_context = getattr(model_config, "max_context_size", fallback_context)
    max_context = int(model_max_context or fallback_context)
    return ContextBudget(
        max_context_tokens=max_context,
        reserved_context_tokens=reserved,
        injection_ceiling_tokens=ceiling,
    )


def collect_within_budget(
    candidates: Sequence[InjectionCandidate], budget_tokens: int
) -> list[InjectionCandidate]:
    """Return deterministic priority-ordered candidates without exceeding ``budget_tokens``.

    Oversize candidates are truncated at a line boundary when possible; otherwise they are
    dropped if no useful prefix fits. The input order is the tie-breaker for equal priorities.
    """
    policies = tuple(
        _legacy_source_policy(index, candidate) for index, candidate in enumerate(candidates)
    )
    source_results = tuple(
        _legacy_source_result(policy, candidate)
        for policy, candidate in zip(policies, candidates, strict=True)
    )
    admissions = admit_source_results(policies, source_results, max(0, budget_tokens))
    candidates_by_key = {
        f"legacy_{index:012d}": candidate for index, candidate in enumerate(candidates)
    }
    return [
        replace(
            candidates_by_key[admission.fragment.key],
            content=admission.fragment.content,
            token_estimate=admission.outcome.admitted_tokens,
        )
        for admission in admissions
        if admission.fragment is not None
        and admission.outcome.status in {FragmentStatus.INCLUDED, FragmentStatus.TRUNCATED}
    ]


def _legacy_source_policy(index: int, candidate: InjectionCandidate) -> TrustedSourcePolicy:
    return TrustedSourcePolicy(
        source="legacy_dynamic_injection",
        key=f"legacy_{index:012d}",
        requirement=FragmentRequirement.BEST_EFFORT,
        persistence=FragmentPersistence.HISTORY,
        priority=candidate.priority,
        budget_class=FragmentBudgetClass.BUDGETED,
        truncation=FragmentTruncation.ALLOWED,
        applicability=SourceApplicability.ALWAYS,
        failure_reason_codes=(),
    )


def _legacy_source_result(
    policy: TrustedSourcePolicy, candidate: InjectionCandidate
) -> RequestSourceResult:
    return RequestSourceResult(
        source=policy.source,
        key=policy.key,
        status=SourceResultStatus.PROVIDED,
        fragment=RequestFragment(
            key=policy.key,
            content=candidate.content,
            source=policy.source,
            requirement=policy.requirement,
            persistence=policy.persistence,
            priority=policy.priority,
            truncatable=True,
        ),
        reason_code=None,
    )


def dynamic_to_candidate(injection: DynamicInjection, *, priority: int = 100) -> InjectionCandidate:
    return InjectionCandidate(
        type=injection.type,
        content=injection.content,
        priority=priority,
        token_estimate=estimate_injection_tokens(injection.content),
        rearm_key=injection.type,
    )


class DynamicInjectionProvider(ABC):
    """Base class for dynamic injection providers.

    Called before each LLM step. Implementations handle their own throttling.
    Providers can access all runtime state via the ``soul`` parameter
    (context_usage, runtime, config, etc.).
    """

    _prepared_injections: tuple[DynamicInjection, ...] = ()

    @abstractmethod
    async def get_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]: ...

    async def prepare_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        """Return retry-stable injections without acknowledging one-shot state."""
        pending = self._prepared_injections
        if pending:
            return list(pending)
        injections = await self.get_injections(history, soul)
        if injections:
            self._prepared_injections = tuple(injections)
        return injections

    def acknowledge_injections(self, keys: Sequence[str]) -> None:
        """Acknowledge prepared injections after their history append commits."""
        pending = self._prepared_injections
        acknowledged = tuple(injection for injection in pending if injection.type in keys)
        if acknowledged:
            self._on_injections_acknowledged(acknowledged)
        self._prepared_injections = tuple(
            injection for injection in pending if injection.type not in keys
        )

    def _on_injections_acknowledged(self, injections: Sequence[DynamicInjection]) -> None:
        _ = injections

    async def on_context_compacted(self) -> None:
        """Called after the context is compacted (history is rebuilt).

        Override to reset internal throttling state when prior injections
        may have been collapsed into the compaction summary and are no
        longer literally present in history. Default is a no-op.
        """
        return None

    async def on_auto_changed(self, enabled: bool) -> None:
        """Called when auto mode is toggled at runtime.

        Override to reset internal throttling state when a mode-specific
        reminder should be eligible to fire again after a user toggle.
        """
        _ = enabled
        return None

    def rearm(self, key: str) -> bool:
        """Re-arm provider throttling for ``key``.

        Return ``True`` when the provider recognized ``key`` and reset its
        throttle, ``False`` otherwise. The base implementation always returns
        ``False`` so a parent ``rearm_injection`` call can fan out across many
        providers without each one having to override; callers MUST treat
        ``False`` as "this provider does not own ``key``" rather than "rearm
        failed silently".
        """
        _ = key
        return False
