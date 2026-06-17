from __future__ import annotations

import importlib

import pytest
from pythinker_core.message import ToolCall
from pythinker_core.tooling import ToolResult, ToolReturnValue
from rich.color import Color
from rich.console import Console, Group
from rich.style import Style

from pythinker_code.soul.live_tokens import add_total_output_tokens, reset_for_tests
from pythinker_code.tools.display import DiffDisplayBlock, TodoDisplayBlock, TodoDisplayItem
from pythinker_code.ui.shell.motion import (
    _SHIMMER_BASE,
    _SHIMMER_HIGHLIGHT,
    _SHIMMER_MID,
)
from pythinker_code.ui.shell.visualize import _LiveView
from pythinker_code.ui.theme import set_active_theme, tui_rich_style
from pythinker_code.wire.types import StatusUpdate, TurnBegin

_live_view_module = importlib.import_module("pythinker_code.ui.shell.visualize._live_view")


@pytest.fixture(autouse=True)
def _reset_live_tokens():
    """Isolate the session-wide live token accumulator between tests."""
    reset_for_tests()
    yield
    reset_for_tests()


_SHIMMER_HEXES = {_SHIMMER_BASE.lower(), _SHIMMER_MID.lower(), _SHIMMER_HIGHLIGHT.lower()}


def _render(renderable) -> str:
    console = Console(width=100, record=True, highlight=False)
    console.print(renderable)
    return console.export_text()


def _style_for(renderable, text: str) -> Style:
    start = renderable.plain.index(text)
    end = start + len(text)
    span = next(span for span in renderable.spans if span.start <= start and span.end >= end)
    return Style.parse(span.style) if isinstance(span.style, str) else span.style


def _color_hex(color: Color | None) -> str:
    assert color is not None
    triplet = color.triplet
    assert triplet is not None
    return triplet.hex.lower()


def _span_colors_for(renderable, text: str) -> set[str]:
    start = renderable.plain.index(text)
    end = start + len(text)
    colors: set[str] = set()
    for span in renderable.spans:
        if span.end <= start or span.start >= end:
            continue
        style = Style.parse(span.style) if isinstance(span.style, str) else span.style
        if style.color is not None:
            colors.add(_color_hex(style.color))
    return colors


def _todo_call(call_id: str = "todo-1") -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(
            name="SetTodoList",
            arguments='{"todos":[{"title":"Implement pinned todos","status":"in_progress"}]}',
        ),
    )


def _todo_result(call_id: str = "todo-1") -> ToolResult:
    return ToolResult(
        tool_call_id=call_id,
        return_value=ToolReturnValue(
            is_error=False,
            output="Todo list updated",
            message="Todo list updated",
            display=[
                TodoDisplayBlock(
                    items=[
                        TodoDisplayItem(title="Implement pinned todos", status="in_progress"),
                        TodoDisplayItem(title="Explore UI", status="done"),
                        TodoDisplayItem(title="Ask question", status="done"),
                        TodoDisplayItem(title="Sketch behavior", status="done"),
                        TodoDisplayItem(title="Write tests", status="done"),
                        TodoDisplayItem(title="Run checks", status="done"),
                    ]
                )
            ],
        ),
    )


def test_todo_update_pins_current_task_under_activity_line(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(_live_view_module.time, "monotonic", lambda: now)
    # Pin the animated braille marker to its static dot for a deterministic
    # assertion on the activity-line content.
    monkeypatch.setenv("PYTHINKER_REDUCED_MOTION", "1")
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="work"))
    add_total_output_tokens(10_000)  # output produced this turn (main + subagents)
    view.dispatch_wire_message(_todo_call())
    view.dispatch_wire_message(_todo_result())

    now = 1460.0
    rendered = _render(view._working_indicator())

    assert "● Implement pinned todos… (7m 40s, ↓ 10k tokens)" in rendered
    assert rendered.count("Implement pinned todos") == 2
    assert "⎿    ■ Implement pinned todos" in rendered
    assert "✓ Explore UI" in rendered
    assert "✓ Write tests" in rendered
    assert "… +1 completed" in rendered
    assert "todos(" not in rendered
    assert "Accomplishing" not in rendered


def test_active_todo_activity_line_does_not_alternate_with_spinner_verb(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(_live_view_module.time, "monotonic", lambda: now)
    monkeypatch.setenv("PYTHINKER_REDUCED_MOTION", "1")
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="work"))
    add_total_output_tokens(10_000)  # output produced this turn (main + subagents)
    view.dispatch_wire_message(_todo_call())
    view.dispatch_wire_message(_todo_result())

    now = 1465.0
    rendered = _render(view._working_indicator())

    assert "● Implement pinned todos… (7m 45s, ↓ 10k tokens)" in rendered
    assert _live_view_module.spinner_message(now) not in rendered
    assert "⎿    ■ Implement pinned todos" in rendered
    assert "✓ Explore UI" in rendered


