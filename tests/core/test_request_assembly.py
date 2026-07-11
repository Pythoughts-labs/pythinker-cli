from __future__ import annotations

from collections.abc import Sequence

import pytest
from pythinker_core.message import Message

from pythinker_code.soul.request_assembly import (
    AGENTS_MD_SOURCE_POLICY,
    FragmentBudgetClass,
    FragmentPersistence,
    FragmentRequirement,
    FragmentStatus,
    FragmentTruncation,
    RequestAssembler,
    RequestAssemblyError,
    RequestAssemblyInput,
    RequestFragment,
    RequestSourceResult,
    RequestStatus,
    SourceApplicability,
    SourceResultStatus,
    TrustedSourcePolicy,
)


def _policy(
    source: str,
    key: str,
    *,
    requirement: FragmentRequirement = FragmentRequirement.BEST_EFFORT,
    persistence: FragmentPersistence = FragmentPersistence.HISTORY,
    priority: int = 100,
    budget_class: FragmentBudgetClass = FragmentBudgetClass.BUDGETED,
    truncation: FragmentTruncation = FragmentTruncation.FORBIDDEN,
    applicability: SourceApplicability = SourceApplicability.ALWAYS,
    failure_reason_codes: tuple[str, ...] = (),
) -> TrustedSourcePolicy:
    return TrustedSourcePolicy(
        source=source,
        key=key,
        requirement=requirement,
        persistence=persistence,
        priority=priority,
        budget_class=budget_class,
        truncation=truncation,
        applicability=applicability,
        failure_reason_codes=failure_reason_codes,
    )


def _provided(policy: TrustedSourcePolicy, content: str) -> RequestSourceResult:
    return RequestSourceResult(
        source=policy.source,
        key=policy.key,
        status=SourceResultStatus.PROVIDED,
        fragment=RequestFragment(
            key=policy.key,
            content=content,
            source=policy.source,
            requirement=policy.requirement,
            persistence=policy.persistence,
            priority=policy.priority,
            truncatable=policy.truncation is FragmentTruncation.ALLOWED,
        ),
        reason_code=None,
    )


def _failed(policy: TrustedSourcePolicy, reason_code: str) -> RequestSourceResult:
    return RequestSourceResult(
        source=policy.source,
        key=policy.key,
        status=SourceResultStatus.FAILED,
        fragment=None,
        reason_code=reason_code,
    )


