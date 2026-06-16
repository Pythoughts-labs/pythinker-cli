"""pythinker-x theme port contract tests."""

from __future__ import annotations

from pythinker_code.ui.theme.palettes import THEME_SPECS
from pythinker_code.ui.theme.pythinker_themes import (
    BUNDLED_SYNTAX_THEME_NAMES,
    DIFF_HEX_DARK,
    DIFF_HEX_LIGHT,
    PYGMENTS_THEME_ALIASES,
    list_syntax_theme_names,
)
from pythinker_code.ui.theme.spec import ThemeMode
from pythinker_code.utils.rich.syntax import (
    available_code_themes,
    code_themes_match_for_picker,
    list_picker_code_themes,
    resolve_code_theme,
)


def test_bundled_syntax_theme_count_matches_pythinker_x():
    assert len(BUNDLED_SYNTAX_THEME_NAMES) == 32


def test_diff_hex_matches_pythinker_x():
    assert THEME_SPECS[ThemeMode.DARK].diff_hex == DIFF_HEX_DARK
    assert THEME_SPECS[ThemeMode.LIGHT].diff_hex == DIFF_HEX_LIGHT
    assert DIFF_HEX_DARK["add_bg"] == "#213A2B"
    assert DIFF_HEX_DARK["del_bg"] == "#4A221D"
    assert DIFF_HEX_LIGHT["add_hl"] == "#aceebb"


def test_all_bundled_themes_accepted_by_config_validator():
    allowed = set(available_code_themes())
    for name in BUNDLED_SYNTAX_THEME_NAMES:
        assert name in allowed


def test_resolve_code_theme_maps_bundled_names():
    assert resolve_code_theme("dracula") == "dracula"
    assert resolve_code_theme("ansi") != "ansi"
    assert resolve_code_theme("github") == "github-dark"


def test_pygments_aliases_cover_all_bundled_names():
    for name in BUNDLED_SYNTAX_THEME_NAMES:
        assert name in PYGMENTS_THEME_ALIASES


def test_list_syntax_theme_names_sorted():
    names = list_syntax_theme_names()
    assert names == sorted(names, key=str.casefold)


def test_list_picker_code_themes_includes_pygments_and_bundled():
    picker = list_picker_code_themes()
    assert "monokai" in picker
    assert "github-dark" in picker
    assert BUNDLED_SYNTAX_THEME_NAMES[0] in picker
    assert len(picker) > len(BUNDLED_SYNTAX_THEME_NAMES)


def test_code_themes_match_for_picker_resolves_aliases():
    assert code_themes_match_for_picker("github", "github")
    assert code_themes_match_for_picker("github-dark", "github")
    assert not code_themes_match_for_picker("dracula", "github")
