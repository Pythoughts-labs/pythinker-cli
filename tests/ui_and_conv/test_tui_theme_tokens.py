"""Tests for the Pythinker semantic theme tokens added to ui/theme.py."""

from __future__ import annotations

import dataclasses

import pytest
from rich.style import Style as RichStyle

from pythinker_code.ui.theme import (
    TUI_TOKEN_NAMES,
    ThemeName,
    TuiTokens,
    get_active_theme,
    get_markdown_colors,
    get_tui_tokens,
    markdown_rich_style,
    set_active_theme,
    tui_rich_style,
)


def _color_name(style: RichStyle) -> str:
    """Return the resolved color name, asserting the style actually set one."""
    color = style.color
    assert color is not None
    return color.name


@pytest.fixture(autouse=True)
def _restore_active_theme():
    """Snapshot/restore the global active theme so tests don't bleed."""
    saved = get_active_theme()
    try:
        yield
    finally:
        set_active_theme(saved)


def test_dark_tokens_have_brand_values():
    set_active_theme("dark")
    t = get_tui_tokens()
    assert t.accent == "#AEB7FF"
    assert t.border_accent == "#7C88DE"  # accent-family chrome (active borders)
    assert t.border == "#8a8d91"  # mid grey
    assert t.info == "#8FDDEA"
    assert t.success == "#7CCF8A"
    assert t.error == "#F87171"
    assert t.thinking_text == "#D4D4D4"  # light neutral grey, not purple-tinted muted
    assert t.thinking_text != t.muted
    assert t.activity_verb == "#C68D7E"  # muted clay-coral resting
    assert t.activity_verb_mid == "#D8AC9E"  # soft coral
    assert t.activity_verb_highlight == "#E9CDC2"  # calm coral spark
    assert t.activity_spinner == "#B8C0CC"
    assert t.tool_title == t.activity_label
    assert t.tool_pending_bg == "#1B2230"
    assert t.tool_error_bg == "#2E1D24"


def test_light_tokens_have_brand_values():
    set_active_theme("light")
    t = get_tui_tokens()
    assert t.accent == "#0B114E"  # deep indigo brand accent (light mode)
    assert t.border_accent == "#3B469B"  # accent-family chrome (active borders)
    assert t.info == "#176B7E"  # text-safe cyan (unchanged)
    assert t.text == "#213853"  # navy text
    assert t.error == "#C0392B"
    assert t.thinking_text == "#7A7A7A"  # lighter neutral grey, not blue/purple muted
    assert t.thinking_text != t.muted
    assert t.activity_verb == "#B26A52"  # muted contrast-safe coral activity verb
    assert t.activity_verb_mid == "#9E563E"  # deeper muted coral
    assert t.activity_verb_highlight == "#82412D"  # deep-coral spark (max contrast on light)
    assert t.activity_spinner == "#6B7280"
    assert t.tool_title == t.activity_label
    assert t.tool_pending_bg == "#EFE7E8"


def test_get_tui_tokens_with_explicit_theme_arg():
    set_active_theme("dark")
    light = get_tui_tokens("light")
    assert light.tool_pending_bg == "#EFE7E8"


def test_text_token_is_empty_string_for_terminal_default():
    # Dark theme: empty string = use terminal's default fg color.
    # Light theme uses an explicit navy text color (#213853).
    assert get_tui_tokens("dark").text == ""


def test_selected_bg_reharmonized_and_drives_prompt_selection():
    """selected_bg joins the accent (periwinkle/indigo) family and is the single
    source for the completion/dialog selection rows (no parallel literals)."""
    from pythinker_code.ui.theme import _PROMPT_STYLE_DARK, _PROMPT_STYLE_LIGHT

    assert get_tui_tokens("dark").selected_bg == "#252944"
    assert get_tui_tokens("light").selected_bg == "#E7E9F9"
    assert _PROMPT_STYLE_DARK["slash-completion-menu.row.current"] == (
        f"bg:{get_tui_tokens('dark').selected_bg}"
    )
    assert _PROMPT_STYLE_LIGHT["slash-completion-menu.row.current"] == (
        f"bg:{get_tui_tokens('light').selected_bg}"
    )


def test_user_message_bg_is_neutral_grey_not_tinted():
    """User-sent messages use a neutral grey block (not the old blue tint).

    The block must stay visible (same lightness as before) but carry no hue —
    R, G and B within a tight tolerance — so it reads as 'grey, not blur'.
    """
    expected: dict[ThemeName, str] = {"dark": "#333333", "light": "#E0E0E0"}
    for mode, hexval in expected.items():
        token = get_tui_tokens(mode).user_message_bg
        assert token == hexval, mode
        r, g, b = int(token[1:3], 16), int(token[3:5], 16), int(token[5:7], 16)
        assert abs(r - g) <= 4 and abs(g - b) <= 4, f"{mode} not neutral grey: {token}"


def test_tokens_dataclass_is_frozen():
    t = get_tui_tokens("dark")
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.accent = "#000000"  # type: ignore[misc]


def test_all_token_fields_are_strings():
    t = get_tui_tokens("dark")
    for field in dataclasses.fields(TuiTokens):
        assert isinstance(getattr(t, field.name), str), field.name


def test_tui_rich_style_bg_token_produces_bgcolor():
    set_active_theme("dark")
    style = tui_rich_style("tool_pending_bg")
    assert isinstance(style, RichStyle)
    assert style.bgcolor is not None
    assert style.color is None


