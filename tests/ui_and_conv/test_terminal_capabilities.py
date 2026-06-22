from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import TextIO, cast

from pythinker_code.ui.terminal_capabilities import (
    ascii_glyphs_enabled,
    color_depth,
    colors_disabled,
    motion_disabled,
)


def test_color_capability_honors_standard_env_vars() -> None:
    assert colors_disabled({"NO_COLOR": "1"})
    assert colors_disabled({"TERM": "dumb"})
    assert colors_disabled({"CLICOLOR": "0"})
    assert colors_disabled({"PYTHINKER_NO_COLOR": "true"})
    assert not colors_disabled({"TERM": "xterm-256color"})


def test_ascii_glyphs_are_opt_in_or_minimal_terminal() -> None:
    assert ascii_glyphs_enabled({"PYTHINKER_TUI_GLYPHS": "ascii"})
    assert ascii_glyphs_enabled({"PYTHINKER_ASCII_UI": "1"})
    assert ascii_glyphs_enabled({"TERM": "dumb"})
    legacy_stdout = cast(TextIO, SimpleNamespace(encoding="cp1252"))
    assert ascii_glyphs_enabled({"TERM": "xterm"}, stdout=legacy_stdout)
    assert not ascii_glyphs_enabled({"PYTHINKER_TUI_GLYPHS": "unicode", "TERM": "dumb"})


def test_motion_capability_honors_static_output_env_vars() -> None:
    assert motion_disabled({"TERM": "dumb"})
    assert motion_disabled({"PYTHINKER_REDUCED_MOTION": "1"})
    assert motion_disabled({"PYTHINKER_NO_ANIMATION": "true"})
    assert motion_disabled({"PYTHINKER_STATIC_OUTPUT": "yes"})
    assert not motion_disabled({"TERM": "xterm-256color"})


def test_color_depth_three_tiers() -> None:
    assert color_depth({"NO_COLOR": "1"}) == "none"
    assert color_depth({"TERM": "dumb"}) == "none"
    assert color_depth({"COLORTERM": "truecolor", "TERM": "xterm-256color"}) == "truecolor"
    assert color_depth({"COLORTERM": "24bit"}) == "truecolor"
    assert color_depth({"TERM": "xterm-direct"}) == "truecolor"
    assert color_depth({"TERM": "xterm-256color"}) == "256"
    assert color_depth({"TERM": "xterm"}) == "16"
    assert color_depth({}) == "16"


def test_color_depth_force_color_levels_win() -> None:
    assert color_depth({"FORCE_COLOR": "3", "TERM": "xterm"}) == "truecolor"
    assert color_depth({"FORCE_COLOR": "2", "COLORTERM": "truecolor"}) == "256"
    assert color_depth({"FORCE_COLOR": "1", "COLORTERM": "truecolor"}) == "16"
    # Non-level FORCE_COLOR values defer to the remaining detection chain.
    assert color_depth({"FORCE_COLOR": "true", "TERM": "xterm-256color"}) == "256"


def test_color_depth_windows_terminal_promotion() -> None:
    assert color_depth({"WT_SESSION": "guid", "TERM": "xterm"}) == "truecolor"
    # An explicit FORCE_COLOR level overrides the promotion.
    assert color_depth({"WT_SESSION": "guid", "FORCE_COLOR": "2"}) == "256"


def test_color_depth_vscode_family_promotion() -> None:
    # VS Code-family integrated terminals (TERM_PROGRAM=vscode, including forks)
    # are truecolor-capable even when they don't advertise COLORTERM and TERM is
    # conservative — promote them so diff tints don't fall to the 16-color path.
    assert color_depth({"TERM_PROGRAM": "vscode", "TERM": "xterm-256color"}) == "truecolor"
    assert color_depth({"TERM_PROGRAM": "vscode", "TERM": "xterm"}) == "truecolor"
    # Disabled color and explicit FORCE_COLOR downgrades still win over the promotion.
    assert color_depth({"TERM_PROGRAM": "vscode", "NO_COLOR": "1"}) == "none"
    assert color_depth({"TERM_PROGRAM": "vscode", "FORCE_COLOR": "2"}) == "256"


