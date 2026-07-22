from __future__ import annotations

import asyncio

from pythinker_core.message import ToolCall
from pythinker_core.tooling import ToolResult, ToolReturnValue
from rich.console import Console, Group
from rich.padding import Padding

from pythinker_code.tools.display import TodoDisplayBlock, TodoDisplayItem
from pythinker_code.ui.shell.components.markdown import PythinkerMarkdown
from pythinker_code.ui.shell.console import console as shell_console
from pythinker_code.ui.shell.keyboard import KeyEvent
from pythinker_code.ui.shell.visualize import _live_view as live_view_module
from pythinker_code.ui.shell.visualize import _LiveView, _PromptLiveView
from pythinker_code.wire.types import (
    CompactionBegin,
    HookOutput,
    HookResolved,
    HookTriggered,
    Notification,
    StatusUpdate,
    TextPart,
    TurnBegin,
    TurnEnd,
)


def _render(renderable) -> str:
    console = Console(width=100, record=True, highlight=False)
    console.print(renderable)
    return console.export_text()


def _notification(index: int = 1, *, source_kind: str = "background_task") -> Notification:
    return Notification(
        id=f"n{index:07d}",
        category="task",
        type="task.completed",
        source_kind=source_kind,
        source_id=f"b{index:07d}",
        title=f"Background task completed: build project {index}",
        body=(f"Task ID: b{index:07d}\nStatus: completed\nDescription: build project {index}"),
        severity="success",
        created_at=123.456,
        payload={"task_id": f"b{index:07d}"},
    )


def test_live_view_renders_notification_block():
    view = _LiveView(StatusUpdate())

    view.dispatch_wire_message(_notification())

    rendered = _render(view.compose())
    assert "Background task completed: build project 1" in rendered
    assert "Task ID: b0000001" in rendered
    assert "Status: completed" in rendered
    assert "..." in rendered


def test_live_view_renders_running_hook_block():
    view = _LiveView(StatusUpdate())

    view.dispatch_wire_message(HookTriggered(event="PreToolUse", target="Shell", hook_count=1))

    rendered = _render(view.compose_agent_output(include_working_indicator=False))
    assert "Hook" in rendered
    assert "PreToolUse Shell" in rendered
    assert "running" in rendered.lower()


def test_live_view_prints_resolved_blocking_hook(monkeypatch):
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(HookTriggered(event="PreToolUse", target="Shell", hook_count=1))
    printed = []
    monkeypatch.setattr(shell_console, "print", lambda *args, **kwargs: printed.extend(args))

    view.dispatch_wire_message(
        HookResolved(
            event="PreToolUse",
            target="Shell",
            action="block",
            reason="blocked by policy",
            duration_ms=12,
        )
    )

    assert view._hook_blocks == {}
    rendered = "\n".join(_render(item) for item in printed)
    assert "Hook" in rendered
    assert "PreToolUse Shell" in rendered
    assert "failed" in rendered.lower()
    assert "blocked by policy" in rendered
    assert "12ms" in rendered


def test_live_view_prints_resolved_hook_stdout(monkeypatch):
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(HookTriggered(event="PreToolUse", target="Shell", hook_count=1))
    printed = []
    monkeypatch.setattr(shell_console, "print", lambda *args, **kwargs: printed.extend(args))

    view.dispatch_wire_message(
        HookResolved(
            event="PreToolUse",
            target="Shell",
            action="allow",
            duration_ms=7,
            outputs=(
                HookOutput(
                    stdout=(
                        "[PreToolUse]\n"
                        "╔════════════════════════════════════════════════════════════════╗\n"
                        "║  🤖 PRO AGENT ACTIVATION RECOMMENDED                    ║\n"
                        "╚════════════════════════════════════════════════════════════════╝\n"
                    )
                ),
            ),
        )
    )

    rendered = "\n".join(_render(item) for item in printed)
    assert "Hook" in rendered
    assert "PreToolUse Shell" in rendered
    assert "[PreToolUse]" in rendered
    assert "PRO AGENT ACTIVATION RECOMMENDED" in rendered


def test_live_view_prints_hook_timeout_status_with_partial_output(monkeypatch):
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(HookTriggered(event="PreToolUse", target="Shell", hook_count=1))
    printed = []
    monkeypatch.setattr(shell_console, "print", lambda *args, **_kwargs: printed.extend(args))

    view.dispatch_wire_message(
        HookResolved(
            event="PreToolUse",
            target="Shell",
            action="allow",
            outputs=(HookOutput(stdout="partial output", timed_out=True),),
        )
    )

    rendered = "\n".join(_render(item) for item in printed)
    assert "partial output" in rendered
    assert "hook timed out" in rendered


def test_working_indicator_uses_turn_elapsed_time(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(live_view_module.time, "monotonic", lambda: now)
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="scan"))

    now = 1012.0
    rendered = _render(view._working_indicator())

    assert "12s" in rendered
    assert "4h" not in rendered


def test_working_indicator_exposes_escape_when_turn_is_cancellable():
    view = _LiveView(StatusUpdate(), asyncio.Event())
    view.dispatch_wire_message(TurnBegin(user_input="scan"))

    rendered = _render(view._working_indicator())

    assert "esc)" in rendered


