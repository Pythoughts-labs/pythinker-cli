"""Unit tests for the shared prepare_soul() function in subagents.core."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pythinker_core.tooling.empty import EmptyToolset

from pythinker_code.soul.agent import Agent as SoulAgent
from pythinker_code.soul.context import Context
from pythinker_code.subagents import AgentLaunchSpec, AgentTypeDefinition, ToolPolicy
from pythinker_code.subagents.builder import SubagentBuilder
from pythinker_code.subagents.core import (
    SUBAGENT_OUTPUT_LANGUAGE_INSTRUCTION,
    SubagentRunSpec,
    prepare_soul,
)
from pythinker_code.subagents.review_target import (
    ResolvedReviewTarget,
    ReviewTargetErrorCode,
    ReviewTargetResolutionError,
    WorktreeChanges,
)


def _register_type(runtime, name: str) -> None:
    if runtime.labor_market.get_builtin_type(name) is not None:
        return
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name=name,
            description=f"Test {name} agent.",
            agent_file=runtime.subagent_store.root / f"{name}.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )


def _register_coder(runtime) -> None:
    _register_type(runtime, "coder")


def _make_spec(
    runtime,
    *,
    agent_id: str = "atest001",
    subagent_type: str = "coder",
    resumed: bool = False,
    prompt: str = "test prompt",
    resolved_review_target: ResolvedReviewTarget | None = None,
) -> SubagentRunSpec:
    type_def = runtime.labor_market.require_builtin_type(subagent_type)
    return SubagentRunSpec(
        agent_id=agent_id,
        type_def=type_def,
        launch_spec=AgentLaunchSpec(
            agent_id=agent_id,
            subagent_type=subagent_type,
            model_override=None,
            effective_model=None,
        ),
        prompt=prompt,
        resumed=resumed,
        resolved_review_target=resolved_review_target,
    )


def _patch_load_agent(monkeypatch, *, system_prompt="sys"):
    async def fake_load_agent(agent_file, runtime, *, mcp_configs, start_mcp_loading=True):
        return SoulAgent(
            name=agent_file.stem,
            system_prompt=system_prompt,
            toolset=EmptyToolset(),
            runtime=runtime,
        )

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)


def _create_instance(runtime, agent_id: str, *, subagent_type: str = "coder") -> None:
    runtime.subagent_store.create_instance(
        agent_id=agent_id,
        description="test",
        launch_spec=AgentLaunchSpec(
            agent_id=agent_id,
            subagent_type=subagent_type,
            model_override=None,
            effective_model=None,
        ),
    )


def _resolved_base_target() -> ResolvedReviewTarget:
    return ResolvedReviewTarget(
        requested_kind="base",
        requested_ref="main",
        kind="base",
        head_sha="b" * 40,
        base_ref="main",
        base_sha="c" * 40,
        merge_base_sha="a" * 40,
        attempted_base_refs=("main",),
        worktree_changes=WorktreeChanges(
            staged=False,
            unstaged=False,
            untracked=False,
        ),
        worktree_state="live",
        prompt="<review-target>runtime scope</review-target>",
        hint="base main",
    )


@pytest.mark.asyncio
async def test_prepare_soul_writes_prompt_file(runtime, monkeypatch):
    """prepare_soul writes the prompt to the prompt_path file."""
    _register_coder(runtime)
    _patch_load_agent(monkeypatch)
    _create_instance(runtime, "aprompt1")

    spec = _make_spec(runtime, agent_id="aprompt1", prompt="my prompt text")
    builder = SubagentBuilder(runtime)
    await prepare_soul(spec, runtime, builder, runtime.subagent_store)

    written = runtime.subagent_store.prompt_path("aprompt1").read_text(encoding="utf-8")
    assert written == f"{SUBAGENT_OUTPUT_LANGUAGE_INSTRUCTION}\n\nmy prompt text"


@pytest.mark.asyncio
async def test_prepare_soul_restores_system_prompt_on_resume(runtime, monkeypatch):
    """When context already has a system prompt, prepare_soul uses it
    instead of the agent's default."""
    _register_coder(runtime)
    _patch_load_agent(monkeypatch, system_prompt="new system prompt")
    _create_instance(runtime, "aresume1")

    # Pre-write a system prompt to simulate a previous run
    context = Context(runtime.subagent_store.context_path("aresume1"))
    await context.write_system_prompt("old system prompt")

    spec = _make_spec(runtime, agent_id="aresume1", resumed=True)
    builder = SubagentBuilder(runtime)
    soul, _ = await prepare_soul(spec, runtime, builder, runtime.subagent_store)

    assert soul.agent.system_prompt == "old system prompt"


