from __future__ import annotations

from rich.console import Console, Group, RenderableType
from rich.style import Style
from rich.text import Text

from pythinker_code.ui.shell.design_system import ShellTone, shell_style, status_icon
from pythinker_code.ui.shell.visualize._activity_tree import ActivityRow, render_activity_tree
from pythinker_code.ui.theme import tui_rich_style

_RUNNING_GLYPH = status_icon("running").plain


def _plain(renderable, *, width: int = 80) -> str:
    console = Console(record=True, width=width, color_system=None)
    console.print(renderable)
    return console.export_text()


def test_activity_tree_renders_compact_rows():
    output = _plain(
        render_activity_tree(
            [
                ActivityRow(label="explore", detail="Read _live_view.py", state="running"),
                ActivityRow(label="review", detail="Finished audit", state="completed"),
            ],
            width=80,
        )
    )
    assert "explore" in output
    assert "Read _live_view.py" in output
    assert "review" in output


def test_activity_tree_truncates_long_detail():
    output = _plain(
        render_activity_tree(
            [ActivityRow(label="explore", detail="x" * 120, state="running")],
            width=40,
        ),
        width=40,
    )
    assert all(len(row) <= 41 for row in output.splitlines() if row)


def test_running_row_marker_pulses_off_phase(monkeypatch):
    for flag in ("PYTHINKER_REDUCED_MOTION", "PYTHINKER_NO_ANIMATION", "PYTHINKER_STATIC_OUTPUT"):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    rows = [ActivityRow(label="explore", detail="Read _live_view.py", state="running")]
    on_phase = _plain(render_activity_tree(rows, width=80, now=0.0))
    off_phase = _plain(render_activity_tree(rows, width=80, now=0.8))
    assert _RUNNING_GLYPH in on_phase
    assert _RUNNING_GLYPH not in off_phase


def test_running_row_marker_static_under_reduced_motion(monkeypatch):
    monkeypatch.setenv("PYTHINKER_REDUCED_MOTION", "1")
    rows = [ActivityRow(label="explore", detail="Read _live_view.py", state="running")]
    on_phase = _plain(render_activity_tree(rows, width=80, now=0.0))
    off_phase = _plain(render_activity_tree(rows, width=80, now=0.8))
    assert _RUNNING_GLYPH in on_phase
    assert _RUNNING_GLYPH in off_phase


def test_non_running_row_marker_does_not_pulse(monkeypatch):
    for flag in ("PYTHINKER_REDUCED_MOTION", "PYTHINKER_NO_ANIMATION", "PYTHINKER_STATIC_OUTPUT"):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    completed_glyph = status_icon("completed").plain
    rows = [ActivityRow(label="review", detail="Finished audit", state="completed")]
    on_phase = _plain(render_activity_tree(rows, width=80, now=0.0))
    off_phase = _plain(render_activity_tree(rows, width=80, now=0.8))
    assert completed_glyph in on_phase
    assert completed_glyph in off_phase


def _detail_style(renderable: RenderableType, detail: str) -> Style:
    assert isinstance(renderable, Group)
    row = renderable.renderables[0]
    assert isinstance(row, Text)
    console = Console(color_system="truecolor")
    return row.get_style_at_offset(console, row.plain.index(detail))


def test_running_activity_detail_is_static_muted_text(monkeypatch):
    for flag in (
        "NO_COLOR",
        "PYTHINKER_REDUCED_MOTION",
        "PYTHINKER_NO_ANIMATION",
        "PYTHINKER_STATIC_OUTPUT",
    ):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    rows = [ActivityRow(label="agent", detail="Shell uv run pytest", state="running")]

    first = _detail_style(render_activity_tree(rows, width=80, now=0.88), "Shell")
    later = _detail_style(render_activity_tree(rows, width=80, now=1.18), "Shell")

    assert first.color == shell_style(ShellTone.MUTED).color
    assert later.color == shell_style(ShellTone.MUTED).color
    assert first.color != tui_rich_style("activity_verb").color
    assert later.color != tui_rich_style("activity_verb").color
