"""Integration tests for subagent ToolOutputPart and ToolExecutionStarted wiring."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from pythinker_core.message import ToolCall
from pythinker_core.tooling import ToolOk
from rich.console import Console, RenderableType

from pythinker_code.ui.shell.components.render_utils import cell_width
from pythinker_code.ui.shell.tool_renderers import (
    clear_tool_renderers,
    register_builtin_renderers,
)
from pythinker_code.ui.shell.visualize import _LiveView
from pythinker_code.wire.types import (
    StatusUpdate,
    SubagentEvent,
    ToolCallPart,
    ToolExecutionStarted,
    ToolOutputPart,
    ToolResult,
    TurnBegin,
)
from pythinker_code.wire.types import (
    ToolCall as WireToolCall,
)


@pytest.fixture(autouse=True)
def _isolated_tool_renderer_registry() -> Generator[None, None, None]:
    clear_tool_renderers()
    register_builtin_renderers()
    yield
    clear_tool_renderers()


def _render(view: _LiveView, *, width: int = 100) -> str:
    console = Console(width=width, record=True, highlight=False, color_system=None)
    console.print(view.compose())
    return console.export_text()


def _agent_call(call_id: str = "agent-1") -> WireToolCall:
    return WireToolCall(
        id=call_id,
        function=WireToolCall.FunctionBody(
            name="Agent",
            arguments='{"description":"security scan","subagent_type":"security-reviewer","prompt":"check it"}',
        ),
    )


def _run_agents_call(call_id: str = "run-agents-1") -> WireToolCall:
    return WireToolCall(
        id=call_id,
        function=WireToolCall.FunctionBody(
            name="RunAgents",
            arguments=(
                '{"summary":"Audit TODOs","run_in_background":false,"agents":['
                '{"title":"Find TODO comments","name":"todo_scan","subagent_type":"explore",'
                '"prompt":"grep -r TODO /repo/src/secret.py"},'
                '{"title":"Count files","name":"file_count","subagent_type":"explore",'
                '"prompt":"read /repo/src/private.py"},'
                '{"title":"Queued worker","name":"queued","subagent_type":"explore",'
                '"prompt":"do not leak this prompt"}'
                "]}"
            ),
        ),
    )


def _sub_tool_call(sub_id: str, name: str, args: str) -> ToolCall:
    return ToolCall(
        id=sub_id,
        function=ToolCall.FunctionBody(name=name, arguments=args),
    )


def test_subagent_tool_output_part_appears_in_live_view():
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_agent_call())

    sub_call = _sub_tool_call("sub-1", "Bash", '{"command":"grep -r TODO ."}')
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=sub_call,
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=ToolOutputPart(tool_call_id="sub-1", text="src/app.py:42: # TODO\n"),
        )
    )

    output = _render(view)
    assert "src/app.py:42" in output


def test_subagent_tool_execution_started_tracked():
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_agent_call())

    sub_call = _sub_tool_call("sub-1", "Read", '{"file_path":"src/app.py"}')
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=sub_call,
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=ToolExecutionStarted(tool_call_id="sub-1"),
        )
    )

    block = view._tool_call_blocks["agent-1"]
    assert "sub-1" in block._subagent_execution_started


def test_subagent_tool_call_and_args_request_live_refresh():
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_agent_call())

    view._need_recompose = False
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=_sub_tool_call("sub-1", "Read", '{"file_path":"src/'),
        )
    )
    assert view._need_recompose is True

    view._need_recompose = False
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=ToolCallPart(arguments_part='app.py"}'),
        )
    )
    assert view._need_recompose is True
    assert "src/app.py" in _render(view)


def test_output_part_for_unknown_parent_renders_fallback_without_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    emitted: list[RenderableType] = []
    from pythinker_code.ui.shell.visualize import _live_view as live_view_module

    monkeypatch.setattr(
        live_view_module,
        "emit_scrollback_block",
        lambda _console, renderable: emitted.append(renderable),
    )
    # No agent tool call dispatched — parent_tool_call_id won't resolve
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="nonexistent-agent",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=ToolOutputPart(tool_call_id="sub-1", text="should be ignored\n"),
        )
    )
    # Must not raise or expose the nested payload; a muted fallback is emitted.
    output = _render(view)
    assert "should be ignored" not in output
    assert len(emitted) == 1
    console = Console(width=100, record=True, highlight=False, color_system=None)
    console.print(emitted[0])
    fallback = console.export_text()
    assert "Nested subagent activity unavailable" in fallback
    assert "should be ignored" not in fallback


def test_output_cleared_after_sub_tool_call_finishes():
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_agent_call())

    sub_call = _sub_tool_call("sub-1", "Bash", '{"command":"ls"}')
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=sub_call,
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=ToolOutputPart(tool_call_id="sub-1", text="SHOULD_DISAPPEAR\n"),
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="agent-1",
            agent_id="a1",
            subagent_type="security-reviewer",
            event=ToolResult(tool_call_id="sub-1", return_value=ToolOk(output="")),
        )
    )

    output = _render(view)
    assert "SHOULD_DISAPPEAR" not in output


def test_run_agents_activity_tree_keeps_same_type_agents_separate_and_safe():
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_run_agents_call())

    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a1",
            subagent_type="explore",
            description="Find TODO comments",
            event=_sub_tool_call(
                "sub-a1", "Grep", '{"pattern":"TODO","path":"/repo/src/secret.py"}'
            ),
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a1",
            subagent_type="explore",
            description="Find TODO comments",
            event=ToolExecutionStarted(tool_call_id="sub-a1"),
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a1",
            subagent_type="explore",
            description="Find TODO comments",
            event=ToolOutputPart(tool_call_id="sub-a1", text="src/app.py:42: # TODO\n"),
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a2",
            subagent_type="explore",
            description="Count files",
            event=_sub_tool_call("sub-a2", "Read", '{"file_path":"/repo/src/private.py"}'),
        )
    )

    output = _render(view, width=100)
    assert output.count("Agents") == 1
    assert "RunAgents(" not in output
    assert "Explore" in output
    assert output.count("Find TODO comments") == 1
    assert "searching…" in output
    assert output.count("Count files") == 1
    assert "reading…" in output
    assert "1 queued" in output
    assert "├─" in output
    assert "└─" in output
    assert "│  ⎿" in output
    assert "agent searching" not in output
    assert "agent reading" not in output
    assert "grep -r TODO" not in output
    assert "/repo/src/secret.py" not in output
    assert "/repo/src/private.py" not in output
    assert "src/app.py:42" not in output
    assert "a1" not in output
    assert "a2" not in output
    assert "do not leak this prompt" not in output


def test_run_agents_activity_states_update_one_row_per_agent():
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_run_agents_call())

    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a1",
            subagent_type="explore",
            description="Find TODO comments",
            event=_sub_tool_call("sub-a1", "Grep", '{"pattern":"TODO"}'),
        )
    )
    waiting = _render(view)
    assert "waiting" in waiting
    assert "searching…" in waiting

    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a1",
            subagent_type="explore",
            description="Find TODO comments",
            event=ToolExecutionStarted(tool_call_id="sub-a1"),
        )
    )
    running = _render(view)
    assert "running" in running

    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a1",
            subagent_type="explore",
            description="Find TODO comments",
            event=ToolResult(tool_call_id="sub-a1", return_value=ToolOk(output="done")),
        )
    )
    thinking = _render(view)
    assert "thinking…" in thinking
    assert thinking.count("Find TODO comments") == 1

    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a1",
            subagent_type="explore",
            description="Find TODO comments",
            event=_sub_tool_call("sub-a1b", "Read", '{"file_path":"/repo/next.py"}'),
        )
    )
    updated = _render(view)
    assert updated.count("Find TODO comments") == 1
    assert "reading…" in updated


def test_run_agents_activity_preserves_launch_order_across_state_transitions():
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_run_agents_call())

    for agent_id, description, tool_name in (
        ("a1", "First launch", "Grep"),
        ("a2", "Second launch", "Read"),
        ("a3", "Third launch", "Bash"),
    ):
        view.dispatch_wire_message(
            SubagentEvent(
                parent_tool_call_id="run-agents-1",
                agent_id=agent_id,
                subagent_type="explore",
                description=description,
                event=_sub_tool_call(f"sub-{agent_id}", tool_name, "{}"),
            )
        )

    waiting = _render(view)
    assert (
        waiting.index("First launch")
        < waiting.index("Second launch")
        < waiting.index("Third launch")
    )

    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a3",
            subagent_type="explore",
            description="Third launch",
            event=ToolExecutionStarted(tool_call_id="sub-a3"),
        )
    )
    running = _render(view)
    assert (
        running.index("First launch")
        < running.index("Second launch")
        < running.index("Third launch")
    )

    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a1",
            subagent_type="explore",
            description="First launch",
            event=ToolResult(tool_call_id="sub-a1", return_value=ToolOk(output="done")),
        )
    )
    thinking = _render(view)
    assert (
        thinking.index("First launch")
        < thinking.index("Second launch")
        < thinking.index("Third launch")
    )


def test_run_agents_parent_result_owns_terminal_rows_after_nested_activity(
    monkeypatch: pytest.MonkeyPatch,
):
    emitted: list[RenderableType] = []
    from pythinker_code.ui.shell.visualize import _live_view as live_view_module

    monkeypatch.setattr(
        live_view_module,
        "emit_scrollback_block",
        lambda _console, renderable: emitted.append(renderable),
    )
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_run_agents_call())
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a1",
            subagent_type="explore",
            description="Find TODO comments",
            event=_sub_tool_call("sub-a1", "Grep", '{"pattern":"TODO"}'),
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a1",
            subagent_type="explore",
            description="Find TODO comments",
            event=ToolExecutionStarted(tool_call_id="sub-a1"),
        )
    )
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a2",
            subagent_type="explore",
            description="Count files",
            event=_sub_tool_call("sub-a2", "Read", '{"file_path":"/repo/src/private.py"}'),
        )
    )

    view.dispatch_wire_message(
        ToolResult(
            tool_call_id="run-agents-1",
            return_value=ToolOk(
                output=(
                    "tool_status: success\n"
                    "mode: foreground\n"
                    "agent_count: 2\n"
                    "agents:\n"
                    "- name: todo_scan\n"
                    "  subagent_type: explore\n"
                    "  status: completed\n"
                    "- name: file_count\n"
                    "  subagent_type: explore\n"
                    "  status: completed\n"
                )
            ),
        )
    )

    assert len(emitted) == 1
    console = Console(width=100, record=True, highlight=False, color_system=None)
    console.print(emitted[0])
    output = console.export_text()
    assert output.count("Agents") == 1
    assert "2 agents completed" in output
    assert "waiting" not in output
    assert "running/background" not in output
    assert "thinking…" not in output
    assert "searching…" not in output
    assert "reading…" not in output


def test_run_agents_live_fallback_without_registry_renders_agents_tree():
    clear_tool_renderers()
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_run_agents_call())
    view.dispatch_wire_message(
        SubagentEvent(
            parent_tool_call_id="run-agents-1",
            agent_id="a1",
            subagent_type="explore",
            description="Find TODO comments",
            event=_sub_tool_call("sub-a1", "Grep", '{"pattern":"TODO"}'),
        )
    )

    output = _render(view, width=100)
    assert output.count("Agents") == 1
    assert "Find TODO comments" in output
    assert "RunAgents(" not in output


def test_run_agents_activity_tree_bounds_overflow_and_width():
    view = _LiveView(StatusUpdate(context_tokens=1000))
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(
        WireToolCall(
            id="run-agents-overflow",
            function=WireToolCall.FunctionBody(
                name="RunAgents",
                arguments=(
                    '{"summary":"many","agents":['
                    + ",".join(
                        f'{{"title":"Worker {i}","subagent_type":"explore","prompt":"hidden {i}"}}'
                        for i in range(8)
                    )
                    + "]}"
                ),
            ),
        )
    )
    for i in range(8):
        view.dispatch_wire_message(
            SubagentEvent(
                parent_tool_call_id="run-agents-overflow",
                agent_id=f"agent-{i}",
                subagent_type="explore",
                description=f"Worker {i}",
                event=_sub_tool_call(f"sub-{i}", "Read", '{"file_path":"/repo/hidden.py"}'),
            )
        )

    output = _render(view, width=40)
    assert output.count("Agents") == 1
    assert "more agents" in output
    assert "├─" in output
    assert "└─" in output
    assert "│  ⎿" in output or "   ⎿" in output
    assert "/repo/hidden.py" not in output
    assert "hidden 7" not in output
    for line in output.splitlines():
        assert cell_width(line) <= 40