def test_tui_rich_style_fg_token_produces_color():
    set_active_theme("dark")
    style = tui_rich_style("accent")
    assert style.color is not None
    assert style.bgcolor is None


def test_tui_rich_style_empty_token_produces_empty_style():
    # text="" means terminal default — should not set color or bgcolor.
    set_active_theme("dark")
    style = tui_rich_style("text")
    assert style.color is None
    assert style.bgcolor is None


def test_tui_rich_style_unknown_token_raises():
    with pytest.raises(ValueError):
        tui_rich_style("not_a_real_token")


def test_dark_markdown_uses_professional_report_roles():
    colors = get_markdown_colors("dark")
    assert colors.heading == "#F4F4F5"  # primary white, not coral/orange
    assert colors.strong == "#F4F4F5"
    assert colors.emphasis == "#8A8A8A"  # neutral UI grey (refined muted contrast)
    assert colors.inline_code == "#AEB7FF"
    assert colors.link == "bright_blue"
    assert colors.spinner_active == "#8FDDEA"
    assert colors.spinner_done == "#7CCF8A"
    assert colors.spinner_failed == "#F87171"
    assert markdown_rich_style("link", theme="dark").color is not None


def test_light_markdown_uses_professional_report_roles():
    colors = get_markdown_colors("light")
    assert colors.heading == "#213853"
    assert colors.strong == "#213853"
    assert colors.emphasis == "#666666"
    assert colors.inline_code == "#0B114E"  # accent token (light)
    assert colors.spinner_active == "#176B7E"  # spinners still use the info token


def test_markdown_ansi_styles_resolve_to_terminal_colors():
    """Link, quote, and ordered_marker use ANSI terminal colors; inline_code
    uses the themed accent token so inline highlights match brand periwinkle."""
    for mode in ("dark", "light"):
        assert _color_name(markdown_rich_style("link", theme=mode)) == "bright_blue"
        assert _color_name(markdown_rich_style("quote", theme=mode)) == "green"
        assert _color_name(markdown_rich_style("ordered_marker", theme=mode)) == "bright_blue"
        # inline_code uses the accent token, not info cyan/green.
        assert _color_name(markdown_rich_style("inline_code", theme=mode)) not in ("cyan", "green")
        # Unordered bullets stay muted (a hex), not an ANSI accent.
        assert _color_name(markdown_rich_style("unordered_marker", theme=mode)) != "green"


def test_info_token_exists_and_is_cyan():
    assert get_tui_tokens("dark").info == "#8FDDEA"
    assert get_tui_tokens("light").info == "#176B7E"
    # resolver works for the new token
    set_active_theme("dark")
    assert tui_rich_style("info").color is not None


# ---------------------------------------------------------------------------
# code_block_bg token (new — must be added to TuiTokens)
# ---------------------------------------------------------------------------


def test_code_block_bg_in_token_names():
    assert "code_block_bg" in TUI_TOKEN_NAMES


def test_activity_tokens_in_token_names():
    assert "activity_verb" in TUI_TOKEN_NAMES
    assert "activity_verb_mid" in TUI_TOKEN_NAMES
    assert "activity_verb_highlight" in TUI_TOKEN_NAMES
    assert "activity_spinner" in TUI_TOKEN_NAMES


def test_code_block_bg_dark_value():
    assert get_tui_tokens("dark").code_block_bg == "#1B1D2B"


def test_code_block_bg_light_value():
    assert get_tui_tokens("light").code_block_bg == "#f1f5f9"


def test_code_block_bg_resolves_as_bgcolor():
    set_active_theme("dark")
    style = tui_rich_style("code_block_bg")
    assert isinstance(style, RichStyle)
    assert style.bgcolor is not None
    assert style.color is None


# ---------------------------------------------------------------------------
# Markdown colors derived from TuiTokens (contract tests)
# The values must stay in sync — no parallel hardcoded hex allowed.
# ---------------------------------------------------------------------------


def test_markdown_colors_derived_from_tokens_dark():
    t = get_tui_tokens("dark")
    c = get_markdown_colors("dark")
    assert c.heading == t.tool_title
    assert c.strong == t.tool_title
    assert c.emphasis == t.muted
    # inline_code uses the accent token for inline highlights; link/quote/ordered_marker remain ANSI.
    assert c.inline_code == t.accent
    assert c.link == "bright_blue"
    assert c.quote == "green"
    assert c.ordered_marker == "bright_blue"
    assert c.unordered_marker == t.muted  # unordered bullets stay muted
    assert c.table_border == t.border_muted
    assert c.code_block_border == t.border_muted
    assert c.code_block_bg == t.code_block_bg
    assert c.spinner_active == t.info
    assert c.spinner_done == t.success
    assert c.spinner_failed == t.error


def test_markdown_colors_derived_from_tokens_light():
    t = get_tui_tokens("light")
    c = get_markdown_colors("light")
    assert c.heading == t.tool_title
    assert c.strong == t.tool_title
    assert c.emphasis == t.muted
    # inline_code uses the accent token; link remains ANSI bright_blue.
    assert c.inline_code == t.accent
    assert c.link == "bright_blue"
    assert c.quote == "green"
    assert c.ordered_marker == "bright_blue"
    assert c.unordered_marker == t.muted
    assert c.table_border == t.border_muted
    assert c.code_block_border == t.border_muted
    assert c.code_block_bg == t.code_block_bg
    assert c.spinner_active == t.info
    assert c.spinner_done == t.success
    assert c.spinner_failed == t.error
