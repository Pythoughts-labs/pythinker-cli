from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pythinker_core.message import Message, TextPart

from pythinker_code.soul.message import system_reminder
from pythinker_code.soul.request_primitives import (
    estimate_injection_tokens,
    normalize_history,
)


class FragmentRequirement(StrEnum):
    REQUIRED = "required"
    BEST_EFFORT = "best_effort"


class FragmentPersistence(StrEnum):
    REQUEST_ONLY = "request_only"
    HISTORY = "history"


class FragmentStatus(StrEnum):
    INCLUDED = "included"
    NOT_APPLICABLE = "not_applicable"
    TRUNCATED = "truncated"
    OMITTED_BUDGET = "omitted_budget"
    DEGRADED = "degraded"
    FAILED = "failed"


class RequestStatus(StrEnum):
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"


class SourceResultStatus(StrEnum):
    PROVIDED = "provided"
    NOT_APPLICABLE = "not_applicable"
    FAILED = "failed"


class FragmentBudgetClass(StrEnum):
    BUDGETED = "budgeted"
    NON_BUDGETED = "non_budgeted"


class FragmentTruncation(StrEnum):
    FORBIDDEN = "forbidden"
    ALLOWED = "allowed"


class SourceApplicability(StrEnum):
    ALWAYS = "always"
    MAY_BE_NOT_APPLICABLE = "may_be_not_applicable"


@dataclass(frozen=True, slots=True)
class RequestFragment:
    key: str
    content: str
    source: str
    requirement: FragmentRequirement
    persistence: FragmentPersistence
    priority: int
    truncatable: bool


@dataclass(frozen=True, slots=True)
class FragmentOutcome:
    key: str
    source: str
    requirement: FragmentRequirement
    persistence: FragmentPersistence
    status: FragmentStatus
    estimated_tokens: int
    admitted_tokens: int
    reason_code: str | None


@dataclass(frozen=True, slots=True)
class RequestManifest:
    status: RequestStatus
    reason_code: str | None
    outcomes: tuple[FragmentOutcome, ...]
    budget_tokens: int
    budgeted_admitted_tokens: int
    non_budgeted_estimated_tokens: int


@dataclass(frozen=True, slots=True)
class RequestAssemblyInput:
    system_prompt: str
    persisted_history: tuple[Message, ...]
    current_task: str
    budget_tokens: int


@dataclass(frozen=True, slots=True)
class AssembledRequest:
    system_prompt: str
    provider_history: tuple[Message, ...]
    history_appends: tuple[Message, ...]
    manifest: RequestManifest


@dataclass(frozen=True, slots=True)
class TrustedSourcePolicy:
    source: str
    key: str
    requirement: FragmentRequirement
    persistence: FragmentPersistence
    priority: int
    budget_class: FragmentBudgetClass
    truncation: FragmentTruncation
    applicability: SourceApplicability
    failure_reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RequestSourceResult:
    source: str
    key: str
    status: SourceResultStatus
    fragment: RequestFragment | None
    reason_code: str | None


class RequestAssemblyError(RuntimeError):
    manifest: RequestManifest
    reason_code: str

    def __init__(self, reason_code: str, manifest: RequestManifest) -> None:
        if manifest.status is not RequestStatus.FAILED or manifest.reason_code != reason_code:
            raise ValueError("request assembly errors require a matching failed manifest")
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.manifest = manifest


class RequestBudgetError(RequestAssemblyError):
    pass


class RequestSourceError(RequestAssemblyError):
    pass


class RequestHistoryError(RequestAssemblyError):
    pass


class RequestInvariantError(RequestAssemblyError):
    pass


AGENTS_MD_SOURCE_POLICY = TrustedSourcePolicy(
    source="agents_md",
    key="agents_preamble",
    requirement=FragmentRequirement.REQUIRED,
    persistence=FragmentPersistence.REQUEST_ONLY,
    priority=1_000,
    budget_class=FragmentBudgetClass.NON_BUDGETED,
    truncation=FragmentTruncation.FORBIDDEN,
    applicability=SourceApplicability.MAY_BE_NOT_APPLICABLE,
    failure_reason_codes=("agents_md_unavailable", "agents_md_invalid"),
)


