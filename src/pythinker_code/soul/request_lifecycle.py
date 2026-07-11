"""Private lifecycle owner for dynamic request-source preparation and finalization."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pythinker_core.message import Message

from pythinker_code.soul.dynamic_injection import (
    DynamicInjectionProvider,
    PreparedInjection,
)
from pythinker_code.soul.dynamic_injections.model_defense import ModelDefenseInjectionProvider
from pythinker_code.soul.dynamic_injections.permissions_state import PermissionsInjectionProvider
from pythinker_code.soul.request_assembly import (
    FragmentBudgetClass,
    FragmentPersistence,
    FragmentRequirement,
    FragmentStatus,
    FragmentTruncation,
    RequestFragment,
    RequestManifest,
    RequestSourceResult,
    RequestStatus,
    SourceApplicability,
    SourceResultStatus,
    TrustedSourcePolicy,
)

if TYPE_CHECKING:
    from pythinker_code.soul.pythinkersoul import PythinkerSoul


_PERMISSIONS_SOURCE = "permissions_state"
_MODEL_DEFENSE_SOURCE = "model_defense"
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")


class RequestLifecycleError(RuntimeError):
    """Categorized request lifecycle failure."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class SourceAcknowledgement:
    registration_id: str
    source: str
    key: str
    prepared_identity: str


@dataclass(frozen=True, slots=True)
class PreparedSources:
    policies: tuple[TrustedSourcePolicy, ...]
    results: tuple[RequestSourceResult, ...]
    acknowledgements: tuple[SourceAcknowledgement, ...]


@dataclass(slots=True)
class _Registration:
    registration_id: str
    source: str
    provider: DynamicInjectionProvider
    lock: asyncio.Lock


FailureReporter = Callable[[DynamicInjectionProvider, Exception], None]


