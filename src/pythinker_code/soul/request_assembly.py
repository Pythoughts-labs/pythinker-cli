from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from pythinker_core.message import Message

from pythinker_code.soul.dynamic_injection import estimate_injection_tokens, normalize_history
from pythinker_code.soul.message import system_reminder


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


@dataclass(frozen=True, slots=True)
class _Admission:
    fragment: RequestFragment
    outcome: FragmentOutcome


class _AdmissionFailure(Exception):
    def __init__(self, reason_code: str, outcomes: tuple[FragmentOutcome, ...]) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.outcomes = outcomes


_NON_BUDGETED_SOURCES = frozenset({"agents_md"})
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
_DEGRADED_STATUSES = frozenset(
    {FragmentStatus.TRUNCATED, FragmentStatus.OMITTED_BUDGET, FragmentStatus.DEGRADED}
)


def admit_fragments(
    fragments: Sequence[RequestFragment],
    budget_tokens: int,
    source_order: Sequence[str],
    non_budgeted_sources: frozenset[str] = frozenset(),
) -> tuple[_Admission, ...]:
    """Admit fragments with required-first reservation and deterministic ordering."""
    if budget_tokens < 0:
        raise _AdmissionFailure("invalid_budget", ())
    _validate_fragment_identifiers(fragments, source_order)
    ordered = _ordered_fragments(fragments, source_order)
    _validate_non_budgeted_fragments(ordered, non_budgeted_sources)
    required_tokens = sum(
        estimate_injection_tokens(fragment.content)
        for fragment in ordered
        if fragment.content
        and fragment.requirement is FragmentRequirement.REQUIRED
        and fragment.source not in non_budgeted_sources
    )
    if required_tokens > budget_tokens:
        outcomes = tuple(
            _required_budget_outcome(fragment, non_budgeted_sources) for fragment in ordered
        )
        raise _AdmissionFailure("required_content_exceeds_budget", outcomes)
    return _admit_ordered_fragments(ordered, budget_tokens, non_budgeted_sources)


def _validate_fragment_identifiers(
    fragments: Sequence[RequestFragment], source_order: Sequence[str]
) -> None:
    identifiers = (*source_order, *(fragment.key for fragment in fragments))
    if any(_SAFE_IDENTIFIER.fullmatch(identifier) is None for identifier in identifiers):
        raise _AdmissionFailure("unsafe_fragment_identifier", ())


def _ordered_fragments(
    fragments: Sequence[RequestFragment], source_order: Sequence[str]
) -> tuple[RequestFragment, ...]:
    if len(source_order) != len(set(source_order)):
        raise _AdmissionFailure("invalid_source_order", ())
    source_rank = {source: index for index, source in enumerate(source_order)}
    if any(fragment.source not in source_rank for fragment in fragments):
        raise _AdmissionFailure("unknown_fragment_source", ())
    indexed = enumerate(fragments)
    ordered = sorted(
        indexed,
        key=lambda pair: (
            pair[1].requirement is not FragmentRequirement.REQUIRED,
            -pair[1].priority,
            source_rank[pair[1].source],
            pair[0],
        ),
    )
    return tuple(fragment for _index, fragment in ordered)


def _validate_non_budgeted_fragments(
    fragments: Sequence[RequestFragment], non_budgeted_sources: frozenset[str]
) -> None:
    if any(
        fragment.source in non_budgeted_sources
        and fragment.requirement is not FragmentRequirement.REQUIRED
        for fragment in fragments
    ):
        raise _AdmissionFailure("invalid_non_budgeted_fragment", ())