def test_diff_colors_fall_back_to_foregrounds_on_16_color(monkeypatch) -> None:
    from pythinker_code.ui.theme import get_diff_colors

    for var in (
        "NO_COLOR",
        "PYTHINKER_NO_COLOR",
        "CLICOLOR",
        "COLORTERM",
        "WT_SESSION",
        "TERM_PROGRAM",
        "FORCE_COLOR",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm")

    colors = get_diff_colors()
    assert colors.add_bg.bgcolor is None
    assert colors.del_bg.bgcolor is None
    assert colors.add_bg.color is not None and colors.add_bg.color.name == "green"
    assert colors.del_bg.color is not None and colors.del_bg.color.name == "red"


def test_theme_resolvers_strip_colors_when_no_color_is_set(monkeypatch) -> None:
    from pythinker_code.ui.theme import (
        _strip_ptk_colors,
        get_diff_colors,
        get_toolbar_colors,
        markdown_rich_style,
        tui_rich_style,
    )

    monkeypatch.setenv("NO_COLOR", "1")

    assert tui_rich_style("accent").color is None
    assert tui_rich_style("tool_error_bg").bgcolor is None
    assert markdown_rich_style("link").color is None
    assert get_diff_colors().add_bg.bgcolor is None
    assert get_toolbar_colors().tip_key == "bold"
    assert _strip_ptk_colors("bg:#112233 fg:#abcdef bold italic") == "bold italic"


def test_ascii_glyph_mode_uses_plain_fallbacks(monkeypatch) -> None:
    import pythinker_code.ui.shell.glyphs as glyphs

    monkeypatch.setenv("PYTHINKER_TUI_GLYPHS", "ascii")
    ascii_glyphs = importlib.reload(glyphs)
    try:
        assert ascii_glyphs.SPINNER_FRAMES == ("-", "\\", "|", "/")
        assert ascii_glyphs.REDUCED_MOTION_GLYPH == "*"
        assert ascii_glyphs.TRANSCRIPT_PROMPT_MARKER == ">"
        assert ascii_glyphs.TRANSCRIPT_TOOL_GUTTER == "|"
    finally:
        monkeypatch.delenv("PYTHINKER_TUI_GLYPHS", raising=False)
        importlib.reload(glyphs)


def test_term_dumb_glyph_mode_uses_plain_fallbacks(monkeypatch) -> None:
    import pythinker_code.ui.shell.glyphs as glyphs

    monkeypatch.setenv("TERM", "dumb")
    plain_glyphs = importlib.reload(glyphs)
    try:
        assert plain_glyphs.SPINNER_FRAMES == ("-", "\\", "|", "/")
        assert plain_glyphs.TRANSCRIPT_PROMPT_MARKER == ">"
        assert plain_glyphs.TRANSCRIPT_TOOL_GUTTER == "|"
    finally:
        monkeypatch.delenv("TERM", raising=False)
        importlib.reload(glyphs)


def test_explicit_unicode_glyph_mode_overrides_term_dumb(monkeypatch) -> None:
    import pythinker_code.ui.shell.glyphs as glyphs

    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("PYTHINKER_TUI_GLYPHS", "unicode")
    unicode_glyphs = importlib.reload(glyphs)
    try:
        assert unicode_glyphs.SPINNER_FRAMES[0] == "⠋"
        assert unicode_glyphs.REDUCED_MOTION_GLYPH == "●"
        assert unicode_glyphs.TRANSCRIPT_PROMPT_MARKER == "❯"
        assert unicode_glyphs.TRANSCRIPT_TOOL_GUTTER == "⎿"
    finally:
        monkeypatch.delenv("TERM", raising=False)
        monkeypatch.delenv("PYTHINKER_TUI_GLYPHS", raising=False)
        importlib.reload(glyphs)


def test_reduced_motion_env_pins_runtime_activity_marker(monkeypatch) -> None:
    from pythinker_code.ui.shell.glyphs import REDUCED_MOTION_GLYPH
    from pythinker_code.ui.shell.motion import (
        ActivitySnapshot,
        active_marker_frame,
        activity_status_line,
    )

    monkeypatch.setenv("PYTHINKER_REDUCED_MOTION", "1")

    assert active_marker_frame(0.0) == REDUCED_MOTION_GLYPH
    assert active_marker_frame(10.0) == REDUCED_MOTION_GLYPH
    line = activity_status_line(ActivitySnapshot(label="Thinking", elapsed_s=10.0))
    assert line.plain.startswith(f"{REDUCED_MOTION_GLYPH} Thinking")
