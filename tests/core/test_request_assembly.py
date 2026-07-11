from __future__ import annotations

import pytest
from pythinker_core.message import Message

from pythinker_code.soul.request_assembly import (
    FragmentPersistence,
    FragmentRequirement,
    FragmentStatus,
    RequestAssembler,
    RequestAssemblyError,
    RequestAssemblyInput,
    RequestFragment,
    RequestStatus,
)


def _fragment(
    key: str,
    content: str,
    *,
    source: str,
    requirement: FragmentRequirement = FragmentRequirement.BEST_EFFORT,
    persistence: FragmentPersistence = FragmentPersistence.HISTORY,
    priority: int = 100,
    truncatable: bool = False,
) -> RequestFragment:
    return RequestFragment(
        key=key,
        content=content,
        source=source,
        requirement=requirement,
        persistence=persistence,
        priority=priority,
        truncatable=truncatable,
    )


def _request(*, budget_tokens: int, history: tuple[Message, ...] = ()) -> RequestAssemblyInput:
    return RequestAssemblyInput(
        system_prompt="stable system prompt",
        persisted_history=history,
        current_task="implement request assembly",
        budget_tokens=budget_tokens,
    )


@pytest.mark.asyncio
async def test_required_fragments_are_reserved_before_higher_priority_optional_content() -> None:
    required = _fragment(
        "permission",
        "r" * 20,
        source="permissions",
        requirement=FragmentRequirement.REQUIRED,
        priority=1,
    )
    optional = _fragment(
        "plan",
        "o" * 20,
        source="plan",
        priority=1_000,
    )

    assembled = await RequestAssembler(
        (optional, required), source_order=("permissions", "plan")
    ).assemble(_request(budget_tokens=5))

    assert [outcome.key for outcome in assembled.manifest.outcomes] == ["permission", "plan"]
    assert [outcome.status for outcome in assembled.manifest.outcomes] == [
        FragmentStatus.INCLUDED,
        FragmentStatus.OMITTED_BUDGET,
    ]
    assert assembled.manifest.budgeted_admitted_tokens == 5


@pytest.mark.asyncio
async def test_agents_preamble_is_required_visible_and_non_budgeted() -> None:
    agents = _fragment(
        "agents_preamble",
        "a" * 40,
        source="agents_md",
        requirement=FragmentRequirement.REQUIRED,
        persistence=FragmentPersistence.REQUEST_ONLY,
    )
    optional = _fragment("plan", "p" * 20, source="plan")

    assembled = await RequestAssembler(
        (optional, agents), source_order=("agents_md", "plan")
    ).assemble(_request(budget_tokens=5))

    assert assembled.manifest.status is RequestStatus.SUCCEEDED
    assert assembled.manifest.budgeted_admitted_tokens == 5
    assert assembled.manifest.non_budgeted_estimated_tokens == 10
    assert [outcome.admitted_tokens for outcome in assembled.manifest.outcomes] == [10, 5]
    assert assembled.history_appends[0].extract_text("").startswith("<system-reminder>")
    assert "p" * 20 in assembled.history_appends[0].extract_text("")
    assert "a" * 40 in assembled.provider_history[-1].extract_text("")


@pytest.mark.asyncio
async def test_equal_priority_fragments_follow_explicit_source_order() -> None:
    git = _fragment("git", "git", source="git", persistence=FragmentPersistence.REQUEST_ONLY)
    plan = _fragment("plan", "plan", source="plan", persistence=FragmentPersistence.REQUEST_ONLY)

    first = await RequestAssembler((git, plan), source_order=("plan", "git")).assemble(
        _request(budget_tokens=10)
    )
    second = await RequestAssembler((plan, git), source_order=("plan", "git")).assemble(
        _request(budget_tokens=10)
    )

    assert [outcome.key for outcome in first.manifest.outcomes] == ["plan", "git"]
    assert first.provider_history == second.provider_history


@pytest.mark.asyncio
async def test_oversized_optional_fragment_is_truncated_only_when_allowed() -> None:
    fixed = _fragment("fixed", "f" * 40, source="fixed")
    flexible = _fragment("flexible", "alpha\nbeta\ngamma" * 10, source="flexible", truncatable=True)

    assembled = await RequestAssembler(
        (fixed, flexible), source_order=("fixed", "flexible")
    ).assemble(_request(budget_tokens=5))

    outcomes = {outcome.key: outcome for outcome in assembled.manifest.outcomes}
    assert outcomes["fixed"].status is FragmentStatus.OMITTED_BUDGET
    assert outcomes["fixed"].admitted_tokens == 0
    assert outcomes["flexible"].status is FragmentStatus.TRUNCATED
    assert outcomes["flexible"].admitted_tokens <= 5
    assert assembled.provider_history[-1].extract_text("").endswith("…\n</system-reminder>")