def test_spinner_verb_shows_until_next_todo_becomes_active(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(_live_view_module.time, "monotonic", lambda: now)
    monkeypatch.setenv("PYTHINKER_REDUCED_MOTION", "1")
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="work"))
    add_total_output_tokens(10_000)  # output produced this turn (main + subagents)
    view._latest_todos = (
        TodoDisplayItem(title="Finished task", status="done"),
        TodoDisplayItem(title="Next task", status="pending"),
    )

    now = 1465.0
    rendered = _render(view._working_indicator())

    assert f"● {_live_view_module.spinner_message(now)} (7m 45s, ↓ 10k tokens)" in rendered
    assert "⎿    □ Next task" in rendered
    assert "✓ Finished task" in rendered


def test_finished_todos_move_to_bottom_of_menu(monkeypatch) -> None:
    now = 1000.0
    monkeypatch.setattr(_live_view_module.time, "monotonic", lambda: now)
    monkeypatch.setenv("PYTHINKER_REDUCED_MOTION", "1")
    view = _LiveView(StatusUpdate(context_tokens=10_000))
    view.dispatch_wire_message(TurnBegin(user_input="work"))
    view._latest_todos = (
        TodoDisplayItem(title="Active task", status="in_progress"),
        TodoDisplayItem(title="Finished first", status="done"),
        TodoDisplayItem(title="Pending first", status="pending"),
        TodoDisplayItem(title="Finished second", status="done"),
        TodoDisplayItem(title="Pending second", status="pending"),
    )

    rendered = _render(view._working_indicator())

    assert rendered.index("■ Active task") < rendered.index("□ Pending first")
    assert rendered.index("□ Pending first") < rendered.index("□ Pending second")
    assert rendered.index("□ Pending second") < rendered.index("✓ Finished first")
    assert rendered.index("✓ Finished first") < rendered.index("✓ Finished second")


def test_todo_activity_line_uses_standard_spinner_shimmer_for_generic_verbs(monkeypatch) -> None:
    # Pin the discrete three-step sheen (256-color tier) for determinism.
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    set_active_theme("dark")
    view = _LiveView(StatusUpdate(context_tokens=10_000))

    line = view._todo_activity_line("Implement pinned todos", elapsed_s=0.88, width=100)

    marker_style = Style.parse(line.style) if isinstance(line.style, str) else line.style
    assert marker_style.color == tui_rich_style("activity_spinner").color
    assert _span_colors_for(line, "Implement pinned todos") >= _SHIMMER_HEXES


def test_active_todo_activity_line_uses_stable_label_not_shimmer() -> None:
    set_active_theme("dark")
    view = _LiveView(StatusUpdate(context_tokens=10_000))

    line = view._todo_activity_line(
        "Implement pinned todos", elapsed_s=0.88, width=100, shimmer_label=False
    )
    later = view._todo_activity_line(
        "Implement pinned todos", elapsed_s=1.18, width=100, shimmer_label=False
    )

    # Stable single coral tone (matches the in-progress ■ boxes), no animation.
    active_color = _color_hex(tui_rich_style("activity_verb").color)
    marker_style = Style.parse(line.style) if isinstance(line.style, str) else line.style
    assert marker_style.color == tui_rich_style("activity_spinner").color
    assert _span_colors_for(line, "Implement pinned todos") == {active_color}
    assert _span_colors_for(later, "Implement pinned todos") == {active_color}


def test_active_pinned_todo_row_uses_neutral_title_not_shimmer() -> None:
    set_active_theme("dark")
    view = _LiveView(StatusUpdate())

    row = view._pinned_todo_row(
        TodoDisplayItem(title="Implement pinned todos", status="in_progress"),
        is_first=True,
        width=100,
        elapsed_s=0.88,
    )

    # Reference design: coral box; title uses explicit primary text on dark.
    coral = _color_hex(tui_rich_style("activity_verb").color)
    primary = _color_hex(tui_rich_style("text").color)
    assert _span_colors_for(row, "■") == {coral}
    assert _span_colors_for(row, "Implement pinned todos") == {primary}


def test_secondary_in_progress_todo_rows_use_light_grey() -> None:
    set_active_theme("dark")
    view = _LiveView(StatusUpdate())

    row = view._pinned_todo_row(
        TodoDisplayItem(title="Deep code review on diff", status="in_progress"),
        is_first=False,
        width=100,
        elapsed_s=0.88,
    )

    # Every in-progress row shares the same design: coral box, primary text title.
    coral = _color_hex(tui_rich_style("activity_verb").color)
    primary = _color_hex(tui_rich_style("text").color)
    assert _span_colors_for(row, "■") == {coral}
    assert _span_colors_for(row, "Deep code review on diff") == {primary}


