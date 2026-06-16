"""Theme system contract tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from rich.style import Style as RichStyle

from pythinker_code.ui.theme import (
    TUI_TOKEN_NAMES,
    get_markdown_colors,
    get_prompt_style,
    get_theme_spec,
    get_tui_tokens,
    set_active_theme,
    tui_rich_style,
)
from pythinker_code.ui.theme.adapters.markdown import markdown_style_overrides
from pythinker_code.ui.theme.palettes import PROMPT_STYLE_DARK, THEME_SPECS
from pythinker_code.ui.theme.spec import ThemeMode

_HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")
_THEME_PKG = Path(__file__).resolve().parents[2] / "src" / "pythinker_code" / "ui" / "theme"
_PALETTE_ONLY_FILES = {_THEME_PKG / "palettes.py", _THEME_PKG / "adapters" / "task_browser.py"}


@pytest.fixture(autouse=True)
def _restore_theme():
    from pythinker_code.ui.theme import get_active_theme

    saved = get_active_theme()
    yield
    set_active_theme(saved)


def test_all_core_tokens_exist_in_dark_and_light():
    for mode in (ThemeMode.DARK, ThemeMode.LIGHT):
        tokens = THEME_SPECS[mode].tokens
        for name in TUI_TOKEN_NAMES:
            assert hasattr(tokens, name)


def test_prompt_styles_derive_from_theme_spec():
    assert PROMPT_STYLE_DARK is THEME_SPECS[ThemeMode.DARK].prompt_classes
    rules = dict(get_prompt_style().style_rules)
    assert rules["slash-command"] == PROMPT_STYLE_DARK["slash-command"]


def test_no_bold_inline_code():
    style = markdown_style_overrides("dark")["markdown.code"]
    assert style.bold is not True


def test_inline_code_uses_accent_not_info():
    colors = get_markdown_colors("dark")
    tokens = get_tui_tokens("dark")
    assert colors.inline_code == tokens.accent
    assert colors.inline_code != tokens.info


def test_unknown_token_raises():
    with pytest.raises(ValueError, match="Unknown TUI token"):
        tui_rich_style("not_a_real_token")


def test_theme_spec_single_source_for_selected_bg():
    dark = get_theme_spec("dark")
    assert dark.tokens.selected_bg in PROMPT_STYLE_DARK["slash-completion-menu.row.current"]


def test_theme_logic_modules_have_no_hex_literals():
    for name in ("spec.py", "registry.py", "resolver.py", "capabilities.py", "__init__.py"):
        text = (_THEME_PKG / name).read_text(encoding="utf-8")
        assert _HEX_RE.search(text) is None, name


def test_resolver_heading_bold_inline_not_bold():
    from pythinker_code.ui.theme import get_resolver

    resolver = get_resolver("dark")
    heading = resolver.markdown_heading_style(level=1)
    inline = resolver.markdown_inline_code_style()
    assert heading.bold is True
    assert inline.bold is not True
    assert inline.color == RichStyle(color=get_tui_tokens("dark").accent).color