class RequestLifecycle:
    """Own provider identities, preparation serialization, dedupe generations, and finalize."""

    def __init__(self, providers: Sequence[DynamicInjectionProvider]) -> None:
        self._registrations: dict[int, _Registration] = {}
        self._source_counts: defaultdict[str, int] = defaultdict(int)
        self._next_registration = 0
        self._generation = 0
        self._committed: set[tuple[int, str, str]] = set()
        self.sync_providers(providers)
        self._default_permissions = _exactly_one_role(
            providers,
            PermissionsInjectionProvider,
        )
        self._default_model_defense = _exactly_one_role(
            providers,
            ModelDefenseInjectionProvider,
        )

    @property
    def history_generation(self) -> int:
        return self._generation

    @property
    def committed_identity_count(self) -> int:
        return len(self._committed)

    def sync_providers(self, providers: Sequence[DynamicInjectionProvider]) -> None:
        for provider in providers:
            provider_key = id(provider)
            if provider_key in self._registrations:
                continue
            source_base = _source_base(provider)
            self._source_counts[source_base] += 1
            occurrence = self._source_counts[source_base]
            source = (
                source_base
                if occurrence == 1
                else _stable_identifier(f"{source_base}:{occurrence}")
            )
            self._next_registration += 1
            self._registrations[provider_key] = _Registration(
                registration_id=f"provider-{self._next_registration:04d}",
                source=source,
                provider=provider,
                lock=asyncio.Lock(),
            )

    async def prepare_required(
        self,
        providers: Sequence[DynamicInjectionProvider],
        history: Sequence[Message],
        soul: PythinkerSoul,
        report_failure: FailureReporter,
    ) -> PreparedSources:
        self.sync_providers(providers)
        registrations = self._ordered_required(providers)
        return await self._prepare(registrations, history, soul, report_failure)

    async def prepare_optional(
        self,
        providers: Sequence[DynamicInjectionProvider],
        history: Sequence[Message],
        soul: PythinkerSoul,
        report_failure: FailureReporter,
        *,
        enabled: bool,
    ) -> PreparedSources:
        self.sync_providers(providers)
        registrations = self._ordered_optional(providers)
        if not enabled:
            policies = tuple(
                _policy(registration, registration.source) for registration in registrations
            )
            return PreparedSources(
                policies,
                tuple(_not_applicable(policy) for policy in policies),
                (),
            )
        return await self._prepare(registrations, history, soul, report_failure)

    async def _prepare(
        self,
        registrations: Sequence[_Registration],
        history: Sequence[Message],
        soul: PythinkerSoul,
        report_failure: FailureReporter,
    ) -> PreparedSources:
        policies: list[TrustedSourcePolicy] = []
        results: list[RequestSourceResult] = []
        acknowledgements: list[SourceAcknowledgement] = []
        for registration in registrations:
            batch = await self._prepare_one(registration, history, soul, report_failure)
            policies.extend(batch.policies)
            results.extend(batch.results)
            acknowledgements.extend(batch.acknowledgements)
            if any(
                result.status is SourceResultStatus.FAILED
                and policy.requirement is FragmentRequirement.REQUIRED
                for policy, result in zip(batch.policies, batch.results, strict=True)
            ):
                break
        return PreparedSources(tuple(policies), tuple(results), tuple(acknowledgements))

    async def _prepare_one(
        self,
        registration: _Registration,
        history: Sequence[Message],
        soul: PythinkerSoul,
        report_failure: FailureReporter,
    ) -> PreparedSources:
        try:
            async with registration.lock:
                prepared = await registration.provider.prepare_injections(history, soul)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            report_failure(registration.provider, exc)
            policy = _policy(registration, registration.source)
            return PreparedSources(
                (policy,),
                (_failed(policy, _unavailable_reason(registration)),),
                (),
            )
        if not prepared:
            policy = _policy(registration, registration.source)
            if isinstance(registration.provider, PermissionsInjectionProvider):
                return PreparedSources(
                    (policy,),
                    (_failed(policy, "permissions_state_invalid"),),
                    (),
                )
            return PreparedSources((policy,), (_not_applicable(policy),), ())
        try:
            return self._prepared_results(registration, prepared)
        except RequestLifecycleError as exc:
            report_failure(registration.provider, exc)
            policy = _policy(registration, registration.source)
            reason = (
                "permissions_state_invalid"
                if isinstance(registration.provider, PermissionsInjectionProvider)
                else (
                    "model_defense_invalid"
                    if isinstance(registration.provider, ModelDefenseInjectionProvider)
                    else "provider_failed"
                )
            )
            return PreparedSources((policy,), (_failed(policy, reason),), ())

    def _prepared_results(
        self,
        registration: _Registration,
        prepared: Sequence[PreparedInjection],
    ) -> PreparedSources:
        identities = [item.identity for item in prepared]
        if len(identities) != len(set(identities)):
            raise RequestLifecycleError("provider_identity_invalid")
        policies: list[TrustedSourcePolicy] = []
        results: list[RequestSourceResult] = []
        acknowledgements: list[SourceAcknowledgement] = []
        for item in prepared:
            key = _stable_identifier(item.identity)
            policy = _policy(registration, key)
            policies.append(policy)
            invalid_reason = _invalid_security_type(registration.provider, item.type)
            if invalid_reason is not None:
                results.append(_failed(policy, invalid_reason))
                continue
            fragment = RequestFragment(
                key=key,
                content=item.content,
                source=policy.source,
                requirement=policy.requirement,
                persistence=policy.persistence,
                priority=policy.priority,
                truncatable=policy.truncation is FragmentTruncation.ALLOWED,
            )
            committed_identity = (
                self._generation,
                registration.registration_id,
                item.identity,
            )
            status = (
                SourceResultStatus.ALREADY_SATISFIED
                if committed_identity in self._committed
                else SourceResultStatus.PROVIDED
            )
            results.append(
                RequestSourceResult(
                    policy.source,
                    key,
                    status,
                    fragment,
                    None,
                    self._generation if status is SourceResultStatus.ALREADY_SATISFIED else None,
                )
            )
            if status is SourceResultStatus.PROVIDED:
                acknowledgements.append(
                    SourceAcknowledgement(
                        registration.registration_id,
                        policy.source,
                        key,
                        item.identity,
                    )
                )
        return PreparedSources(tuple(policies), tuple(results), tuple(acknowledgements))

    def finalize(
        self,
        manifest: RequestManifest,
        acknowledgements: Sequence[SourceAcknowledgement],
    ) -> None:
        admitted = {
            (outcome.source, outcome.key)
            for outcome in manifest.outcomes
            if outcome.persistence is FragmentPersistence.HISTORY
            and outcome.status in {FragmentStatus.INCLUDED, FragmentStatus.TRUNCATED}
        }
        by_registration: defaultdict[str, list[SourceAcknowledgement]] = defaultdict(list)
        for acknowledgement in acknowledgements:
            if (acknowledgement.source, acknowledgement.key) in admitted:
                by_registration[acknowledgement.registration_id].append(acknowledgement)
        failures: list[Exception] = []
        registrations = {
            registration.registration_id: registration
            for registration in self._registrations.values()
        }
        for registration_id, batch in by_registration.items():
            registration = registrations[registration_id]
            for acknowledgement in batch:
                self._committed.add(
                    (self._generation, registration_id, acknowledgement.prepared_identity)
                )
            try:
                registration.provider.acknowledge_injections(
                    tuple(item.prepared_identity for item in batch)
                )
            except Exception as exc:
                failures.append(exc)
        if failures:
            from pythinker_code.telemetry.errors import report_handled_error

            for failure in failures:
                report_handled_error(failure, site="soul.request_lifecycle.finalize")
            raise RequestLifecycleError("provider_finalization_failed") from failures[0]

    def context_rebuilt(self) -> None:
        self._generation += 1
        self._committed.clear()

    def rearm(
        self, providers: Sequence[DynamicInjectionProvider], key: str
    ) -> tuple[Exception, ...]:
        self.sync_providers(providers)
        failures: list[Exception] = []
        for provider in providers:
            registration = self._registrations[id(provider)]
            try:
                recognized = provider.rearm(key)
            except Exception as exc:
                failures.append(exc)
                continue
            if recognized:
                self._committed = {
                    identity
                    for identity in self._committed
                    if identity[1] != registration.registration_id
                }
        return tuple(failures)

    def _ordered_required(
        self, providers: Sequence[DynamicInjectionProvider]
    ) -> tuple[_Registration, ...]:
        permissions = [
            provider for provider in providers if isinstance(provider, PermissionsInjectionProvider)
        ]
        defenses = [
            provider
            for provider in providers
            if isinstance(provider, ModelDefenseInjectionProvider)
        ]
        if len(permissions) > 1 or len(defenses) > 1:
            raise RequestLifecycleError("ambiguous_required_provider")
        if not permissions:
            if self._default_permissions is None:
                raise RequestLifecycleError("required_provider_missing")
            permissions = [self._default_permissions]
        if not defenses:
            if self._default_model_defense is None:
                raise RequestLifecycleError("required_provider_missing")
            defenses = [self._default_model_defense]
        return tuple(self._registrations[id(provider)] for provider in (*permissions, *defenses))

    def _ordered_optional(
        self, providers: Sequence[DynamicInjectionProvider]
    ) -> tuple[_Registration, ...]:
        return tuple(
            self._registrations[id(provider)]
            for provider in providers
            if not isinstance(
                provider,
                (PermissionsInjectionProvider, ModelDefenseInjectionProvider),
            )
        )