def _admit_ordered_fragments(
    fragments: Sequence[RequestFragment],
    budget_tokens: int,
    non_budgeted_sources: frozenset[str],
) -> tuple[_Admission, ...]:
    admissions: list[_Admission] = []
    used_tokens = 0
    truncation_budget = budget_tokens
    for fragment in fragments:
        remaining_tokens = budget_tokens - used_tokens
        try:
            admission, charged_tokens = _admit_fragment(
                fragment,
                remaining_tokens,
                min(remaining_tokens, truncation_budget),
                non_budgeted_sources,
            )
        except _AdmissionFailure as failure:
            observed = tuple(admission.outcome for admission in admissions)
            raise _AdmissionFailure(
                failure.reason_code, (*observed, *failure.outcomes)
            ) from failure
        admissions.append(admission)
        used_tokens += charged_tokens
        if admission.outcome.status is FragmentStatus.TRUNCATED:
            truncation_budget = 0
    return tuple(admissions)


def _admit_fragment(
    fragment: RequestFragment,
    remaining_tokens: int,
    truncation_budget: int,
    non_budgeted_sources: frozenset[str],
) -> tuple[_Admission, int]:
    estimate = estimate_injection_tokens(fragment.content) if fragment.content else 0
    if not fragment.content:
        if fragment.requirement is FragmentRequirement.REQUIRED:
            outcome = _outcome(fragment, FragmentStatus.FAILED, 0, 0, "required_source_invalid")
            raise _AdmissionFailure("required_source_invalid", (outcome,))
        return _Admission(fragment, _outcome(fragment, FragmentStatus.NOT_APPLICABLE, 0, 0)), 0
    if fragment.source in non_budgeted_sources or estimate <= remaining_tokens:
        charge = 0 if fragment.source in non_budgeted_sources else estimate
        return _Admission(
            fragment, _outcome(fragment, FragmentStatus.INCLUDED, estimate, estimate)
        ), charge
    if fragment.requirement is FragmentRequirement.REQUIRED:
        outcome = _outcome(
            fragment, FragmentStatus.FAILED, estimate, 0, "required_content_exceeds_budget"
        )
        raise _AdmissionFailure("required_content_exceeds_budget", (outcome,))
    return _admit_optional_fragment(fragment, estimate, truncation_budget)


def _admit_optional_fragment(
    fragment: RequestFragment, estimate: int, remaining_tokens: int
) -> tuple[_Admission, int]:
    if not fragment.truncatable or remaining_tokens <= 0:
        outcome = _outcome(fragment, FragmentStatus.OMITTED_BUDGET, estimate, 0, "budget_exceeded")
        return _Admission(fragment, outcome), 0
    truncated = _truncate_to_tokens(fragment.content, remaining_tokens)
    if not truncated:
        outcome = _outcome(fragment, FragmentStatus.OMITTED_BUDGET, estimate, 0, "budget_exceeded")
        return _Admission(fragment, outcome), 0
    admitted_estimate = estimate_injection_tokens(truncated)
    admitted = RequestFragment(
        key=fragment.key,
        content=truncated,
        source=fragment.source,
        requirement=fragment.requirement,
        persistence=fragment.persistence,
        priority=fragment.priority,
        truncatable=fragment.truncatable,
    )
    outcome = _outcome(
        fragment, FragmentStatus.TRUNCATED, estimate, admitted_estimate, "budget_truncated"
    )
    return _Admission(admitted, outcome), admitted_estimate


def _outcome(
    fragment: RequestFragment,
    status: FragmentStatus,
    estimated_tokens: int,
    admitted_tokens: int,
    reason_code: str | None = None,
) -> FragmentOutcome:
    return FragmentOutcome(
        key=fragment.key,
        source=fragment.source,
        requirement=fragment.requirement,
        persistence=fragment.persistence,
        status=status,
        estimated_tokens=estimated_tokens,
        admitted_tokens=admitted_tokens,
        reason_code=reason_code,
    )


def _required_budget_outcome(
    fragment: RequestFragment, non_budgeted_sources: frozenset[str]
) -> FragmentOutcome:
    estimate = estimate_injection_tokens(fragment.content) if fragment.content else 0
    if (
        fragment.requirement is FragmentRequirement.REQUIRED
        and fragment.source not in non_budgeted_sources
    ):
        return _outcome(
            fragment, FragmentStatus.FAILED, estimate, 0, "required_content_exceeds_budget"
        )
    return _outcome(fragment, FragmentStatus.OMITTED_BUDGET, estimate, 0, "assembly_stopped")