@dataclass(frozen=True, slots=True)
class _Admission:
    policy: TrustedSourcePolicy
    fragment: RequestFragment | None
    outcome: FragmentOutcome


@dataclass(frozen=True, slots=True)
class _RequestProjection:
    leading_messages: tuple[Message, ...]
    trailing_messages: tuple[Message, ...]
    history_appends: tuple[Message, ...]
    outcomes: tuple[FragmentOutcome, ...]


class _AssemblyFailure(Exception):
    def __init__(self, reason_code: str, outcomes: tuple[FragmentOutcome, ...]) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.outcomes = outcomes


@dataclass(frozen=True, slots=True)
class _TrustedSourceRegistry:
    policies: tuple[TrustedSourcePolicy, ...]
    by_identity: dict[tuple[str, str], TrustedSourcePolicy]
    source_rank: dict[str, int]
    key_rank: dict[tuple[str, str], int]

    @classmethod
    def build(cls, policies: Sequence[TrustedSourcePolicy]) -> _TrustedSourceRegistry:
        _validate_policies(policies)
        by_identity = {(policy.source, policy.key): policy for policy in policies}
        source_rank: dict[str, int] = {}
        for policy in policies:
            source_rank.setdefault(policy.source, len(source_rank))
        ordered_identities = sorted(by_identity)
        key_rank = {
            identity: index
            for source in source_rank
            for index, identity in enumerate(
                candidate for candidate in ordered_identities if candidate[0] == source
            )
        }
        return cls(tuple(policies), by_identity, source_rank, key_rank)

    def ordered(self) -> tuple[TrustedSourcePolicy, ...]:
        return tuple(
            sorted(
                self.policies,
                key=lambda policy: (
                    policy.requirement is not FragmentRequirement.REQUIRED,
                    -policy.priority,
                    self.source_rank[policy.source],
                    self.key_rank[(policy.source, policy.key)],
                    policy.key,
                ),
            )
        )


HistoryNormalizer = Callable[[Sequence[Message]], list[Message]]
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
_MAX_IDENTIFIER_LENGTH = 64
_DEGRADED_STATUSES = frozenset(
    {FragmentStatus.TRUNCATED, FragmentStatus.OMITTED_BUDGET, FragmentStatus.DEGRADED}
)


def _validate_policies(policies: Sequence[TrustedSourcePolicy]) -> None:
    identities = [(policy.source, policy.key) for policy in policies]
    if len(identities) != len(set(identities)):
        raise _AssemblyFailure("internal_invariant_violation", ())
    for policy in policies:
        _validate_policy_identifiers(policy)
        if policy.source == AGENTS_MD_SOURCE_POLICY.source and policy != AGENTS_MD_SOURCE_POLICY:
            raise _AssemblyFailure("invalid_agents_policy", ())
        if (
            policy.budget_class is FragmentBudgetClass.NON_BUDGETED
            and policy != AGENTS_MD_SOURCE_POLICY
        ):
            raise _AssemblyFailure("invalid_non_budgeted_policy", ())


def _validate_policy_identifiers(policy: TrustedSourcePolicy) -> None:
    identifiers = (policy.source, policy.key, *policy.failure_reason_codes)
    if any(not _is_safe_identifier(identifier) for identifier in identifiers):
        raise _AssemblyFailure("unsafe_source_policy", ())


def _is_safe_identifier(identifier: str) -> bool:
    return (
        0 < len(identifier) <= _MAX_IDENTIFIER_LENGTH
        and _SAFE_IDENTIFIER.fullmatch(identifier) is not None
    )


def admit_source_results(
    policies: Sequence[TrustedSourcePolicy],
    source_results: Sequence[RequestSourceResult],
    budget_tokens: int,
) -> tuple[_Admission, ...]:
    if budget_tokens < 0:
        raise _AssemblyFailure("invalid_budget", ())
    registry = _TrustedSourceRegistry.build(policies)
    results_by_identity = _validated_results(registry, source_results)
    ordered_pairs = tuple(
        (policy, results_by_identity[(policy.source, policy.key)]) for policy in registry.ordered()
    )
    initial = _initial_admissions(ordered_pairs)
    _raise_required_source_failure(initial)
    _reserve_required_budget(initial, budget_tokens)
    return _admit_with_budget(initial, budget_tokens)