def _source_base(provider: DynamicInjectionProvider) -> str:
    if isinstance(provider, PermissionsInjectionProvider):
        return _PERMISSIONS_SOURCE
    if isinstance(provider, ModelDefenseInjectionProvider):
        return _MODEL_DEFENSE_SOURCE
    return _stable_identifier(type(provider).__name__.lstrip("_"))


def _exactly_one_role(
    providers: Sequence[DynamicInjectionProvider],
    role: type[DynamicInjectionProvider],
) -> DynamicInjectionProvider | None:
    matches = [provider for provider in providers if isinstance(provider, role)]
    return matches[0] if len(matches) == 1 else None


def _stable_identifier(identifier: str) -> str:
    if _SAFE_IDENTIFIER.fullmatch(identifier):
        return identifier
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", identifier).strip("_.:-")
    prefix = normalized[:48] or "provider"
    digest = hashlib.sha256(identifier.encode(encoding="utf-8")).hexdigest()[:12]
    return f"{prefix}:{digest}"


def _policy(registration: _Registration, key: str) -> TrustedSourcePolicy:
    provider = registration.provider
    required = isinstance(provider, (PermissionsInjectionProvider, ModelDefenseInjectionProvider))
    if isinstance(provider, PermissionsInjectionProvider):
        failure_codes = ("permissions_state_unavailable", "permissions_state_invalid")
        applicability = SourceApplicability.ALWAYS
    elif isinstance(provider, ModelDefenseInjectionProvider):
        failure_codes = ("model_defense_unavailable", "model_defense_invalid")
        applicability = SourceApplicability.MAY_BE_NOT_APPLICABLE
    else:
        failure_codes = ("provider_failed", "provider_identity_invalid")
        applicability = SourceApplicability.MAY_BE_NOT_APPLICABLE
    return TrustedSourcePolicy(
        source=registration.source,
        key=key,
        requirement=FragmentRequirement.REQUIRED if required else FragmentRequirement.BEST_EFFORT,
        persistence=FragmentPersistence.HISTORY,
        priority=100,
        budget_class=FragmentBudgetClass.BUDGETED,
        truncation=FragmentTruncation.FORBIDDEN if required else FragmentTruncation.ALLOWED,
        applicability=applicability,
        failure_reason_codes=failure_codes,
    )