def test_pinned_todo_rows_align_icons_and_titles() -> None:
    view = _LiveView(StatusUpdate())

    first = view._pinned_todo_row(
        TodoDisplayItem(title="Lead task", status="in_progress"),
        is_first=True,
        width=100,
        elapsed_s=0.0,
    )
    later = view._pinned_todo_row(
        TodoDisplayItem(title="Next task", status="pending"),
        is_first=False,
        width=100,
    )

    # First row carries the ⎿ gutter; later rows omit it but keep the same
    # checkbox and title columns.
    assert first.plain.startswith("  ⎿    ■ ")
    assert later.plain.startswith("       □ ")
    assert later.plain.index("□") == first.plain.index("■")
    assert later.plain.index("Next task") == first.plain.index("Lead task")


def test_successful_todo_tool_card_is_suppressed() -> None:
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="work"))
    view.dispatch_wire_message(_todo_call())
    assert len(view._tool_call_blocks) == 1

    view.dispatch_wire_message(_todo_result())

    assert view._tool_call_blocks == {}
    rendered = _render(Group(*view.compose_agent_output()))
    assert "Implement pinned todos" in rendered
    assert "SetTodoList" not in rendered


def test_completed_todo_row_is_muted_and_struck() -> None:
    view = _LiveView(StatusUpdate())

    row = view._pinned_todo_row(
        TodoDisplayItem(title="Finished task", status="done"), is_first=True, width=80
    )
    title_style = _style_for(row, "Finished task")

    # Reference design: green check, muted struck title.
    assert _span_colors_for(row, "✓") == {_color_hex(tui_rich_style("success").color)}
    assert title_style.strike is True
    assert title_style.color == tui_rich_style("muted").color


def test_cancelled_todo_row_is_muted_and_struck() -> None:
    view = _LiveView(StatusUpdate())

    row = view._pinned_todo_row(
        TodoDisplayItem(title="Abandoned task", status="cancelled"), is_first=True, width=80
    )
    title_style = _style_for(row, "Abandoned task")

    assert _span_colors_for(row, "✕") == {_color_hex(tui_rich_style("muted").color)}
    assert title_style.strike is True
    assert title_style.color == tui_rich_style("muted").color


def test_cancelled_todo_remains_visible_in_pinned_list(monkeypatch) -> None:
    """A cancelled todo must stay represented in the pinned list (with the ✕ marker)
    rather than silently disappearing — successful todo tool cards are suppressed in
    the transcript, so the pinned list is the only place a cancelled item shows."""
    monkeypatch.setenv("PYTHINKER_REDUCED_MOTION", "1")
    view = _LiveView(StatusUpdate(context_tokens=10_000))
    view.dispatch_wire_message(TurnBegin(user_input="work"))
    view._latest_todos = (
        TodoDisplayItem(title="Active task", status="in_progress"),
        TodoDisplayItem(title="Abandoned task", status="cancelled"),
    )

    rendered = _render(view._working_indicator())

    assert "✕ Abandoned task" in rendered


def test_toggle_pinned_todos_hides_todo_rows() -> None:
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="work"))
    view.dispatch_wire_message(_todo_call())
    view.dispatch_wire_message(_todo_result())

    assert view.toggle_pinned_todos() is False

    rendered = _render(view._working_indicator())
    assert "Implement pinned todos" not in rendered
    assert "…" in rendered


def test_turn_recap_tracks_and_clears_modified_files() -> None:
    view = _LiveView(StatusUpdate())
    result = ToolResult(
        tool_call_id="1",
        return_value=ToolReturnValue(
            is_error=False,
            output="ok",
            message="ok",
            display=[
                DiffDisplayBlock(
                    path="src/a.py", old_text="", new_text="x", old_start=1, new_start=1
                )
            ],
        ),
    )

    view._track_recap_modified_files(result)
    view._track_recap_modified_files(result)  # idempotent — backed by a set
    assert view._recap_files_modified == {"src/a.py"}

    # A fresh top-level turn resets the per-turn delta state.
    view.dispatch_wire_message(TurnBegin(user_input="next ask"))
    assert view._recap_files_modified == set()


def test_pinned_todo_row_has_no_muted_base_style_bleed() -> None:
    """The row must not carry a base style: a muted base would bleed through
    the bold running-task title (which sets no color) and render it grey."""
    set_active_theme("dark")
    view = _LiveView(StatusUpdate())

    row = view._pinned_todo_row(
        TodoDisplayItem(title="Active task", status="in_progress"),
        is_first=True,
        width=100,
        elapsed_s=0.0,
    )
    assert not row.style
