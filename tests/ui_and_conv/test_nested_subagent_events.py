"""Nested SubagentEvent ancestry and bounded dispatch tests."""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from pythinker_core.message import ToolCall
from pythinker_core.tooling import ToolOk
from rich.console import Console, Group, RenderableType

from pythinker_code.ui.shell.visualize import _live_view as live_view_module
from pythinker_code.ui.shell.visualize import _LiveView
from pythinker_code.wire.types import (
    Event,
    StatusUpdate,
    SubagentEvent,
    ToolCallPart,
    ToolExecutionStarted,
    ToolOutputPart,
    ToolResult,
    TurnBegin,
)
from pythinker_code.wire.types import ToolCall as WireToolCall


def _render(renderables: Iterable[RenderableType]) -> str:
    console = Console(width=100, record=True, highlight=False, color_system=None)
    console.print(Group(*renderables))
    return console.export_text()


def _tool_call(call_id: str, name: str, arguments: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(name=name, arguments=arguments),
    )


def _root_agent_call() -> WireToolCall:
    return WireToolCall(
        id="root-agent",
        function=WireToolCall.FunctionBody(
            name="Agent",
            arguments='{"description":"nested task","subagent_type":"coder","prompt":"work"}',
        ),
    )


def _nested_event(ancestor_ids: list[str], event: Event) -> SubagentEvent:
    nested: Event = event
    for parent_id in reversed(ancestor_ids):
        nested = SubagentEvent(
            parent_tool_call_id=parent_id,
            agent_id=f"agent-{parent_id}",
            subagent_type="coder",
            event=nested,
        )
    return SubagentEvent(
        parent_tool_call_id="root-agent",
        agent_id="agent-root",
        subagent_type="coder",
        event=nested,
    )


def _view() -> _LiveView:
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="work"))
    view.dispatch_wire_message(_root_agent_call())
    return view


@pytest.mark.parametrize("nesting_depth", [2, 3])
def test_nested_tool_lifecycle_rolls_up_under_root_and_first_result_wins(
    nesting_depth: int,
) -> None:
    view = _view()
    ancestor_ids: list[str] = []

    for depth in range(1, nesting_depth + 1):
        call_id = f"nested-{depth}"
        is_leaf = depth == nesting_depth
        call = _tool_call(
            call_id,
            "Read" if is_leaf else "Agent",
            '{"file_path":"src/' if is_leaf else '{"description":"deeper"}',
        )
        view.dispatch_wire_message(_nested_event(ancestor_ids, call))
        ancestor_ids.append(call_id)

    leaf_id = ancestor_ids[-1]
    leaf_parent_ids = ancestor_ids[:-1]
    view.dispatch_wire_message(
        _nested_event(leaf_parent_ids, ToolCallPart(arguments_part='module.py"}'))
    )
    view.dispatch_wire_message(
        _nested_event(leaf_parent_ids, ToolExecutionStarted(tool_call_id=leaf_id))
    )
    view.dispatch_wire_message(
        _nested_event(
            leaf_parent_ids,
            ToolOutputPart(tool_call_id=leaf_id, text="STREAMED_NESTED_OUTPUT\n"),
        )
    )

    root_block = view._tool_call_blocks["root-agent"]
    for expected_depth, call_id in enumerate(ancestor_ids, start=1):
        indexed_block, indexed_depth = view._subagent_tool_call_ancestry[call_id]
        assert indexed_block is root_block
        assert indexed_depth == expected_depth
    # Nested activity rolls up under the root Agent as payload-free semantic rows;
    # the leaf's raw output and args never leak into the rendered tree.
    assert leaf_id in root_block._subagent_execution_started
    assert "reading…" in _render([view.compose()])
    assert "STREAMED_NESTED_OUTPUT" not in _render([view.compose()])
    assert "src/module.py" not in _render([view.compose()])

    view.dispatch_wire_message(
        _nested_event(
            leaf_parent_ids,
            ToolResult(tool_call_id=leaf_id, return_value=ToolOk(output="FIRST_RESULT")),
        )
    )
    view.dispatch_wire_message(
        _nested_event(leaf_parent_ids, ToolExecutionStarted(tool_call_id=leaf_id))
    )
    view.dispatch_wire_message(
        _nested_event(
            leaf_parent_ids,
            ToolOutputPart(tool_call_id=leaf_id, text="LATE_OUTPUT"),
        )
    )
    view.dispatch_wire_message(
        _nested_event(
            leaf_parent_ids,
            ToolResult(tool_call_id=leaf_id, return_value=ToolOk(output="LATE_RESULT")),
        )
    )

    # First-result-wins: the leaf finish is recorded once and the late duplicate
    # result/output is ignored; neither raw payload is ever surfaced or buffered.
    assert leaf_id in root_block._finished_subagent_tool_call_ids
    assert root_block._n_finished_subagent_tool_calls == 0
    assert not root_block._finished_subagent_tool_calls
    rendered_after_late = _render([view.compose()])
    assert "FIRST_RESULT" not in rendered_after_late
    assert "LATE_OUTPUT" not in rendered_after_late
    assert "LATE_RESULT" not in rendered_after_late
    assert "LATE_OUTPUT" not in root_block._subagent_output_parts