def _not_applicable(policy: TrustedSourcePolicy) -> RequestSourceResult:
    return RequestSourceResult(
        policy.source,
        policy.key,
        SourceResultStatus.NOT_APPLICABLE,
        None,
        None,
    )


def _failed(policy: TrustedSourcePolicy, reason: str) -> RequestSourceResult:
    return RequestSourceResult(policy.source, policy.key, SourceResultStatus.FAILED, None, reason)


def _unavailable_reason(registration: _Registration) -> str:
    if isinstance(registration.provider, PermissionsInjectionProvider):
        return "permissions_state_unavailable"
    if isinstance(registration.provider, ModelDefenseInjectionProvider):
        return "model_defense_unavailable"
    return "provider_failed"


def _invalid_security_type(provider: DynamicInjectionProvider, injection_type: str) -> str | None:
    if isinstance(provider, PermissionsInjectionProvider):
        return None if injection_type == _PERMISSIONS_SOURCE else "permissions_state_invalid"
    if isinstance(provider, ModelDefenseInjectionProvider):
        return (
            None
            if injection_type.startswith(f"{_MODEL_DEFENSE_SOURCE}:")
            else "model_defense_invalid"
        )
    return None


def failed_manifest(reason_code: str, prior: RequestManifest | None) -> RequestManifest:
    """Return a fresh sanitized failed manifest without retaining stale success state."""
    return RequestManifest(
        status=RequestStatus.FAILED,
        reason_code=_stable_identifier(reason_code),
        outcomes=prior.outcomes if prior is not None else (),
        budget_tokens=prior.budget_tokens if prior is not None else 0,
        budgeted_admitted_tokens=prior.budgeted_admitted_tokens if prior is not None else 0,
        non_budgeted_estimated_tokens=(
            prior.non_budgeted_estimated_tokens if prior is not None else 0
        ),
    )