def test_working_indicator_uses_rotating_thinking_words(monkeypatch):
    now = 90.0
    monkeypatch.setattr(live_view_module.time, "monotonic", lambda: now)
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="scan"))

    rendered = _render(view._working_indicator())

    assert "Accomplishing…" in rendered
    assert "Working" not in rendered


def test_working_indicator_rotates_thinking_words_every_ten_minutes(monkeypatch):
    now = 0.0
    monkeypatch.setattr(live_view_module.time, "monotonic", lambda: now)
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="scan"))

    first = _render(view._working_indicator())
    now = 599.9
    still_first = _render(view._working_indicator())
    now = 600.0
    second = _render(view._working_indicator())

    assert "Accomplishing…" in first
    assert "Accomplishing…" in still_first
    assert "Actioning…" in second


def _todo_tool_call() -> ToolCall:
    return ToolCall(
        id="todo-1",
        function=ToolCall.FunctionBody(name="SetTodoList", arguments='{"todos": []}'),
    )


def _todo_tool_result(items: list[TodoDisplayItem]) -> ToolResult:
    return ToolResult(
        tool_call_id="todo-1",
        return_value=ToolReturnValue(
            is_error=False,
            output="Todo list updated",
            message="Todo list updated",
            display=[TodoDisplayBlock(items=items)],
        ),
    )


def test_working_indicator_pins_todos_under_spinner(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(live_view_module.time, "monotonic", lambda: now)
    # Pin the animated braille marker to its static dot so this
    # structure-focused assertion does not depend on the animation phase.
    monkeypatch.setenv("PYTHINKER_REDUCED_MOTION", "1")
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_todo_tool_call())
    assert view.compose_agent_output(include_working_indicator=False) == []
    view.dispatch_wire_message(
        _todo_tool_result(
            [
                TodoDisplayItem(title="Gather requirements", status="done"),
                TodoDisplayItem(title="Inspect UI", status="done"),
                TodoDisplayItem(
                    title="Explore project context — blogs page and image components",
                    status="in_progress",
                ),
                TodoDisplayItem(title="Ask clarifying questions one at a time", status="pending"),
                TodoDisplayItem(title="Propose 2–3 approaches with trade-offs", status="pending"),
                TodoDisplayItem(title="Present design and get user approval", status="pending"),
                TodoDisplayItem(title="Write design doc and self-review spec", status="pending"),
                TodoDisplayItem(title="Run final checks", status="pending"),
            ]
        )
    )
    assert view.compose_agent_output(include_working_indicator=False) == []

    now = 1060.0
    rendered = _render(view._working_indicator())

    assert "● Explore project context — blogs page and image components… (1m 0s)" in rendered
    # Active todo now appears both in the spinner header and the pinned list;
    # done todos sort to the bottom and are dropped by the 5-row cap when
    # active + pending already fill the rows.
    assert "⎿    ■ Explore project context" in rendered
    assert rendered.count("Explore project context") == 2
    assert "□ Ask clarifying questions one at a time" in rendered
    assert "□ Propose 2–3 approaches with trade-offs" in rendered
    assert "… +3 more" in rendered
    assert "todos(" not in rendered
    assert "Tip:" not in rendered