def test_missing_ancestry_renders_one_muted_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[RenderableType] = []
    monkeypatch.setattr(
        live_view_module,
        "emit_scrollback_block",
        lambda _console, renderable: emitted.append(renderable),
    )
    view = _view()

    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="missing-parent",
            event=ToolOutputPart(tool_call_id="missing-tool", text="SECRET_PAYLOAD"),
        )
    )

    assert len(emitted) == 1
    output = _render(emitted)
    assert "Nested subagent activity unavailable" in output
    assert "missing ancestry" in output
    assert "SECRET_PAYLOAD" not in output


def test_recursive_depth_overflow_renders_one_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted: list[RenderableType] = []
    monkeypatch.setattr(
        live_view_module,
        "emit_scrollback_block",
        lambda _console, renderable: emitted.append(renderable),
    )
    view = _view()
    nested: Event = ToolOutputPart(tool_call_id="never-dispatched", text="TOO_DEEP")
    for _ in range(17):
        nested = SubagentEvent(parent_tool_call_id="root-agent", event=nested)
    assert isinstance(nested, SubagentEvent)

    view.dispatch_wire_message(nested)

    assert len(emitted) == 1
    output = _render(emitted)
    assert "Nested subagent activity unavailable" in output
    assert "depth limit reached" in output
    assert "TOO_DEEP" not in output


def test_flushing_root_agent_purges_nested_ancestry() -> None:
    view = _view()
    # Two nested tool calls under the root Agent populate the ancestry map.
    view.dispatch_wire_message(
        _nested_event([], _tool_call("nested-1", "Agent", '{"description":"deeper"}'))
    )
    view.dispatch_wire_message(
        _nested_event(["nested-1"], _tool_call("nested-2", "Read", '{"file_path":"x"}'))
    )
    root_block = view._tool_call_blocks["root-agent"]
    assert set(view._subagent_tool_call_ancestry) == {"nested-1", "nested-2"}

    # Finishing the root Agent tool call flushes it out of the live area.
    view.dispatch_wire_message(
        ToolResult(tool_call_id="root-agent", return_value=ToolOk(output="done"))
    )

    assert "root-agent" not in view._tool_call_blocks
    # The whole nested subtree's ancestry is purged, not left dangling on the
    # now-archived root block.
    assert view._subagent_tool_call_ancestry == {}
    assert root_block is not None  # kept referenced so identity purge is exercised

    # A late nested event for a now-purged parent falls back to "missing
    # ancestry" instead of resolving to (and mutating) the archived root block.
    view.dispatch_wire_message(
        _nested_event(["nested-1"], _tool_call("nested-3", "Read", '{"file_path":"y"}'))
    )
    assert "nested-3" not in view._subagent_tool_call_ancestry
