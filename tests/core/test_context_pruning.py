"""Graduated stale-tool-output pruning (ctxmgmt-2).

A cheap, fidelity-preserving tier between "do nothing" and full LLM
summarization: replace large *completed* tool-result bodies in deep history with
a short placeholder, preserving conversational structure and tool_call_id
pairing. Recent messages are protected; small outputs are left alone.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from pythinker_core.message import Message, TextPart

from pythinker_code.soul.compaction import (
    PRUNE_PLACEHOLDER,
    prune_stale_tool_outputs,
    should_prune,
)


def _tool(text: str, call_id: str) -> Message:
    return Message(role="tool", content=text, tool_call_id=call_id)


def test_prunes_large_stale_tool_output() -> None:
    big = "x" * 5000
    history = [
        Message(role="user", content="do it"),
        Message(role="assistant", content=[TextPart(text="ok")]),
        _tool(big, "c1"),
    ] + [
        Message(role="user", content=f"m{i}") for i in range(30)
    ]  # push the tool into deep history

    pruned, freed = prune_stale_tool_outputs(history, protect_last=10, min_chars=2000)

    assert freed == len(big)
    # The tool message body is replaced by a placeholder; structure preserved.
    tool_msg = pruned[2]
    assert tool_msg.role == "tool"
    assert tool_msg.tool_call_id == "c1"
    assert tool_msg.extract_text("") == PRUNE_PLACEHOLDER.format(n=len(big))
    # Same number of messages — nothing dropped.
    assert len(pruned) == len(history)


def test_protects_recent_tool_outputs() -> None:
    big = "y" * 5000
    history = [Message(role="user", content="hi"), _tool(big, "recent")]

    pruned, freed = prune_stale_tool_outputs(history, protect_last=10, min_chars=2000)

    assert freed == 0
    assert pruned[1].extract_text("") == big  # untouched


def test_skips_small_tool_outputs() -> None:
    small = "z" * 100
    history = [_tool(small, "c1")] + [Message(role="user", content=f"m{i}") for i in range(30)]

    pruned, freed = prune_stale_tool_outputs(history, protect_last=5, min_chars=2000)

    assert freed == 0
    assert pruned[0].extract_text("") == small


def test_only_tool_messages_are_pruned() -> None:
    big_assistant = Message(role="assistant", content=[TextPart(text="a" * 5000)])
    history = [big_assistant] + [Message(role="user", content=f"m{i}") for i in range(30)]

    pruned, freed = prune_stale_tool_outputs(history, protect_last=5, min_chars=2000)

    assert freed == 0
    assert pruned[0].extract_text("") == "a" * 5000


def test_should_prune_threshold() -> None:
    assert should_prune(710, 1000, ratio=0.7) is True
    assert should_prune(690, 1000, ratio=0.7) is False
    assert should_prune(999, 1000, ratio=0.0) is True  # ratio 0 still fires once any usage


# ── prune_context integration: rewrites the persisted context safely ──

import pytest  # noqa: E402
from pythinker_core.tooling.simple import SimpleToolset  # noqa: E402

from pythinker_code.soul.agent import Agent, Runtime  # noqa: E402
from pythinker_code.soul.context import Context, ContextGenerationConflictError  # noqa: E402
from pythinker_code.soul.pythinkersoul import PythinkerSoul  # noqa: E402


def _make_soul(runtime: Runtime, tmp_path) -> tuple[Context, PythinkerSoul]:
    # prune_context performs no LLM call, so the fixture runtime is used as-is.
    agent = Agent(name="Prune", system_prompt="sys", toolset=SimpleToolset(), runtime=runtime)
    context = Context(file_backend=tmp_path / "history.jsonl")
    return context, PythinkerSoul(agent, context=context)


@pytest.mark.asyncio
async def test_prune_context_rewrites_history_preserving_structure(runtime, tmp_path) -> None:
    runtime.config.loop_control.prune_protect_last = 2
    runtime.config.loop_control.prune_min_chars = 2000
    context, soul = _make_soul(runtime, tmp_path)
    await context.write_system_prompt("sys")
    await context.append_message(
        [
            Message(role="user", content="go"),
            Message(role="assistant", content=[TextPart(text="working")]),
            Message(role="tool", content="x" * 6000, tool_call_id="c1"),
            Message(role="user", content="more"),
            Message(role="assistant", content=[TextPart(text="done")]),
        ]
    )
    legacy_clear = AsyncMock(side_effect=AssertionError("legacy clear path used"))
    legacy_write_prompt = AsyncMock(side_effect=AssertionError("legacy prompt write used"))
    legacy_checkpoint = AsyncMock(side_effect=AssertionError("legacy checkpoint path used"))
    legacy_append = AsyncMock(side_effect=AssertionError("legacy rebuild append used"))
    legacy_usage = AsyncMock(side_effect=AssertionError("legacy usage rollback used"))
    context.clear = legacy_clear  # type: ignore[method-assign]
    context.write_system_prompt = legacy_write_prompt  # type: ignore[method-assign]
    context.checkpoint = legacy_checkpoint  # type: ignore[method-assign]
    context.append_message = legacy_append  # type: ignore[method-assign]
    context.update_token_count = legacy_usage  # type: ignore[method-assign]

    did_prune = await soul.prune_context()

    assert did_prune is True
    history = soul.context.history
    assert len(history) == 5  # nothing dropped
    tool_msgs = [m for m in history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "c1"  # pairing preserved
    assert "elided" in tool_msgs[0].extract_text("")  # body replaced
    # Recent + non-tool messages untouched.
    assert history[-1].extract_text("") == "done"
    legacy_clear.assert_not_awaited()
    legacy_write_prompt.assert_not_awaited()
    legacy_checkpoint.assert_not_awaited()
    legacy_append.assert_not_awaited()
    legacy_usage.assert_not_awaited()


def _seed_prunable() -> list[Message]:
    return [
        Message(role="user", content="go"),
        Message(role="assistant", content=[TextPart(text="working")]),
        Message(role="tool", content="x" * 6000, tool_call_id="c1"),
        Message(role="user", content="more"),
        Message(role="assistant", content=[TextPart(text="done")]),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("disk full"), asyncio.CancelledError()])
async def test_prune_context_replacement_failure_preserves_generation(
    runtime, tmp_path, error: BaseException
) -> None:
    runtime.config.loop_control.prune_protect_last = 2
    runtime.config.loop_control.prune_min_chars = 2000
    context, soul = _make_soul(runtime, tmp_path)
    await context.write_system_prompt("sys")
    await context.append_message(_seed_prunable())
    await context.update_token_count(91)
    before_bytes = context.file_backend.read_bytes()
    before_memory = (
        tuple(context.history),
        context.system_prompt,
        context.token_count,
        context.token_count_with_pending,
        context.n_checkpoints,
    )
    replace_history = AsyncMock(side_effect=error)
    context.replace_history = replace_history  # type: ignore[method-assign]

    with pytest.raises(type(error)):
        await soul.prune_context()

    replace_history.assert_awaited_once()
    assert context.file_backend.read_bytes() == before_bytes
    assert (
        tuple(context.history),
        context.system_prompt,
        context.token_count,
        context.token_count_with_pending,
        context.n_checkpoints,
    ) == before_memory


@pytest.mark.asyncio
async def test_prune_context_does_not_rearm_injection_providers(runtime, tmp_path) -> None:
    """Prune preserves prior injected reminders (they are user messages, not tool bodies),
    so it must NOT reset providers' one-shot state — re-arming re-emits duplicate fragments."""
    from pythinker_code.soul.dynamic_injection import DynamicInjectionProvider

    runtime.config.loop_control.prune_protect_last = 2
    runtime.config.loop_control.prune_min_chars = 2000
    context, soul = _make_soul(runtime, tmp_path)

    class _SpyProvider(DynamicInjectionProvider):
        def __init__(self) -> None:
            self.compacted = 0

        async def get_injections(self, history, soul):  # noqa: ANN001
            return []

        async def on_context_compacted(self) -> None:
            self.compacted += 1

    spy = _SpyProvider()
    soul.add_injection_provider(spy)

    await context.write_system_prompt("sys")
    await context.append_message(_seed_prunable())

    assert await soul.prune_context() is True
    assert spy.compacted == 0  # prune is not compaction; one-shot state must survive


