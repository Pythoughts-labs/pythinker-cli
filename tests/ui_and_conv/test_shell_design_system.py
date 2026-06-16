from __future__ import annotations

from rich.color import Color
from rich.console import Console
from rich.style import Style

from pythinker_code.ui.shell.components.render_utils import cell_width
from pythinker_code.ui.shell.design_system import (
    ShellTone,
    dialog_title,
    keyboard_hint,
    render_row,
    render_segment_line,
    shell_style,
    status_icon,
)
from pythinker_code.ui.shell.glyphs import QUESTION_MARKER


def _plain(renderable, *, width: int = 80) -> str:
    console = Console(record=True, width=width, color_system=None)
    console.print(renderable)
    return console.export_text()


def _color_hex(style: Style) -> str:
    color: Color | None = style.color
    assert color is not None
    triplet = color.triplet
    assert triplet is not None
    return triplet.hex.lower()


def test_status_icon_names_are_stable():
    assert status_icon("running").plain == "●"
    assert status_icon("completed").plain == "✓"
    assert status_icon("failed").plain == "✘"
    assert status_icon("denied").plain == "×"
    assert status_icon("interrupted").plain == "■"
    assert status_icon("waiting").plain == "○"
    assert status_icon("question").plain == QUESTION_MARKER
    assert status_icon("approval").plain == QUESTION_MARKER


def test_running_and_failed_status_icons_use_expected_tones():
    assert status_icon("running").style == shell_style(ShellTone.MUTED)
    assert status_icon("completed").style == shell_style(ShellTone.SUCCESS)
    assert status_icon("failed").style == shell_style(ShellTone.ERROR)


def test_keyboard_hint_uses_key_and_label():
    output = _plain(keyboard_hint("esc", "interrupt"))
    assert "esc" in output
    assert "interrupt" in output


def test_segment_line_hides_right_segments_before_wrapping():
    line = render_segment_line(
        left=["Pythinker Code", "insert"],
        right=["very-long-context-value", "shift+up/down agents"],
        width=32,
        tone=ShellTone.MUTED,
    )
    output = _plain(line, width=32)
    assert "Pythinker Code" in output
    assert all(cell_width(row) <= 32 for row in output.splitlines() if row)


def test_dialog_title_includes_icon_and_title():
    output = _plain(dialog_title("approval", "Run shell command"))
    assert "Run shell command" in output


def test_render_row_combines_icon_and_content():
    output = _plain(render_row(status_icon("completed"), "done"))
    assert "✓" in output
    assert "done" in output


def test_shell_style_resolves_brand_tokens_and_switches_theme():
    from pythinker_code.ui.theme import set_active_theme

    set_active_theme("dark")
    assert _color_hex(shell_style(ShellTone.ACCENT)) == "#a9b4ff"
    assert _color_hex(shell_style(ShellTone.SUCCESS)) == "#7ccf8a"
    set_active_theme("light")
    assert _color_hex(shell_style(ShellTone.ACCENT)) == "#0b114e"
    set_active_theme("dark")


def test_verb_spinner_stays_champagne_independent_of_accent_token():
    from pythinker_code.ui.shell.motion import _SHIMMER_BASE, verb_spinner_style
    from pythinker_code.ui.theme import set_active_theme

    set_active_theme("dark")
    assert _color_hex(verb_spinner_style()) == _SHIMMER_BASE.lower()