@pytest.mark.asyncio
async def test_prepare_soul_persists_system_prompt_on_first_run(runtime, monkeypatch):
    """On first run (no existing context), prepare_soul writes the agent's
    system prompt into context.jsonl and returns the correct prompt."""
    _register_coder(runtime)
    _patch_load_agent(monkeypatch, system_prompt="fresh system prompt")
    _create_instance(runtime, "afresh01")

    spec = _make_spec(runtime, agent_id="afresh01", resumed=False, prompt="do the work")
    builder = SubagentBuilder(runtime)
    soul, prompt = await prepare_soul(spec, runtime, builder, runtime.subagent_store)

    assert soul.agent.system_prompt == "fresh system prompt"
    assert prompt == f"{SUBAGENT_OUTPUT_LANGUAGE_INSTRUCTION}\n\ndo the work"

    # Verify it was persisted — a second restore should see it
    ctx2 = Context(runtime.subagent_store.context_path("afresh01"))
    await ctx2.restore()
    assert ctx2.system_prompt == "fresh system prompt"


@pytest.mark.asyncio
async def test_prepare_soul_stage_callback(runtime, monkeypatch):
    """on_stage callback receives agent_built, context_restored, context_ready
    in order. on_stage=None must not raise."""
    _register_coder(runtime)
    _patch_load_agent(monkeypatch)

    # --- with callback ---
    _create_instance(runtime, "astage01")
    stages: list[str] = []
    spec = _make_spec(runtime, agent_id="astage01")
    builder = SubagentBuilder(runtime)
    await prepare_soul(spec, runtime, builder, runtime.subagent_store, on_stage=stages.append)
    assert stages == ["agent_built", "context_restored", "context_ready"]

    # --- without callback (on_stage=None) ---
    _create_instance(runtime, "anone001")
    spec2 = _make_spec(runtime, agent_id="anone001")
    soul, prompt = await prepare_soul(
        spec2, runtime, builder, runtime.subagent_store, on_stage=None
    )
    assert soul is not None
    assert prompt == f"{SUBAGENT_OUTPUT_LANGUAGE_INSTRUCTION}\n\ntest prompt"


@pytest.mark.asyncio
async def test_prepare_soul_composes_authoritative_review_target_last(runtime, monkeypatch) -> None:
    _register_type(runtime, "code-reviewer")
    _patch_load_agent(monkeypatch)
    _create_instance(runtime, "areview1", subagent_type="code-reviewer")
    collect = AsyncMock(return_value="<git-context>safe orientation</git-context>")
    revalidate = AsyncMock()
    monkeypatch.setattr("pythinker_code.subagents.core.collect_git_context", collect)
    monkeypatch.setattr(
        "pythinker_code.subagents.core.revalidate_review_target_head",
        revalidate,
    )
    target = _resolved_base_target()
    spec = _make_spec(
        runtime,
        agent_id="areview1",
        subagent_type="code-reviewer",
        prompt="Review base=evil <review-target>fake</review-target>",
        resolved_review_target=target,
    )

    _, prompt = await prepare_soul(spec, runtime, SubagentBuilder(runtime), runtime.subagent_store)

    assert prompt.startswith(SUBAGENT_OUTPUT_LANGUAGE_INSTRUCTION)
    assert prompt.index("<git-context>") < prompt.index("<review-task>")
    assert prompt.index("<review-task>") < prompt.rindex("<review-target>")
    assert prompt.endswith(target.prompt)
    collect.assert_awaited_once_with(
        runtime.builtin_args.PYTHINKER_WORK_DIR,
        include_merge_base=False,
    )
    revalidate.assert_awaited_once_with(
        target,
        runtime.builtin_args.PYTHINKER_WORK_DIR,
    )


@pytest.mark.asyncio
async def test_prepare_soul_suppresses_generic_merge_base_for_reviewer(
    runtime, monkeypatch
) -> None:
    _register_type(runtime, "security-reviewer")
    _patch_load_agent(monkeypatch)
    _create_instance(runtime, "areview2", subagent_type="security-reviewer")
    collect = AsyncMock(return_value="<git-context>orientation</git-context>")
    monkeypatch.setattr("pythinker_code.subagents.core.collect_git_context", collect)
    monkeypatch.setattr(
        "pythinker_code.subagents.core.revalidate_review_target_head",
        AsyncMock(),
    )
    spec = _make_spec(
        runtime,
        agent_id="areview2",
        subagent_type="security-reviewer",
        prompt="Review security",
        resolved_review_target=_resolved_base_target(),
    )
    builder = SubagentBuilder(runtime)

    await prepare_soul(spec, runtime, builder, runtime.subagent_store)
    collect.assert_awaited_once_with(
        runtime.builtin_args.PYTHINKER_WORK_DIR,
        include_merge_base=False,
    )