@pytest.mark.asyncio
async def test_prune_context_never_increases_token_count(runtime, tmp_path) -> None:
    """Pruning only removes content, so the post-prune token count must never exceed the
    pre-prune authoritative count. A full heuristic re-estimate could over-count the
    remaining content, keep the context over the prune trigger, and re-fire the whole
    rewrite every step — anchor to the authoritative count minus the freed delta instead."""
    runtime.config.loop_control.prune_protect_last = 2
    runtime.config.loop_control.prune_min_chars = 2000
    context, soul = _make_soul(runtime, tmp_path)
    await context.write_system_prompt("sys")
    await context.append_message(_seed_prunable())
    # Authoritative pre-prune count (from the LLM) below the heuristic estimate of the
    # remaining content — the case where a naive full re-estimate would grow the count.
    before = 1
    await context.update_token_count(before)

    assert await soul.prune_context() is True
    assert context.token_count <= before


@pytest.mark.asyncio
async def test_prune_context_noop_when_nothing_stale(runtime, tmp_path) -> None:
    runtime.config.loop_control.prune_protect_last = 20
    runtime.config.loop_control.prune_min_chars = 2000
    context, soul = _make_soul(runtime, tmp_path)
    await context.write_system_prompt("sys")
    await context.append_message(
        [
            Message(role="user", content="hi"),
            Message(role="tool", content="small", tool_call_id="c1"),
        ]
    )

    did_prune = await soul.prune_context()

    assert did_prune is False  # protected + small → nothing to prune
    assert soul.context.history[-1].extract_text("") == "small"


@pytest.mark.asyncio
async def test_prune_context_rejects_stale_replacement_after_concurrent_append(
    runtime, tmp_path
) -> None:
    runtime.config.loop_control.prune_protect_last = 2
    runtime.config.loop_control.prune_min_chars = 2000
    context, soul = _make_soul(runtime, tmp_path)
    await context.write_system_prompt("sys")
    await context.append_message(_seed_prunable())
    replacement_entered = asyncio.Event()
    release_replacement = asyncio.Event()
    real_replace = context.replace_history

    async def delayed_replace(replacement, **kwargs):  # noqa: ANN001, ANN003
        replacement_entered.set()
        await release_replacement.wait()
        return await real_replace(replacement, **kwargs)

    context.replace_history = delayed_replace  # type: ignore[method-assign]
    prune = asyncio.create_task(soul.prune_context())
    await replacement_entered.wait()
    concurrent = Message(role="user", content="concurrent append wins")
    await context.append_message(concurrent)
    release_replacement.set()

    with pytest.raises(ContextGenerationConflictError):
        await prune
    assert context.history[-1] == concurrent