def _validated_results(
    registry: _TrustedSourceRegistry, source_results: Sequence[RequestSourceResult]
) -> dict[tuple[str, str], RequestSourceResult]:
    identities = [(source_result.source, source_result.key) for source_result in source_results]
    if len(identities) != len(set(identities)) or len(source_results) != len(registry.policies):
        raise _AssemblyFailure("internal_invariant_violation", ())
    if any(identity not in registry.by_identity for identity in identities):
        raise _AssemblyFailure("unknown_source_result", ())
    results = {
        identity: source_result
        for identity, source_result in zip(identities, source_results, strict=True)
    }
    if set(results) != set(registry.by_identity):
        raise _AssemblyFailure("internal_invariant_violation", ())
    for identity, source_result in results.items():
        _validate_source_result(registry.by_identity[identity], source_result)
    return results


def _validate_source_result(
    policy: TrustedSourcePolicy, source_result: RequestSourceResult
) -> None:
    if source_result.status is SourceResultStatus.PROVIDED:
        if source_result.fragment is None or source_result.reason_code is not None:
            raise _AssemblyFailure("source_result_invalid", ())
        _validate_fragment_matches_policy(policy, source_result.fragment)
        return
    if source_result.fragment is not None:
        raise _AssemblyFailure("source_result_invalid", ())
    if source_result.status is SourceResultStatus.NOT_APPLICABLE:
        if source_result.reason_code is not None:
            raise _AssemblyFailure("source_result_invalid", ())
        if policy.applicability is SourceApplicability.ALWAYS:
            raise _AssemblyFailure("source_policy_mismatch", ())
        return
    if source_result.reason_code not in policy.failure_reason_codes:
        raise _AssemblyFailure("unsafe_source_reason", ())


def _validate_fragment_matches_policy(
    policy: TrustedSourcePolicy, fragment: RequestFragment
) -> None:
    expected_truncatable = policy.truncation is FragmentTruncation.ALLOWED
    if (
        fragment.source != policy.source
        or fragment.key != policy.key
        or fragment.requirement is not policy.requirement
        or fragment.persistence is not policy.persistence
        or fragment.priority != policy.priority
        or fragment.truncatable is not expected_truncatable
    ):
        raise _AssemblyFailure("source_policy_mismatch", ())


def _initial_admissions(
    ordered_pairs: Sequence[tuple[TrustedSourcePolicy, RequestSourceResult]],
) -> tuple[_Admission, ...]:
    return tuple(
        _initial_admission(policy, source_result) for policy, source_result in ordered_pairs
    )


def _initial_admission(
    policy: TrustedSourcePolicy, source_result: RequestSourceResult
) -> _Admission:
    if source_result.status is SourceResultStatus.NOT_APPLICABLE:
        return _Admission(policy, None, _outcome(policy, FragmentStatus.NOT_APPLICABLE, 0, 0))
    if source_result.status is SourceResultStatus.FAILED:
        status = (
            FragmentStatus.FAILED
            if policy.requirement is FragmentRequirement.REQUIRED
            else FragmentStatus.DEGRADED
        )
        return _Admission(
            policy,
            None,
            _outcome(policy, status, 0, 0, source_result.reason_code),
        )
    fragment = source_result.fragment
    if fragment is None:
        raise _AssemblyFailure("internal_invariant_violation", ())
    if not fragment.content and policy.requirement is FragmentRequirement.REQUIRED:
        outcome = _outcome(
            policy,
            FragmentStatus.FAILED,
            0,
            0,
            "required_source_invalid",
        )
        return _Admission(policy, None, outcome)
    estimate = estimate_injection_tokens(fragment.content)
    return _Admission(policy, fragment, _outcome(policy, FragmentStatus.INCLUDED, estimate, 0))


def _raise_required_source_failure(admissions: Sequence[_Admission]) -> None:
    observed: list[FragmentOutcome] = []
    for admission in admissions:
        observed.append(admission.outcome)
        if (
            admission.policy.requirement is FragmentRequirement.REQUIRED
            and admission.outcome.status is FragmentStatus.FAILED
        ):
            reason_code = admission.outcome.reason_code or "required_source_invalid"
            raise _AssemblyFailure(reason_code, tuple(observed))