def _truncate_to_tokens(text: str, budget_tokens: int) -> str:
    max_chars = max(0, budget_tokens * 4)
    if max_chars <= 1:
        return ""
    truncated = text[: max_chars - 1].rstrip()
    if "\n" in truncated:
        truncated = truncated.rsplit("\n", 1)[0].rstrip()
    return f"{truncated}\n…" if truncated else ""


class RequestAssembler:
    def __init__(
        self, fragments: Sequence[RequestFragment], *, source_order: Sequence[str]
    ) -> None:
        self._fragments = tuple(fragments)
        self._source_order = tuple(source_order)

    async def assemble(self, request: RequestAssemblyInput) -> AssembledRequest:
        try:
            admissions = admit_fragments(
                self._fragments,
                request.budget_tokens,
                self._source_order,
                _NON_BUDGETED_SOURCES,
            )
        except _AdmissionFailure as failure:
            manifest = _failed_manifest(request.budget_tokens, failure)
            error_type = (
                RequestBudgetError
                if failure.reason_code in {"invalid_budget", "required_content_exceeds_budget"}
                else RequestSourceError
            )
            raise error_type(failure.reason_code, manifest) from failure
        return _assembled_request(request, admissions)


def _assembled_request(
    request: RequestAssemblyInput, admissions: Sequence[_Admission]
) -> AssembledRequest:
    admitted = tuple(
        admission.fragment
        for admission in admissions
        if admission.outcome.status in {FragmentStatus.INCLUDED, FragmentStatus.TRUNCATED}
    )
    messages = tuple(_fragment_message(fragment) for fragment in admitted)
    history_appends = tuple(
        message
        for fragment, message in zip(admitted, messages, strict=True)
        if fragment.persistence is FragmentPersistence.HISTORY
    )
    provider_history = tuple(normalize_history((*request.persisted_history, *messages)))
    return AssembledRequest(
        system_prompt=request.system_prompt,
        provider_history=provider_history,
        history_appends=history_appends,
        manifest=_successful_manifest(request.budget_tokens, admissions),
    )


def _fragment_message(fragment: RequestFragment) -> Message:
    return Message(role="user", content=[system_reminder(fragment.content)])


def _successful_manifest(budget_tokens: int, admissions: Sequence[_Admission]) -> RequestManifest:
    outcomes = tuple(admission.outcome for admission in admissions)
    status = (
        RequestStatus.DEGRADED
        if any(outcome.status in _DEGRADED_STATUSES for outcome in outcomes)
        else RequestStatus.SUCCEEDED
    )
    return RequestManifest(
        status=status,
        reason_code="optional_fragments_degraded" if status is RequestStatus.DEGRADED else None,
        outcomes=outcomes,
        budget_tokens=budget_tokens,
        budgeted_admitted_tokens=sum(
            outcome.admitted_tokens
            for outcome in outcomes
            if outcome.source not in _NON_BUDGETED_SOURCES
        ),
        non_budgeted_estimated_tokens=sum(
            outcome.estimated_tokens
            for outcome in outcomes
            if outcome.source in _NON_BUDGETED_SOURCES
        ),
    )


def _failed_manifest(budget_tokens: int, failure: _AdmissionFailure) -> RequestManifest:
    budgeted_admitted_tokens = sum(
        outcome.admitted_tokens
        for outcome in failure.outcomes
        if outcome.source not in _NON_BUDGETED_SOURCES
    )
    non_budgeted_estimated_tokens = sum(
        outcome.estimated_tokens
        for outcome in failure.outcomes
        if outcome.source in _NON_BUDGETED_SOURCES and outcome.status is FragmentStatus.INCLUDED
    )
    return RequestManifest(
        status=RequestStatus.FAILED,
        reason_code=failure.reason_code,
        outcomes=failure.outcomes,
        budget_tokens=budget_tokens,
        budgeted_admitted_tokens=budgeted_admitted_tokens,
        non_budgeted_estimated_tokens=non_budgeted_estimated_tokens,
    )