def test_working_indicator_tip_wraps_with_hanging_indent(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(live_view_module.time, "monotonic", lambda: now)
    long_tip = (
        "Type /login to configure providers and /model to switch models quickly "
        "without leaving the shell."
    )
    monkeypatch.setattr(live_view_module, "current_tip", lambda _now: long_tip)
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view._turn_start_time = now - 10.0

    console = Console(width=36, record=True, highlight=False)
    console.print(view._working_indicator())
    lines = [line.rstrip() for line in console.export_text().splitlines() if line.strip()]

    assert any("Tip:" in line for line in lines)
    assert len(lines) >= 2
    for line in lines[1:]:
        assert line.startswith(" "), f"wrapped tip line lost hanging indent: {line!r}"


def test_working_indicator_keeps_done_todos_pinned(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(live_view_module.time, "monotonic", lambda: now)
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_todo_tool_call())
    view.dispatch_wire_message(
        _todo_tool_result([TodoDisplayItem(title="Done task", status="done")])
    )

    rendered = _render(view._working_indicator())

    assert "todos(" not in rendered
    assert "⎿    ✓ Done task" in rendered
    assert "■" not in rendered
    assert "□" not in rendered


def test_ctrl_t_toggles_pinned_todos(monkeypatch):
    now = 1000.0
    monkeypatch.setattr(live_view_module.time, "monotonic", lambda: now)
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view.dispatch_wire_message(_todo_tool_call())
    view.dispatch_wire_message(
        _todo_tool_result([TodoDisplayItem(title="Pinned task", status="pending")])
    )

    assert "Pinned task" in _render(view._working_indicator())

    view.dispatch_keyboard_event(KeyEvent.CTRL_T)
    assert "Pinned task" not in _render(view._working_indicator())

    view.dispatch_keyboard_event(KeyEvent.CTRL_T)
    assert "Pinned task" in _render(view._working_indicator())


def test_prompt_live_view_suppresses_background_task_notifications(monkeypatch):
    view = object.__new__(_PromptLiveView)
    view._pending_local_steer_count = 0
    view._btw_spinner = None

    forwarded: list[Notification] = []
    monkeypatch.setattr(
        _LiveView, "dispatch_wire_message", lambda _self, msg: forwarded.append(msg)
    )

    view.dispatch_wire_message(_notification())

    assert forwarded == []


def test_prompt_live_view_keeps_non_background_task_notifications(monkeypatch):
    view = object.__new__(_PromptLiveView)
    view._pending_local_steer_count = 0
    view._btw_spinner = None

    forwarded: list[Notification] = []
    monkeypatch.setattr(
        _LiveView, "dispatch_wire_message", lambda _self, msg: forwarded.append(msg)
    )

    notification = _notification(source_kind="system")
    view.dispatch_wire_message(notification)

    assert forwarded == [notification]


def test_live_view_prints_turn_recap_when_enabled(monkeypatch):
    printed = []

    def fake_print(*args, **_kwargs):
        printed.append(args[0] if args else None)

    monkeypatch.setattr(live_view_module.console, "print", fake_print)

    view = _LiveView(StatusUpdate(), show_turn_recaps=True)
    view.dispatch_wire_message(TurnBegin(user_input="implement recaps"))
    view.dispatch_wire_message(TextPart(text="Implemented a "))
    view.dispatch_wire_message(TextPart(text="/recap command."))
    view.dispatch_wire_message(TurnEnd())
    view.cleanup(is_interrupt=False)

    plain = "\n".join(_render(item) for item in printed if item is not None)
    assert "※ recap: Implemented a /recap command." in plain
    assert "Implemented a  /recap" not in plain
    assert "disable recaps in /config" in plain

    recap_index = next(
        index
        for index, item in enumerate(printed)
        if item is not None and "※ recap:" in _render(item)
    )
    # The recap is padded to the card inset; the renderable inside is markdown.
    recap_item = printed[recap_index]
    assert isinstance(recap_item, Padding)
    assert isinstance(recap_item.renderable, PythinkerMarkdown)
    assert printed[recap_index - 1] is None
    assert printed[recap_index + 1] is None


def test_cleanup_flushes_notifications_to_terminal_history(monkeypatch):
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(_notification())

    printed = []
    monkeypatch.setattr(shell_console, "print", lambda *args, **kwargs: printed.extend(args))

    view.cleanup(is_interrupt=False)

    assert not view._notification_blocks
    assert not view._live_notification_blocks
    assert printed
    rendered = _render(printed[0])
    assert "Background task completed: build project 1" in rendered
    assert "Task ID: b0000001" in rendered


def test_cleanup_flushes_all_notifications_even_when_live_view_shows_only_latest_four(monkeypatch):
    view = _LiveView(StatusUpdate())
    for index in range(1, 6):
        view.dispatch_wire_message(_notification(index))

    live_rendered = _render(view.compose())
    assert "Background task completed: build project 1" not in live_rendered
    for index in range(2, 6):
        assert f"Background task completed: build project {index}" in live_rendered

    printed = []
    monkeypatch.setattr(shell_console, "print", lambda *args, **kwargs: printed.extend(args))

    view.cleanup(is_interrupt=False)

    assert len(printed) == 5
    rendered = "\n".join(_render(item) for item in printed)
    for index in range(1, 6):
        assert f"Background task completed: build project {index}" in rendered


def test_compaction_status_keeps_one_blank_row_above_live_block():
    view = _LiveView(StatusUpdate(context_tokens=221_300))

    view.dispatch_wire_message(CompactionBegin())

    rendered = _render(Group(*view.compose_agent_output(include_working_indicator=False)))
    assert rendered.startswith("\n· Compacting conversation…")


def test_compose_inserts_gap_under_agent_output_before_nonempty_status():
    """Non-interactive compose() puts one blank row under the spinner verb
    before a non-empty status line (the under-gap), and the status line stays last."""
    from rich.console import Group
    from rich.text import Text

    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    view._status_block.update(
        StatusUpdate(context_usage=0.5, context_tokens=1000, max_context_tokens=2000)
    )
    assert view._status_block.text.plain.strip()  # status line has content

    group = view.compose()
    assert isinstance(group, Group)
    renderables = list(group.renderables)
    assert renderables[-1] is view._status_block.text
    assert isinstance(renderables[-2], Text)
    assert renderables[-2].plain == ""  # blank row separating spinner verb from status


def test_compose_no_double_gap_when_status_empty():
    """When the status line is empty (renders as its own blank row) compose() does
    not insert an extra spacer, avoiding a double blank under the spinner verb."""
    from rich.console import Group

    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="scan"))
    assert view._status_block.text.plain == ""  # empty status

    group = view.compose()
    assert isinstance(group, Group)
    renderables = list(group.renderables)
    # status (empty Text) is last; the row before it is the agent output tail,
    # not an inserted blank spacer pair.
    assert renderables[-1] is view._status_block.text