def _reserve_required_budget(admissions: Sequence[_Admission], budget_tokens: int) -> None:
    required_tokens = sum(
        admission.outcome.estimated_tokens
        for admission in admissions
        if admission.policy.requirement is FragmentRequirement.REQUIRED
        and admission.policy.budget_class is FragmentBudgetClass.BUDGETED
    )
    if required_tokens <= budget_tokens:
        return
    outcomes = tuple(_required_budget_outcome(admission) for admission in admissions)
    raise _AssemblyFailure("required_content_exceeds_budget", outcomes)


def _required_budget_outcome(admission: _Admission) -> FragmentOutcome:
    policy = admission.policy
    estimate = admission.outcome.estimated_tokens
    if (
        policy.requirement is FragmentRequirement.REQUIRED
        and policy.budget_class is FragmentBudgetClass.NON_BUDGETED
    ):
        return _outcome(policy, FragmentStatus.INCLUDED, estimate, estimate)
    if policy.requirement is FragmentRequirement.REQUIRED:
        return _outcome(
            policy,
            FragmentStatus.FAILED,
            estimate,
            0,
            "required_content_exceeds_budget",
        )
    if admission.outcome.status in {FragmentStatus.DEGRADED, FragmentStatus.NOT_APPLICABLE}:
        return admission.outcome
    return _outcome(policy, FragmentStatus.OMITTED_BUDGET, estimate, 0, "assembly_stopped")


def _admit_with_budget(initial: Sequence[_Admission], budget_tokens: int) -> tuple[_Admission, ...]:
    admitted: list[_Admission] = []
    used_tokens = 0
    truncation_budget = budget_tokens
    for admission in initial:
        remaining_tokens = budget_tokens - used_tokens
        updated, charged_tokens = _admit_one(
            admission, remaining_tokens, min(remaining_tokens, truncation_budget)
        )
        admitted.append(updated)
        used_tokens += charged_tokens
        if _used_truncation_attempt(admission, remaining_tokens):
            truncation_budget = 0
    return tuple(admitted)


def _admit_one(
    admission: _Admission, remaining_tokens: int, truncation_budget: int
) -> tuple[_Admission, int]:
    fragment = admission.fragment
    if fragment is None:
        return admission, 0
    estimate = admission.outcome.estimated_tokens
    if admission.policy.budget_class is FragmentBudgetClass.NON_BUDGETED:
        return _included_admission(admission, estimate), 0
    if estimate <= remaining_tokens:
        return _included_admission(admission, estimate), estimate
    if admission.policy.requirement is FragmentRequirement.REQUIRED:
        raise _AssemblyFailure("internal_invariant_violation", ())
    return _admit_optional(admission, truncation_budget)


def _included_admission(admission: _Admission, estimate: int) -> _Admission:
    return _Admission(
        admission.policy,
        admission.fragment,
        _outcome(admission.policy, FragmentStatus.INCLUDED, estimate, estimate),
    )


def _admit_optional(admission: _Admission, truncation_budget: int) -> tuple[_Admission, int]:
    fragment = admission.fragment
    if fragment is None:
        raise _AssemblyFailure("internal_invariant_violation", ())
    estimate = admission.outcome.estimated_tokens
    if admission.policy.truncation is FragmentTruncation.FORBIDDEN or truncation_budget <= 0:
        outcome = _outcome(
            admission.policy, FragmentStatus.OMITTED_BUDGET, estimate, 0, "budget_exceeded"
        )
        return _Admission(admission.policy, fragment, outcome), 0
    truncated = _truncate_to_tokens(fragment.content, truncation_budget)
    if not truncated:
        outcome = _outcome(
            admission.policy, FragmentStatus.OMITTED_BUDGET, estimate, 0, "budget_exceeded"
        )
        return _Admission(admission.policy, fragment, outcome), 0
    admitted_estimate = estimate_injection_tokens(truncated)
    truncated_fragment = _replace_fragment_content(fragment, truncated)
    outcome = _outcome(
        admission.policy,
        FragmentStatus.TRUNCATED,
        estimate,
        admitted_estimate,
        "budget_truncated",
    )
    return _Admission(admission.policy, truncated_fragment, outcome), admitted_estimate