@pytest.mark.asyncio
async def test_exact_optional_budget_includes_fragment_without_truncation() -> None:
    fragment = _fragment("exact", "x" * 20, source="exact", truncatable=True)

    assembled = await RequestAssembler((fragment,), source_order=("exact",)).assemble(
        _request(budget_tokens=5)
    )

    assert assembled.manifest.outcomes[0].status is FragmentStatus.INCLUDED
    assert assembled.manifest.outcomes[0].estimated_tokens == 5
    assert assembled.manifest.outcomes[0].admitted_tokens == 5


@pytest.mark.asyncio
async def test_request_only_fragment_never_appears_in_history_appends() -> None:
    persisted = (Message(role="assistant", content="previous"),)
    request_only = _fragment(
        "skills", "candidate", source="skills", persistence=FragmentPersistence.REQUEST_ONLY
    )

    assembled = await RequestAssembler((request_only,), source_order=("skills",)).assemble(
        _request(budget_tokens=10, history=persisted)
    )

    assert assembled.history_appends == ()
    assert assembled.provider_history[0] == persisted[0]
    assert "candidate" in assembled.provider_history[-1].extract_text("")


@pytest.mark.asyncio
async def test_empty_fragment_set_preserves_request_and_has_zero_accounting() -> None:
    persisted = (Message(role="user", content="task"),)

    assembled = await RequestAssembler((), source_order=()).assemble(
        _request(budget_tokens=0, history=persisted)
    )

    assert assembled.system_prompt == "stable system prompt"
    assert assembled.provider_history == persisted
    assert assembled.history_appends == ()
    assert assembled.manifest.status is RequestStatus.SUCCEEDED
    assert assembled.manifest.outcomes == ()
    assert assembled.manifest.budgeted_admitted_tokens == 0
    assert assembled.manifest.non_budgeted_estimated_tokens == 0


@pytest.mark.asyncio
async def test_required_content_over_budget_raises_sanitized_categorized_error() -> None:
    secret = "credential=do-not-leak /Users/private/project"
    required = _fragment(
        "permission",
        secret * 10,
        source="permissions",
        requirement=FragmentRequirement.REQUIRED,
        truncatable=True,
    )

    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler((required,), source_order=("permissions",)).assemble(
            _request(budget_tokens=1)
        )

    error = caught.value
    assert error.reason_code == "required_content_exceeds_budget"
    assert error.manifest.status is RequestStatus.FAILED
    assert error.manifest.reason_code == error.reason_code
    assert error.manifest.outcomes[0].status is FragmentStatus.FAILED
    assert secret not in repr(error.manifest)
    assert "/Users/private" not in str(error)


@pytest.mark.asyncio
async def test_unsafe_fragment_identifiers_fail_without_entering_manifest() -> None:
    unsafe_source = "/Users/private/token=do-not-leak"
    fragment = _fragment("unsafe/key", "safe content", source=unsafe_source)

    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler((fragment,), source_order=(unsafe_source,)).assemble(
            _request(budget_tokens=10)
        )

    assert caught.value.reason_code == "unsafe_fragment_identifier"
    assert caught.value.manifest.outcomes == ()
    assert unsafe_source not in repr(caught.value.manifest)
    assert "unsafe/key" not in repr(caught.value.manifest)


@pytest.mark.asyncio
async def test_optional_empty_content_is_not_applicable() -> None:
    optional = _fragment("optional", "", source="optional")

    optional_result = await RequestAssembler((optional,), source_order=("optional",)).assemble(
        _request(budget_tokens=1)
    )
    assert optional_result.manifest.outcomes[0].status is FragmentStatus.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_required_empty_content_fails_with_categorized_error() -> None:
    required = _fragment(
        "required",
        "",
        source="required",
        requirement=FragmentRequirement.REQUIRED,
    )

    with pytest.raises(RequestAssemblyError, match="required_source_invalid"):
        await RequestAssembler((required,), source_order=("required",)).assemble(
            _request(budget_tokens=1)
        )