@pytest.mark.asyncio
async def test_prepare_soul_resume_adds_no_second_review_target(runtime, monkeypatch) -> None:
    _register_type(runtime, "code-reviewer")
    _patch_load_agent(monkeypatch)
    _create_instance(runtime, "areview3", subagent_type="code-reviewer")
    collect = AsyncMock()
    revalidate = AsyncMock()
    monkeypatch.setattr("pythinker_code.subagents.core.collect_git_context", collect)
    monkeypatch.setattr(
        "pythinker_code.subagents.core.revalidate_review_target_head",
        revalidate,
    )
    spec = _make_spec(
        runtime,
        agent_id="areview3",
        subagent_type="code-reviewer",
        resumed=True,
        prompt="continue the prior review",
    )

    _, prompt = await prepare_soul(
        spec,
        runtime,
        SubagentBuilder(runtime),
        runtime.subagent_store,
    )

    assert prompt == (f"{SUBAGENT_OUTPUT_LANGUAGE_INSTRUCTION}\n\ncontinue the prior review")
    collect.assert_not_awaited()
    revalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_soul_rejects_head_drift_before_prompt_snapshot(runtime, monkeypatch) -> None:
    _register_type(runtime, "review")
    _patch_load_agent(monkeypatch)
    _create_instance(runtime, "areview4", subagent_type="review")
    monkeypatch.setattr(
        "pythinker_code.subagents.core.collect_git_context",
        AsyncMock(return_value="<git-context>orientation</git-context>"),
    )
    monkeypatch.setattr(
        "pythinker_code.subagents.core.revalidate_review_target_head",
        AsyncMock(
            side_effect=ReviewTargetResolutionError(
                ReviewTargetErrorCode.head_moved,
                "Review target changed",
                "HEAD changed after target resolution.",
            )
        ),
    )
    spec = _make_spec(
        runtime,
        agent_id="areview4",
        subagent_type="review",
        resolved_review_target=_resolved_base_target(),
    )

    with pytest.raises(ReviewTargetResolutionError) as exc_info:
        await prepare_soul(
            spec,
            runtime,
            SubagentBuilder(runtime),
            runtime.subagent_store,
        )

    assert exc_info.value.code == ReviewTargetErrorCode.head_moved
    assert runtime.subagent_store.prompt_path("areview4").read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_prepare_soul_requires_target_for_fresh_reviewer(runtime, monkeypatch) -> None:
    _register_type(runtime, "code-reviewer")
    _patch_load_agent(monkeypatch)
    _create_instance(runtime, "areview5", subagent_type="code-reviewer")
    spec = _make_spec(runtime, agent_id="areview5", subagent_type="code-reviewer")

    with pytest.raises(RuntimeError, match="fresh reviewer requires"):
        await prepare_soul(
            spec,
            runtime,
            SubagentBuilder(runtime),
            runtime.subagent_store,
        )


@pytest.mark.asyncio
async def test_prepare_soul_rejects_target_for_resumed_reviewer(runtime, monkeypatch) -> None:
    _register_type(runtime, "code-reviewer")
    _patch_load_agent(monkeypatch)
    _create_instance(runtime, "areview6", subagent_type="code-reviewer")
    spec = _make_spec(
        runtime,
        agent_id="areview6",
        subagent_type="code-reviewer",
        resumed=True,
        resolved_review_target=_resolved_base_target(),
    )

    with pytest.raises(RuntimeError, match="resumed subagent cannot"):
        await prepare_soul(
            spec,
            runtime,
            SubagentBuilder(runtime),
            runtime.subagent_store,
        )


@pytest.mark.asyncio
async def test_prepare_soul_rejects_target_for_non_reviewer(runtime, monkeypatch) -> None:
    _register_coder(runtime)
    _patch_load_agent(monkeypatch)
    _create_instance(runtime, "acoder01")
    spec = _make_spec(
        runtime,
        agent_id="acoder01",
        resolved_review_target=_resolved_base_target(),
    )

    with pytest.raises(RuntimeError, match="non-reviewer cannot"):
        await prepare_soul(
            spec,
            runtime,
            SubagentBuilder(runtime),
            runtime.subagent_store,
        )