def _used_truncation_attempt(admission: _Admission, remaining_tokens: int) -> bool:
    return bool(
        admission.fragment is not None
        and admission.policy.requirement is FragmentRequirement.BEST_EFFORT
        and admission.policy.truncation is FragmentTruncation.ALLOWED
        and admission.outcome.estimated_tokens > remaining_tokens
    )


def _replace_fragment_content(fragment: RequestFragment, content: str) -> RequestFragment:
    return RequestFragment(
        key=fragment.key,
        content=content,
        source=fragment.source,
        requirement=fragment.requirement,
        persistence=fragment.persistence,
        priority=fragment.priority,
        truncatable=fragment.truncatable,
    )


def _truncate_to_tokens(text: str, budget_tokens: int) -> str:
    max_characters = max(0, budget_tokens * 4)
    if max_characters <= 1:
        return ""
    truncated = text[: max_characters - 1].rstrip()
    if "\n" in truncated:
        truncated = truncated.rsplit("\n", 1)[0].rstrip()
    return f"{truncated}\n…" if truncated else ""


def _outcome(
    policy: TrustedSourcePolicy,
    status: FragmentStatus,
    estimated_tokens: int,
    admitted_tokens: int,
    reason_code: str | None = None,
) -> FragmentOutcome:
    return FragmentOutcome(
        key=policy.key,
        source=policy.source,
        requirement=policy.requirement,
        persistence=policy.persistence,
        status=status,
        estimated_tokens=estimated_tokens,
        admitted_tokens=admitted_tokens,
        reason_code=reason_code,
    )


class RequestAssembler:
    def __init__(
        self,
        policies: Sequence[TrustedSourcePolicy],
        source_results: Sequence[RequestSourceResult],
        *,
        history_normalizer: HistoryNormalizer = normalize_history,
    ) -> None:
        self._policies = tuple(policies)
        self._source_results = tuple(source_results)
        self._history_normalizer = history_normalizer

    async def assemble(self, request: RequestAssemblyInput) -> AssembledRequest:
        outcomes: tuple[FragmentOutcome, ...] = ()
        boundary_reason = "internal_invariant_violation"
        try:
            admissions = admit_source_results(
                self._policies, self._source_results, request.budget_tokens
            )
            projection = _project_admissions(admissions, self._policies)
            outcomes = projection.outcomes
            boundary_reason = "history_normalization_failed"
            provider_history = tuple(
                self._history_normalizer(
                    (
                        *projection.leading_messages,
                        *request.persisted_history,
                        *projection.trailing_messages,
                    )
                )
            )
            boundary_reason = "internal_invariant_violation"
            return AssembledRequest(
                system_prompt=request.system_prompt,
                provider_history=provider_history,
                history_appends=projection.history_appends,
                manifest=_successful_manifest(request.budget_tokens, admissions),
            )
        except RequestAssemblyError:
            raise
        except _AssemblyFailure as failure:
            raise _categorized_error(request.budget_tokens, failure, self._policies) from failure
        except Exception as error:
            failure = _AssemblyFailure(boundary_reason, outcomes)
            raise _categorized_error(request.budget_tokens, failure, self._policies) from error


def _project_admissions(
    admissions: Sequence[_Admission], policies: Sequence[TrustedSourcePolicy]
) -> _RequestProjection:
    registry = _TrustedSourceRegistry.build(policies)
    projection_order = sorted(
        admissions,
        key=lambda admission: (
            -admission.policy.priority,
            registry.source_rank[admission.policy.source],
            registry.key_rank[(admission.policy.source, admission.policy.key)],
            admission.policy.key,
        ),
    )
    fragments = tuple(
        admission.fragment
        for admission in projection_order
        if admission.fragment is not None
        and admission.outcome.status in {FragmentStatus.INCLUDED, FragmentStatus.TRUNCATED}
    )
    leading = tuple(
        _fragment_message(fragment)
        for fragment in fragments
        if fragment.source == AGENTS_MD_SOURCE_POLICY.source
    )
    history_fragments = tuple(
        fragment for fragment in fragments if fragment.persistence is FragmentPersistence.HISTORY
    )
    history_message = _combined_history_message(history_fragments)
    request_only = tuple(
        _fragment_message(fragment)
        for fragment in fragments
        if fragment.persistence is FragmentPersistence.REQUEST_ONLY
        and fragment.source != AGENTS_MD_SOURCE_POLICY.source
    )
    trailing = ((history_message,) if history_message is not None else ()) + request_only
    return _RequestProjection(
        leading_messages=leading,
        trailing_messages=trailing,
        history_appends=(history_message,) if history_message is not None else (),
        outcomes=tuple(admission.outcome for admission in admissions),
    )


