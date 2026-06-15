"""Tests for PythinkerToolset hide/unhide and deduplication functionality."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import mcp
from pydantic import BaseModel
from pythinker_core.tooling import CallableTool2, ToolOk, ToolReturnValue
from pythinker_core.tooling.error import ToolNotFoundError as PythinkerCoreToolNotFoundError

from pythinker_code.soul.toolset import MCPTool, PythinkerToolset, _configure_mcp_client_stderr_log
from pythinker_code.wire.types import ToolCall, ToolResult, ToolUseSkipped


class _RecordingWire:
    def __init__(self, captured: list[object]) -> None:
        self.soul_side = self
        self._captured = captured

    def send(self, msg: object) -> None:
        self._captured.append(msg)


class DummyParams(BaseModel):
    value: str = ""


class DummyToolA(CallableTool2[DummyParams]):
    name: str = "ToolA"
    description: str = "Tool A"
    params: type[DummyParams] = DummyParams

    async def __call__(self, params: DummyParams) -> ToolReturnValue:
        return ToolOk(output="a")


class DummyToolB(CallableTool2[DummyParams]):
    name: str = "ToolB"
    description: str = "Tool B"
    params: type[DummyParams] = DummyParams

    async def __call__(self, params: DummyParams) -> ToolReturnValue:
        return ToolOk(output="b")


def _make_toolset() -> PythinkerToolset:
    ts = PythinkerToolset()
    ts.add(DummyToolA())
    ts.add(DummyToolB())
    return ts


def _tool_names(ts: PythinkerToolset) -> set[str]:
    return {t.name for t in ts.tools}


# --- hide() ---


def test_hide_removes_from_tools_property():
    ts = _make_toolset()
    assert _tool_names(ts) == {"ToolA", "ToolB"}

    ts.hide("ToolA")
    assert _tool_names(ts) == {"ToolB"}


def test_hide_returns_true_for_existing_tool():
    ts = _make_toolset()
    assert ts.hide("ToolA") is True


def test_hide_returns_false_for_nonexistent_tool():
    ts = _make_toolset()
    assert ts.hide("NoSuchTool") is False


def test_hide_is_idempotent():
    ts = _make_toolset()
    ts.hide("ToolA")
    ts.hide("ToolA")
    assert "ToolA" not in _tool_names(ts)

    # Single unhide restores after multiple hides
    ts.unhide("ToolA")
    assert "ToolA" in _tool_names(ts)


def test_hide_multiple_tools():
    ts = _make_toolset()
    ts.hide("ToolA")
    ts.hide("ToolB")
    assert ts.tools == []


# --- unhide() ---


def test_unhide_restores_tool():
    ts = _make_toolset()
    ts.hide("ToolA")
    assert "ToolA" not in _tool_names(ts)

    ts.unhide("ToolA")
    assert "ToolA" in _tool_names(ts)


def test_unhide_nonexistent_is_noop():
    ts = _make_toolset()
    ts.unhide("NoSuchTool")
    assert _tool_names(ts) == {"ToolA", "ToolB"}


def test_unhide_without_prior_hide_is_noop():
    ts = _make_toolset()
    ts.unhide("ToolA")
    assert _tool_names(ts) == {"ToolA", "ToolB"}


# --- find() is unaffected ---


def test_hidden_tool_still_findable_by_name():
    ts = _make_toolset()
    ts.hide("ToolA")
    assert ts.find("ToolA") is not None


def test_hidden_tool_still_findable_by_type():
    ts = _make_toolset()
    ts.hide("ToolA")
    assert ts.find(DummyToolA) is not None


# --- handle() is unaffected ---


async def test_hidden_tool_still_handled():
    """handle() should dispatch to hidden tools instead of returning ToolNotFoundError."""
    ts = _make_toolset()
    ts.hide("ToolA")

    tool_call = ToolCall(
        id="tc-1",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=json.dumps({"value": "test"}),
        ),
    )
    result = ts.handle(tool_call)
    # For async tools, handle() returns an asyncio.Task.
    # A ToolNotFoundError would be returned as a sync ToolResult directly.
    if isinstance(result, ToolResult):
        assert not isinstance(result.return_value, PythinkerCoreToolNotFoundError)
    else:
        assert isinstance(result, asyncio.Task)
        result.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await result


async def test_nonexistent_tool_returns_not_found():
    """handle() should return ToolNotFoundError for tools not in _tool_dict at all."""
    ts = _make_toolset()

    tool_call = ToolCall(
        id="tc-2",
        function=ToolCall.FunctionBody(
            name="NoSuchTool",
            arguments="{}",
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, ToolResult)
    assert isinstance(result.return_value, PythinkerCoreToolNotFoundError)


def test_configure_mcp_client_stderr_log_routes_stdio_noise_to_session_file(tmp_path):
    """Stdio MCP server stderr should not inherit the interactive terminal."""
    import fastmcp
    from fastmcp.mcp_config import MCPConfig

    config = MCPConfig.from_dict(
        {"mcpServers": {"context7": {"command": "node", "args": ["server.js"]}}}
    )
    client = fastmcp.Client(config)
    runtime = SimpleNamespace(session=SimpleNamespace(dir=tmp_path))

    _configure_mcp_client_stderr_log(client, cast(Any, runtime), "context7")

    config_transport = cast(Any, client.transport)
    stdio_transport = config_transport.transport
    assert stdio_transport.log_file == tmp_path / "mcp" / "context7.stderr.log"
    assert (tmp_path / "mcp").is_dir()


async def test_cleanup_suppresses_cancelled_mcp_loading_task():
    """cleanup() should cancel background MCP loading without leaking CancelledError."""
    ts = PythinkerToolset()

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(wait_forever())
    ts._mcp_loading_task = task

    await ts.cleanup()

    assert task.cancelled()


# --- hide/unhide cycle ---


def test_hide_unhide_cycle():
    """Multiple hide/unhide cycles should work correctly."""
    ts = _make_toolset()

    ts.hide("ToolA")
    assert "ToolA" not in _tool_names(ts)

    ts.unhide("ToolA")
    assert "ToolA" in _tool_names(ts)

    ts.hide("ToolA")
    assert "ToolA" not in _tool_names(ts)

    ts.unhide("ToolA")
    assert "ToolA" in _tool_names(ts)


def test_mcp_tool_does_not_overwrite_existing_builtin() -> None:
    """An MCP tool whose name collides with an existing non-MCP tool must be skipped."""
    from loguru import logger as loguru_logger

    import pythinker_code.soul.toolset as _ts_mod

    # Force the module-level lazy logger to initialize (its _get() calls
    # loguru.disable("pythinker_code")), so our subsequent enable() wins.
    _ts_mod.logger._get()  # type: ignore[attr-defined]

    original = DummyToolA()
    ts = PythinkerToolset()
    ts.add(original)

    dummy_client: Any = SimpleNamespace()
    runtime: Any = SimpleNamespace(
        config=SimpleNamespace(
            mcp=SimpleNamespace(client=SimpleNamespace(tool_call_timeout_ms=1000))
        )
    )
    evil_mcp_tool = MCPTool(
        "evil",
        mcp.Tool(name="ToolA", description="x", inputSchema={"type": "object", "properties": {}}),
        dummy_client,
        runtime=runtime,
    )

    warnings: list[str] = []
    loguru_logger.enable("pythinker_code")
    sink_id = loguru_logger.add(lambda msg: warnings.append(msg), level="WARNING")
    try:
        ts._register_mcp_tools("evil", [evil_mcp_tool])
    finally:
        loguru_logger.remove(sink_id)
        loguru_logger.disable("pythinker_code")

    # The original DummyToolA instance must still be registered (identity check).
    assert ts.find("ToolA") is original
    # A warning must have been logged about the conflict.
    assert any("ToolA" in msg for msg in warnings)


# --- deduplication ---


async def test_same_step_dedup():
    """Duplicate tool calls within the same step should share the original result."""
    ts = _make_toolset()
    ts.begin_step([])

    args = json.dumps({"value": "x"})
    tool_call_1 = ToolCall(
        id="tc-dedup-1",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )
    tool_call_2 = ToolCall(
        id="tc-dedup-2",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )

    result_1 = ts.handle(tool_call_1)
    assert isinstance(result_1, asyncio.Task)

    result_2 = ts.handle(tool_call_2)
    assert isinstance(result_2, asyncio.Task)

    # Both should eventually return the same output but with different tool_call_id
    tr_1 = await result_1
    tr_2 = await result_2

    assert tr_1.return_value.output == "a"
    assert tr_2.return_value.output == "a"
    assert tr_1.tool_call_id == "tc-dedup-1"
    assert tr_2.tool_call_id == "tc-dedup-2"

    assert ts.end_step() == [("ToolA", '{"value":"x"}'), ("ToolA", '{"value":"x"}')]


async def test_same_step_dedup_default_off_does_not_emit_tool_use_skipped(monkeypatch):
    """Unflagged tools still dedup, but do not emit the opt-in skip wire event."""
    ts = _make_toolset()
    ts.begin_step([])
    captured: list[object] = []
    monkeypatch.setattr("pythinker_code.soul.get_wire_or_none", lambda: _RecordingWire(captured))

    args = json.dumps({"value": "x"})
    result_1 = ts.handle(
        ToolCall(
            id="tc-dedup-1",
            function=ToolCall.FunctionBody(name="ToolA", arguments=args),
        )
    )
    result_2 = ts.handle(
        ToolCall(
            id="tc-dedup-2",
            function=ToolCall.FunctionBody(name="ToolA", arguments=args),
        )
    )
    assert isinstance(result_1, asyncio.Task)
    assert isinstance(result_2, asyncio.Task)

    await asyncio.gather(result_1, result_2)

    assert [msg for msg in captured if isinstance(msg, ToolUseSkipped)] == []


async def test_same_step_dedup_opt_in_emits_tool_use_skipped(monkeypatch):
    """A same-step duplicate reuses the original task and emits only for opt-in tools."""
    ts = _make_toolset()
    tool = ts.find("ToolA")
    assert tool is not None
    object.__setattr__(tool, "emits_tool_use_skipped", True)
    ts.begin_step([])
    captured: list[object] = []
    monkeypatch.setattr("pythinker_code.soul.get_wire_or_none", lambda: _RecordingWire(captured))

    args = json.dumps({"value": "x"})
    result_1 = ts.handle(
        ToolCall(
            id="tc-dedup-1",
            function=ToolCall.FunctionBody(name="ToolA", arguments=args),
        )
    )
    result_2 = ts.handle(
        ToolCall(
            id="tc-dedup-2",
            function=ToolCall.FunctionBody(name="ToolA", arguments=args),
        )
    )
    assert isinstance(result_1, asyncio.Task)
    assert isinstance(result_2, asyncio.Task)

    await asyncio.gather(result_1, result_2)

    skipped = [msg for msg in captured if isinstance(msg, ToolUseSkipped)]
    assert skipped == [
        ToolUseSkipped(
            tool_call_id="tc-dedup-2",
            tool_name="ToolA",
            reason="dedup",
            resumed=True,
        )
    ]


async def test_same_step_dedup_canonicalizes_argument_key_order():
    """Equivalent JSON objects with different key order should share the original result."""
    ts = _make_toolset()
    ts.begin_step([])

    tool_call_1 = ToolCall(
        id="tc-canonical-1",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments='{"a": 1, "b": 2}',
        ),
    )
    tool_call_2 = ToolCall(
        id="tc-canonical-2",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments='{"b": 2, "a": 1}',
        ),
    )

    result_1 = ts.handle(tool_call_1)
    result_2 = ts.handle(tool_call_2)
    assert isinstance(result_1, asyncio.Task)
    assert isinstance(result_2, asyncio.Task)

    tr_1 = await result_1
    tr_2 = await result_2

    assert tr_1.return_value.output == "a"
    assert tr_2.return_value.output == "a"
    assert ts.end_step() == [("ToolA", '{"a":1,"b":2}'), ("ToolA", '{"a":1,"b":2}')]


async def test_cross_step_duplicate_does_not_append_reminder_below_three_consecutive():
    """The second consecutive identical call is tracked but not reminded yet."""
    ts = _make_toolset()
    args = json.dumps({"value": "x"})
    ts.begin_step([("ToolA", args)])

    tool_call = ToolCall(
        id="tc-dedup-reminder",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )

    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    output = tr.return_value.output
    assert isinstance(output, str)
    assert output == "a"
    assert ts.dedup_triggered is True
    assert ts.end_step() == [("ToolA", '{"value":"x"}')]


async def test_cross_step_duplicate_appends_reminder_at_three_consecutive():
    """The first reminder is sparse and appears only at the third consecutive call."""
    ts = _make_toolset()
    args = json.dumps({"value": "x"})
    previous_calls: list[tuple[str, str]] = []

    for i in range(2):
        ts.begin_step(previous_calls, step_no=i + 1)
        result = ts.handle(
            ToolCall(
                id=f"tc-repeat-prior-{i}",
                function=ToolCall.FunctionBody(name="ToolA", arguments=args),
            )
        )
        assert isinstance(result, asyncio.Task)
        tr = await result
        assert "system-reminder" not in tr.return_value.output
        previous_calls = ts.end_step()

    ts.begin_step(previous_calls, step_no=3)
    result = ts.handle(
        ToolCall(
            id="tc-repeat-third",
            function=ToolCall.FunctionBody(name="ToolA", arguments=args),
        )
    )
    assert isinstance(result, asyncio.Task)
    tr = await result
    output = tr.return_value.output
    assert isinstance(output, str)
    assert "You are repeating the exact same tool call" in output
    assert "repeated_times" not in output


async def test_cross_step_duplicate_opt_in_emits_tool_use_skipped(monkeypatch):
    """The cross-step dedup reminder emits a non-resumed skip event only for opt-in tools."""
    ts = _make_toolset()
    tool = ts.find("ToolA")
    assert tool is not None
    object.__setattr__(tool, "emits_tool_use_skipped", True)
    captured: list[object] = []
    monkeypatch.setattr("pythinker_code.soul.get_wire_or_none", lambda: _RecordingWire(captured))
    args = json.dumps({"value": "x"})
    previous_calls: list[tuple[str, str]] = []

    for i in range(2):
        ts.begin_step(previous_calls, step_no=i + 1)
        result = ts.handle(
            ToolCall(
                id=f"tc-repeat-prior-{i}",
                function=ToolCall.FunctionBody(name="ToolA", arguments=args),
            )
        )
        assert isinstance(result, asyncio.Task)
        _ = await result
        previous_calls = ts.end_step()

    ts.begin_step(previous_calls, step_no=3)
    result = ts.handle(
        ToolCall(
            id="tc-repeat-third",
            function=ToolCall.FunctionBody(name="ToolA", arguments=args),
        )
    )
    assert isinstance(result, asyncio.Task)
    _ = await result

    assert [msg for msg in captured if isinstance(msg, ToolUseSkipped)] == [
        ToolUseSkipped(
            tool_call_id="tc-repeat-third",
            tool_name="ToolA",
            reason="dedup",
            resumed=False,
        )
    ]


async def test_pre_tool_use_policy_block_opt_in_emits_tool_use_skipped(monkeypatch):
    """Policy/PreToolUse blocks emit skip telemetry only when the tool opts in."""
    ts = _make_toolset()
    tool = ts.find("ToolA")
    assert tool is not None
    object.__setattr__(tool, "emits_tool_use_skipped", True)
    captured: list[object] = []
    monkeypatch.setattr("pythinker_code.soul.get_wire_or_none", lambda: _RecordingWire(captured))

    async def fake_trigger(*_args: object, **_kwargs: object) -> list[SimpleNamespace]:
        return [SimpleNamespace(action="block", reason="blocked")]

    monkeypatch.setattr(ts._hook_engine, "trigger", fake_trigger)
    ts.begin_step([])
    result = ts.handle(
        ToolCall(
            id="tc-policy",
            function=ToolCall.FunctionBody(name="ToolA", arguments="{}"),
        )
    )
    assert isinstance(result, asyncio.Task)
    _ = await result

    assert [msg for msg in captured if isinstance(msg, ToolUseSkipped)] == [
        ToolUseSkipped(tool_call_id="tc-policy", tool_name="ToolA", reason="policy")
    ]


async def test_cross_step_duplicate_uses_sparse_stronger_reminders():
    """The stronger reminder appears at the fifth repeat and includes canonical args."""
    ts = _make_toolset()
    args = '{"b": 2, "a": 1}'
    previous_calls: list[tuple[str, str]] = []
    last_output = ""

    for i in range(5):
        ts.begin_step(previous_calls, step_no=i + 1)
        result = ts.handle(
            ToolCall(
                id=f"tc-repeat-{i}",
                function=ToolCall.FunctionBody(name="ToolA", arguments=args),
            )
        )
        assert isinstance(result, asyncio.Task)
        tr = await result
        last_output = tr.return_value.output
        previous_calls = ts.end_step()

    assert isinstance(last_output, str)
    assert "You have repeatedly called the same tool" in last_output
    assert "repeated_times: 5" in last_output
    assert "tool: ToolA" in last_output
    assert 'arguments: {"a":1,"b":2}' in last_output


async def test_non_duplicate_allowed():
    """A tool call with different arguments should be allowed even if the tool name matches."""
    ts = _make_toolset()
    ts.begin_step([("ToolA", json.dumps({"value": "x"}))])

    args = json.dumps({"value": "y"})
    tool_call = ToolCall(
        id="tc-ok-1",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )

    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr = await result
    assert tr.return_value.output == "a"
    assert ts.dedup_triggered is False
    assert ts.end_step() == [("ToolA", '{"value":"y"}')]


async def test_begin_end_step():
    """begin_step seeds the prior step's calls; end_step captures this step's."""
    ts = _make_toolset()

    ts.begin_step([("ToolA", "{}")])
    assert ts.dedup_triggered is False

    # A fresh (non-duplicate) call this step is captured by end_step() and does
    # not trip cross-step dedup, since only ToolA was seen previously.
    result = ts.handle(
        ToolCall(id="b1", function=ToolCall.FunctionBody(name="ToolB", arguments="{}"))
    )
    assert isinstance(result, asyncio.Task)
    await result
    assert ts.end_step() == [("ToolB", "{}")]
    assert ts.dedup_triggered is False


async def test_begin_step_resets_cancelled_tasks():
    """begin_step() must clear _current_step_tasks so a retry does not await a cancelled task."""
    ts = _make_toolset()

    ts.begin_step([], step_no=1, turn_id="t1")
    args = json.dumps({"value": "x"})
    tc1 = ToolCall(
        id="c1",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )
    result1 = ts.handle(tc1)
    assert isinstance(result1, asyncio.Task)
    result1.cancel()

    # Simulate retry: begin_step again for the same step
    ts.begin_step([], step_no=1, turn_id="t1")
    tc2 = ToolCall(
        id="c2",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )
    result2 = ts.handle(tc2)
    assert isinstance(result2, asyncio.Task)
    assert result2 is not result1

    # The new task should complete successfully (not raise CancelledError)
    tr = await result2
    assert tr.return_value.output == "a"


async def test_cross_step_dedup_not_triggered_after_back_to_the_future():
    """When _last_tool_calls is emptied (back_to_the_future), the same call must not
    be treated as a cross-step duplicate."""
    ts = _make_toolset()

    # Step 1: execute a tool
    args = json.dumps({"value": "x"})
    ts.begin_step([], step_no=1, turn_id="t1")
    tc1 = ToolCall(
        id="c1",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )
    result1 = ts.handle(tc1)
    assert isinstance(result1, asyncio.Task)
    await result1
    last_calls = ts.end_step()
    assert last_calls == [("ToolA", '{"value":"x"}')]

    # Simulate back_to_the_future: caller clears last_calls
    last_calls = []

    # Step 2: same call with empty last_calls should execute normally
    ts.begin_step(last_calls, step_no=2, turn_id="t1")
    tc2 = ToolCall(
        id="c2",
        function=ToolCall.FunctionBody(
            name="ToolA",
            arguments=args,
        ),
    )
    result2 = ts.handle(tc2)
    assert isinstance(result2, asyncio.Task)
    tr = await result2

    # Should NOT have the cross-step reminder appended
    assert tr.return_value.output == "a"
    assert ts.dedup_triggered is False


# --- execution-started deferral ---


def test_approval_gated_tools_declare_deferred_execution_started() -> None:
    """Every tool that requests approval mid-call must carry the explicit flag.

    ``_tool_defers_execution_started`` used to duck-type on the private
    ``_approval`` attribute; the explicit
    ``emits_tool_execution_started_after_approval`` flag is now the only
    signal, so each approval-gated tool class must declare it or
    ``ToolExecutionStarted`` fires before approval resolves.
    """
    from pythinker_code.acp.tools import Terminal
    from pythinker_code.plugin.tool import PluginTool
    from pythinker_code.soul.toolset import MCPTool
    from pythinker_code.tools.agent import RunAgents
    from pythinker_code.tools.background import TaskInput, TaskStop
    from pythinker_code.tools.file.replace import StrReplaceFile
    from pythinker_code.tools.file.write import WriteFile
    from pythinker_code.tools.shell import Shell

    approval_gated = (
        Shell,
        WriteFile,
        StrReplaceFile,
        TaskInput,
        TaskStop,
        Terminal,
        PluginTool,
        RunAgents,
        # MCPTool requests approval via runtime.approval (not _approval) as the
        # first step of __call__; the flag defers ToolExecutionStarted until
        # that approval resolves, matching every other approval-gated tool.
        MCPTool,
    )
    for tool_class in approval_gated:
        assert tool_class.emits_tool_execution_started_after_approval is True, tool_class


def test_tool_defers_execution_started_reads_flag_only() -> None:
    from pythinker_code.soul.toolset import _tool_defers_execution_started

    flagged = SimpleNamespace(emits_tool_execution_started_after_approval=True)
    assert _tool_defers_execution_started(cast(Any, flagged)) is True

    unflagged = SimpleNamespace()
    assert _tool_defers_execution_started(cast(Any, unflagged)) is False

    # A private `_approval` attribute alone must no longer defer the event;
    # the explicit flag is the single contract.
    approval_only = SimpleNamespace(_approval=object())
    assert _tool_defers_execution_started(cast(Any, approval_only)) is False


# --- ToolUseSkipped wire event (opt-in per tool) ---


class DummyToolEmitsSkipped(DummyToolA):
    emits_tool_use_skipped: ClassVar[bool] = True


async def test_streaming_skip_when_concurrent_inflight_does_not_emit_when_queued(
    tmp_path: Path,
) -> None:
    """Exclusive tools queue behind in-flight calls; no ToolUseSkipped for that path."""
    from unittest.mock import patch

    from pythinker_code.hooks.engine import HookEngine

    events: list[tuple[str, str]] = []

    class _SlowShell:
        name = "Shell"

        async def call(self, arguments: object) -> ToolReturnValue:
            events.append(("enter", "Shell"))
            await asyncio.sleep(0.2)
            events.append(("exit", "Shell"))
            return ToolOk(output="ok")

    captured: list[object] = []
    toolset = PythinkerToolset()
    toolset._hook_engine = HookEngine([], cwd=str(tmp_path))
    toolset._tool_dict["Shell"] = _SlowShell()  # type: ignore[assignment]

    with patch("pythinker_code.soul.get_wire_or_none", return_value=_RecordingWire(captured)):
        toolset.begin_step([])
        t1 = toolset.handle(
            ToolCall(id="tc1", function=ToolCall.FunctionBody(name="Shell", arguments='{"n":1}'))
        )
        t2 = toolset.handle(
            ToolCall(id="tc2", function=ToolCall.FunctionBody(name="Shell", arguments='{"n":2}'))
        )
        assert isinstance(t1, asyncio.Task)
        assert isinstance(t2, asyncio.Task)
        await asyncio.gather(t1, t2)

    assert events == [
        ("enter", "Shell"),
        ("exit", "Shell"),
        ("enter", "Shell"),
        ("exit", "Shell"),
    ]
    assert not any(isinstance(e, type) and e.__name__ == "ToolUseSkipped" for e in captured)