def _not_applicable(policy: TrustedSourcePolicy) -> RequestSourceResult:
    return RequestSourceResult(
        source=policy.source,
        key=policy.key,
        status=SourceResultStatus.NOT_APPLICABLE,
        fragment=None,
        reason_code=None,
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
    required = _policy(
        "permissions",
        "permission",
        requirement=FragmentRequirement.REQUIRED,
        priority=1,
    )
    optional = _policy("plan", "plan", priority=1_000)

    assembled = await RequestAssembler(
        (required, optional), (_provided(optional, "o" * 20), _provided(required, "r" * 20))
    ).assemble(_request(budget_tokens=5))

    assert [outcome.key for outcome in assembled.manifest.outcomes] == ["permission", "plan"]
    assert [outcome.status for outcome in assembled.manifest.outcomes] == [
        FragmentStatus.INCLUDED,
        FragmentStatus.OMITTED_BUDGET,
    ]
    assert assembled.manifest.budgeted_admitted_tokens == 5


@pytest.mark.asyncio
async def test_agents_preamble_is_required_visible_and_non_budgeted() -> None:
    plan = _policy("plan", "plan")
    assembled = await RequestAssembler(
        (AGENTS_MD_SOURCE_POLICY, plan),
        (_provided(plan, "p" * 20), _provided(AGENTS_MD_SOURCE_POLICY, "a" * 40)),
    ).assemble(_request(budget_tokens=5))

    assert assembled.manifest.status is RequestStatus.SUCCEEDED
    assert assembled.manifest.budgeted_admitted_tokens == 5
    assert assembled.manifest.non_budgeted_estimated_tokens == 10
    assert [outcome.admitted_tokens for outcome in assembled.manifest.outcomes] == [10, 5]
    assert len(assembled.history_appends) == 1
    assert "p" * 20 in assembled.history_appends[0].extract_text("")
    assert "a" * 40 in assembled.provider_history[-1].extract_text("")


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_tokens", [0, 5])
async def test_non_budgeted_agents_and_exact_required_budget_succeed(
    budget_tokens: int,
) -> None:
    policies = [AGENTS_MD_SOURCE_POLICY]
    source_results = [_provided(AGENTS_MD_SOURCE_POLICY, "a" * 20)]
    if budget_tokens:
        permission = _policy(
            "permissions",
            "permission",
            requirement=FragmentRequirement.REQUIRED,
        )
        policies.append(permission)
        source_results.append(_provided(permission, "p" * 20))

    assembled = await RequestAssembler(tuple(policies), tuple(source_results)).assemble(
        _request(budget_tokens=budget_tokens)
    )

    assert assembled.manifest.status is RequestStatus.SUCCEEDED
    assert assembled.manifest.budgeted_admitted_tokens == budget_tokens
    assert assembled.manifest.non_budgeted_estimated_tokens == 5


@pytest.mark.asyncio
async def test_negative_budget_raises_categorized_error() -> None:
    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler((), ()).assemble(_request(budget_tokens=-1))

    assert caught.value.reason_code == "invalid_budget"
    assert caught.value.manifest.status is RequestStatus.FAILED
    assert caught.value.manifest.reason_code == caught.value.reason_code


@pytest.mark.asyncio
async def test_optional_source_failure_degrades_without_prompt_content() -> None:
    plan = _policy("plan", "plan", failure_reason_codes=("plan_unavailable",))

    assembled = await RequestAssembler((plan,), (_failed(plan, "plan_unavailable"),)).assemble(
        _request(budget_tokens=10)
    )

    assert assembled.provider_history == ()
    assert assembled.history_appends == ()
    assert assembled.manifest.status is RequestStatus.DEGRADED
    assert assembled.manifest.outcomes[0].status is FragmentStatus.DEGRADED
    assert assembled.manifest.outcomes[0].reason_code == "plan_unavailable"


@pytest.mark.asyncio
async def test_required_source_failure_raises_with_matching_safe_reason() -> None:
    permission = _policy(
        "permissions",
        "permission",
        requirement=FragmentRequirement.REQUIRED,
        failure_reason_codes=("permission_state_unavailable",),
    )

    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler(
            (permission,), (_failed(permission, "permission_state_unavailable"),)
        ).assemble(_request(budget_tokens=10))

    assert caught.value.reason_code == "permission_state_unavailable"
    assert caught.value.manifest.reason_code == caught.value.reason_code
    assert caught.value.manifest.outcomes[0].status is FragmentStatus.FAILED


@pytest.mark.asyncio
async def test_not_applicable_source_is_successful_and_visible() -> None:
    defense = _policy(
        "model_defense",
        "model_defense",
        requirement=FragmentRequirement.REQUIRED,
        applicability=SourceApplicability.MAY_BE_NOT_APPLICABLE,
    )

    assembled = await RequestAssembler((defense,), (_not_applicable(defense),)).assemble(
        _request(budget_tokens=0)
    )

    assert assembled.manifest.status is RequestStatus.SUCCEEDED
    assert assembled.manifest.outcomes[0].status is FragmentStatus.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_required_source_cannot_claim_not_applicable_without_policy_permission() -> None:
    permission = _policy("permissions", "permission", requirement=FragmentRequirement.REQUIRED)

    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler((permission,), (_not_applicable(permission),)).assemble(
            _request(budget_tokens=0)
        )

    assert caught.value.reason_code == "source_policy_mismatch"
    assert caught.value.manifest.outcomes == ()


@pytest.mark.asyncio
async def test_equal_priority_sources_follow_policy_order_for_all_result_permutations() -> None:
    plan = _policy("plan", "plan", persistence=FragmentPersistence.REQUEST_ONLY, priority=100)
    git = _policy("git", "git", persistence=FragmentPersistence.REQUEST_ONLY, priority=100)
    plan_result = _provided(plan, "plan")
    git_result = _provided(git, "git")

    first = await RequestAssembler((plan, git), (git_result, plan_result)).assemble(
        _request(budget_tokens=10)
    )
    second = await RequestAssembler((plan, git), (plan_result, git_result)).assemble(
        _request(budget_tokens=10)
    )

    assert [outcome.key for outcome in first.manifest.outcomes] == ["plan", "git"]
    assert first.provider_history == second.provider_history


@pytest.mark.asyncio
async def test_equal_priority_same_source_keys_follow_policy_key_rank() -> None:
    alpha = _policy("reminders", "alpha", persistence=FragmentPersistence.REQUEST_ONLY)
    beta = _policy("reminders", "beta", persistence=FragmentPersistence.REQUEST_ONLY)
    alpha_result = _provided(alpha, "alpha")
    beta_result = _provided(beta, "beta")

    first = await RequestAssembler((beta, alpha), (alpha_result, beta_result)).assemble(
        _request(budget_tokens=10)
    )
    second = await RequestAssembler((alpha, beta), (beta_result, alpha_result)).assemble(
        _request(budget_tokens=10)
    )

    assert [outcome.key for outcome in first.manifest.outcomes] == ["alpha", "beta"]
    assert first.provider_history == second.provider_history


@pytest.mark.asyncio
async def test_oversized_optional_fragment_is_truncated_only_when_policy_allows() -> None:
    fixed = _policy("fixed", "fixed")
    flexible = _policy("flexible", "flexible", truncation=FragmentTruncation.ALLOWED)

    assembled = await RequestAssembler(
        (fixed, flexible),
        (_provided(fixed, "f" * 40), _provided(flexible, "alpha\nbeta\ngamma" * 10)),
    ).assemble(_request(budget_tokens=5))

    outcomes = {outcome.key: outcome for outcome in assembled.manifest.outcomes}
    assert outcomes["fixed"].status is FragmentStatus.OMITTED_BUDGET
    assert outcomes["flexible"].status is FragmentStatus.TRUNCATED
    assert outcomes["flexible"].admitted_tokens <= 5


@pytest.mark.asyncio
async def test_unicode_truncation_keeps_complete_codepoints_and_line_boundary() -> None:
    flexible = _policy("flexible", "unicode", truncation=FragmentTruncation.ALLOWED)

    assembled = await RequestAssembler(
        (flexible,), (_provided(flexible, "alpha🙂beta\nsecond🙂line"),)
    ).assemble(_request(budget_tokens=3))

    rendered = assembled.provider_history[-1].extract_text("")
    assert "alpha🙂beta\n…" in rendered
    assert "second" not in rendered
    assert "�" not in rendered
    assert assembled.manifest.outcomes[0].admitted_tokens == 3


@pytest.mark.asyncio
async def test_request_only_fragment_never_appears_in_history_appends() -> None:
    persisted = (Message(role="assistant", content="previous"),)
    skills = _policy("skills", "skills", persistence=FragmentPersistence.REQUEST_ONLY)

    assembled = await RequestAssembler((skills,), (_provided(skills, "candidate"),)).assemble(
        _request(budget_tokens=10, history=persisted)
    )

    assert assembled.history_appends == ()
    assert assembled.provider_history[0] == persisted[0]
    assert "candidate" in assembled.provider_history[-1].extract_text("")


@pytest.mark.asyncio
async def test_empty_source_registry_preserves_request_and_zero_accounting() -> None:
    persisted = (Message(role="user", content="task"),)

    assembled = await RequestAssembler((), ()).assemble(
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
    permission = _policy(
        "permissions",
        "permission",
        requirement=FragmentRequirement.REQUIRED,
        truncation=FragmentTruncation.ALLOWED,
    )
    secret = "credential=do-not-leak /Users/private/project"

    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler((permission,), (_provided(permission, secret * 10),)).assemble(
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
async def test_failed_manifest_counts_observed_non_budgeted_agents() -> None:
    permission = _policy("permissions", "permission", requirement=FragmentRequirement.REQUIRED)

    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler(
            (AGENTS_MD_SOURCE_POLICY, permission),
            (
                _provided(AGENTS_MD_SOURCE_POLICY, "a" * 40),
                _provided(permission, "p" * 40),
            ),
        ).assemble(_request(budget_tokens=5))

    assert caught.value.manifest.non_budgeted_estimated_tokens == 10
    agents_outcome = caught.value.manifest.outcomes[0]
    assert agents_outcome.source == "agents_md"
    assert agents_outcome.admitted_tokens == 10


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spoofed_policy", "reason_code"),
    [
        (
            _policy(
                "agents_md",
                "agents_preamble",
                requirement=FragmentRequirement.REQUIRED,
                persistence=FragmentPersistence.HISTORY,
                budget_class=FragmentBudgetClass.NON_BUDGETED,
            ),
            "invalid_agents_policy",
        ),
        (
            _policy(
                "agents_md",
                "agents_preamble",
                requirement=FragmentRequirement.BEST_EFFORT,
                persistence=FragmentPersistence.REQUEST_ONLY,
                budget_class=FragmentBudgetClass.NON_BUDGETED,
            ),
            "invalid_agents_policy",
        ),
    ],
)
async def test_agents_policy_rejects_history_and_requirement_spoofs(
    spoofed_policy: TrustedSourcePolicy, reason_code: str
) -> None:
    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler((spoofed_policy,), (_provided(spoofed_policy, "content"),)).assemble(
            _request(budget_tokens=10)
        )

    assert caught.value.reason_code == reason_code
    assert caught.value.manifest.outcomes == ()


@pytest.mark.asyncio
async def test_agents_fragment_cannot_override_request_only_persistence() -> None:
    mismatched = RequestSourceResult(
        source=AGENTS_MD_SOURCE_POLICY.source,
        key=AGENTS_MD_SOURCE_POLICY.key,
        status=SourceResultStatus.PROVIDED,
        fragment=RequestFragment(
            key=AGENTS_MD_SOURCE_POLICY.key,
            content="agents content",
            source=AGENTS_MD_SOURCE_POLICY.source,
            requirement=FragmentRequirement.REQUIRED,
            persistence=FragmentPersistence.HISTORY,
            priority=AGENTS_MD_SOURCE_POLICY.priority,
            truncatable=False,
        ),
        reason_code=None,
    )

    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler((AGENTS_MD_SOURCE_POLICY,), (mismatched,)).assemble(
            _request(budget_tokens=0)
        )

    assert caught.value.reason_code == "source_policy_mismatch"
    assert caught.value.manifest.outcomes == ()


@pytest.mark.asyncio
async def test_only_agents_policy_can_be_non_budgeted() -> None:
    spoofed = _policy(
        "plan",
        "plan",
        budget_class=FragmentBudgetClass.NON_BUDGETED,
    )

    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler((spoofed,), (_provided(spoofed, "content"),)).assemble(
            _request(budget_tokens=0)
        )

    assert caught.value.reason_code == "invalid_non_budgeted_policy"
    assert caught.value.manifest.outcomes == ()


@pytest.mark.asyncio
async def test_unknown_credential_shaped_source_is_rejected_before_manifest() -> None:
    plan = _policy("plan", "plan")
    credential_source = "api_token_abcd1234"
    unknown = RequestSourceResult(
        source=credential_source,
        key="user_alice_request",
        status=SourceResultStatus.NOT_APPLICABLE,
        fragment=None,
        reason_code=None,
    )

    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler((plan,), (unknown,)).assemble(_request(budget_tokens=10))

    assert caught.value.reason_code == "unknown_source_result"
    assert caught.value.manifest.outcomes == ()
    assert credential_source not in repr(caught.value.manifest)
    assert "user_alice_request" not in repr(caught.value.manifest)


@pytest.mark.asyncio
async def test_fragment_metadata_must_match_trusted_policy() -> None:
    plan = _policy("plan", "plan", persistence=FragmentPersistence.REQUEST_ONLY)
    mismatched = RequestSourceResult(
        source="plan",
        key="plan",
        status=SourceResultStatus.PROVIDED,
        fragment=RequestFragment(
            key="plan",
            content="content",
            source="plan",
            requirement=FragmentRequirement.REQUIRED,
            persistence=FragmentPersistence.HISTORY,
            priority=plan.priority,
            truncatable=True,
        ),
        reason_code=None,
    )

    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler((plan,), (mismatched,)).assemble(_request(budget_tokens=10))

    assert caught.value.reason_code == "source_policy_mismatch"
    assert caught.value.manifest.outcomes == ()


@pytest.mark.asyncio
async def test_normalization_failure_is_categorized_and_preserves_cause() -> None:
    plan = _policy("plan", "plan")
    failure = RuntimeError("normalizer details must not surface")

    def fail_normalization(_history: Sequence[Message]) -> list[Message]:
        raise failure

    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler(
            (plan,), (_provided(plan, "content"),), history_normalizer=fail_normalization
        ).assemble(_request(budget_tokens=10))

    assert caught.value.reason_code == "history_normalization_failed"
    assert caught.value.manifest.status is RequestStatus.FAILED
    assert caught.value.manifest.reason_code == caught.value.reason_code
    assert caught.value.__cause__ is failure
    assert "normalizer details" not in str(caught.value)


@pytest.mark.asyncio
async def test_duplicate_policy_is_categorized_as_internal_invariant_failure() -> None:
    plan = _policy("plan", "plan")

    with pytest.raises(RequestAssemblyError) as caught:
        await RequestAssembler((plan, plan), (_provided(plan, "content"),)).assemble(
            _request(budget_tokens=10)
        )

    assert caught.value.reason_code == "internal_invariant_violation"
    assert caught.value.manifest.status is RequestStatus.FAILED