def _combined_history_message(fragments: Sequence[RequestFragment]) -> Message | None:
    if not fragments:
        return None
    combined = "\n".join(system_reminder(fragment.content).text for fragment in fragments)
    return Message(role="user", content=[TextPart(text=combined)])


def _fragment_message(fragment: RequestFragment) -> Message:
    return Message(role="user", content=[system_reminder(fragment.content)])


def _successful_manifest(budget_tokens: int, admissions: Sequence[_Admission]) -> RequestManifest:
    outcomes = tuple(admission.outcome for admission in admissions)
    status = (
        RequestStatus.DEGRADED
        if any(outcome.status in _DEGRADED_STATUSES for outcome in outcomes)
        else RequestStatus.SUCCEEDED
    )
    budgeted_tokens, non_budgeted_tokens = _aggregate_tokens(admissions)
    return RequestManifest(
        status=status,
        reason_code="optional_fragments_degraded" if status is RequestStatus.DEGRADED else None,
        outcomes=outcomes,
        budget_tokens=budget_tokens,
        budgeted_admitted_tokens=budgeted_tokens,
        non_budgeted_estimated_tokens=non_budgeted_tokens,
    )


def _aggregate_tokens(admissions: Sequence[_Admission]) -> tuple[int, int]:
    budgeted_tokens = sum(
        admission.outcome.admitted_tokens
        for admission in admissions
        if admission.policy.budget_class is FragmentBudgetClass.BUDGETED
    )
    non_budgeted_tokens = sum(
        admission.outcome.estimated_tokens
        for admission in admissions
        if admission.policy.budget_class is FragmentBudgetClass.NON_BUDGETED
        and admission.outcome.status is FragmentStatus.INCLUDED
    )
    return budgeted_tokens, non_budgeted_tokens


def _categorized_error(
    budget_tokens: int,
    failure: _AssemblyFailure,
    policies: Sequence[TrustedSourcePolicy],
) -> RequestAssemblyError:
    manifest = _failed_manifest(budget_tokens, failure.reason_code, failure.outcomes, policies)
    if failure.reason_code in {"invalid_budget", "required_content_exceeds_budget"}:
        return RequestBudgetError(failure.reason_code, manifest)
    if failure.reason_code == "internal_invariant_violation":
        return RequestInvariantError(failure.reason_code, manifest)
    if failure.reason_code == "history_normalization_failed":
        return RequestHistoryError(failure.reason_code, manifest)
    return RequestSourceError(failure.reason_code, manifest)


def _failed_manifest(
    budget_tokens: int,
    reason_code: str,
    outcomes: tuple[FragmentOutcome, ...],
    policies: Sequence[TrustedSourcePolicy],
) -> RequestManifest:
    policy_by_identity = {(policy.source, policy.key): policy for policy in policies}
    budgeted_tokens = sum(
        outcome.admitted_tokens
        for outcome in outcomes
        if policy_by_identity[(outcome.source, outcome.key)].budget_class
        is FragmentBudgetClass.BUDGETED
    )
    non_budgeted_tokens = sum(
        outcome.estimated_tokens
        for outcome in outcomes
        if policy_by_identity[(outcome.source, outcome.key)].budget_class
        is FragmentBudgetClass.NON_BUDGETED
        and outcome.status is FragmentStatus.INCLUDED
    )
    return RequestManifest(
        status=RequestStatus.FAILED,
        reason_code=reason_code,
        outcomes=outcomes,
        budget_tokens=budget_tokens,
        budgeted_admitted_tokens=budgeted_tokens,
        non_budgeted_estimated_tokens=non_budgeted_tokens,
    )
