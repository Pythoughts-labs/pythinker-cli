from __future__ import annotations

from pathlib import Path
from typing import Any

from pygments.style import Style as PygmentsStyle
from pygments.token import (
    Comment,
    Error,
    Generic,
    Keyword,
    Name,
    Number,
    Operator,
    Punctuation,
    String,
    Whitespace,
)
from pygments.token import (
    Literal as PygmentsLiteral,
)
from pygments.token import (
    Text as PygmentsText,
)
from pygments.token import (
    Token as PygmentsToken,
)
from rich.style import Style
from rich.syntax import ANSISyntaxTheme, PygmentsSyntaxTheme, Syntax, SyntaxTheme

PYTHINKER_ANSI_THEME_NAME = "pythinker-ansi"
PYTHINKER_ANSI_THEME = ANSISyntaxTheme(
    {
        PygmentsToken: Style(color="default"),
        PygmentsText: Style(color="default"),
        Comment: Style(color="bright_black", italic=True),
        Keyword: Style(color="bright_blue"),
        Keyword.Constant: Style(color="bright_blue"),
        Keyword.Declaration: Style(color="bright_blue"),
        Keyword.Namespace: Style(color="bright_blue"),
        Keyword.Pseudo: Style(color="bright_blue"),
        Keyword.Reserved: Style(color="bright_blue"),
        Keyword.Type: Style(color="bright_blue"),
        Name: Style(color="default"),
        Name.Attribute: Style(color="bright_blue"),
        Name.Builtin: Style(color="bright_yellow"),
        Name.Builtin.Pseudo: Style(color="bright_blue"),
        Name.Builtin.Type: Style(color="bright_yellow", bold=True),
        Name.Class: Style(color="bright_yellow", bold=True),
        Name.Constant: Style(color="bright_blue"),
        Name.Decorator: Style(color="blue"),
        Name.Entity: Style(color="bright_yellow"),
        Name.Exception: Style(color="bright_yellow", bold=True),
        Name.Function: Style(color="blue"),
        Name.Label: Style(color="bright_blue"),
        Name.Namespace: Style(color="bright_blue"),
        Name.Other: Style(color="blue"),
        Name.Property: Style(color="bright_blue"),
        Name.Tag: Style(color="bright_green"),
        Name.Variable: Style(color="bright_yellow"),
        PygmentsLiteral: Style(color="#CE9178"),
        PygmentsLiteral.Date: Style(color="#CE9178"),
        String: Style(color="#CE9178"),
        String.Doc: Style(color="#CE9178", italic=True),
        String.Interpol: Style(color="#CE9178"),
        String.Affix: Style(color="bright_blue"),
        Number: Style(color="bright_blue"),
        Operator: Style(color="default"),
        Operator.Word: Style(color="bright_blue"),
        Punctuation: Style(color="default"),
        Generic.Deleted: Style(color="red"),
        Generic.Emph: Style(italic=True),
        Generic.Error: Style(color="bright_red", bold=True),
        Generic.Heading: Style(color="bright_blue", bold=True),
        Generic.Inserted: Style(color="green"),
        Generic.Output: Style(color="bright_black"),
        Generic.Prompt: Style(color="blue"),
        Generic.Strong: Style(bold=True),
        Generic.Subheading: Style(color="bright_blue"),
        Generic.Traceback: Style(color="bright_red", bold=True),
    }
)


# ---------------------------------------------------------------------------
# Catppuccin Mocha / Latte — hand-built as Pygments styles from the official
# palette + the canonical Catppuccin syntax mapping, with NO italic/underline
# and minimal bold (foreground + bold only). Rendered with a transparent
# background so they respect the terminal/panel bg.
# ---------------------------------------------------------------------------

CATPPUCCIN_ADAPTIVE_THEME_NAME = "catppuccin-adaptive"
CATPPUCCIN_MOCHA_THEME_NAME = "catppuccin-mocha"
CATPPUCCIN_LATTE_THEME_NAME = "catppuccin-latte"
CATPPUCCIN_FRAPPE_THEME_NAME = "catppuccin-frappe"
CATPPUCCIN_MACCHIATO_THEME_NAME = "catppuccin-macchiato"

# Official palettes (catppuccin.com/palette).
_CATPPUCCIN_MOCHA = {
    "base": "#1e1e2e",
    "text": "#cdd6f4",
    "overlay0": "#6c7086",
    "overlay2": "#9399b2",
    "mauve": "#cba6f7",
    "red": "#f38ba8",
    "peach": "#fab387",
    "yellow": "#f9e2af",
    "green": "#a6e3a1",
    "teal": "#94e2d5",
    "sky": "#89dceb",
    "blue": "#89b4fa",
    "pink": "#f5c2e7",
}
_CATPPUCCIN_LATTE = {
    "base": "#eff1f5",
    "text": "#4c4f69",
    "overlay0": "#9ca0b0",
    "overlay2": "#7c7f93",
    "mauve": "#8839ef",
    "red": "#d20f39",
    "peach": "#fe640b",
    "yellow": "#df8e1d",
    "green": "#40a02b",
    "teal": "#179299",
    "sky": "#04a5e5",
    "blue": "#1e66f5",
    "pink": "#ea76cb",
}
_CATPPUCCIN_FRAPPE = {
    "base": "#303446",
    "text": "#c6d0f5",
    "overlay0": "#737994",
    "overlay2": "#949cbb",
    "mauve": "#ca9ee6",
    "red": "#e78284",
    "peach": "#ef9f76",
    "yellow": "#e5c890",
    "green": "#a6d189",
    "teal": "#81c8be",
    "sky": "#99d1db",
    "blue": "#8caaee",
    "pink": "#f4b8e4",
}
_CATPPUCCIN_MACCHIATO = {
    "base": "#24273a",
    "text": "#cad3f5",
    "overlay0": "#6e738d",
    "overlay2": "#939ab7",
    "mauve": "#c6a0f6",
    "red": "#ed8796",
    "peach": "#f5a97f",
    "yellow": "#eed49f",
    "green": "#a6da95",
    "teal": "#8bd5ca",
    "sky": "#91d7e3",
    "blue": "#8aadf4",
    "pink": "#f5bde6",
}


def _catppuccin_styles(p: dict[str, str]) -> dict[Any, str]:
    """Canonical Catppuccin token → color mapping (no italic/underline)."""
    return {
        PygmentsToken: p["text"],
        PygmentsText: p["text"],
        Whitespace: p["text"],
        Comment: p["overlay0"],
        Comment.Preproc: p["pink"],
        Keyword: p["mauve"],
        Keyword.Constant: p["peach"],
        Keyword.Declaration: p["mauve"],
        Keyword.Namespace: p["mauve"],
        Keyword.Pseudo: p["mauve"],
        Keyword.Reserved: p["mauve"],
        Keyword.Type: p["yellow"],
        Operator: p["sky"],
        Operator.Word: p["mauve"],
        Punctuation: p["overlay2"],
        Name: p["text"],
        Name.Attribute: p["blue"],
        Name.Builtin: p["red"],
        Name.Builtin.Pseudo: p["red"],
        Name.Class: p["yellow"],
        Name.Constant: p["peach"],
        Name.Decorator: p["blue"],
        Name.Entity: p["pink"],
        Name.Exception: p["yellow"],
        Name.Function: p["blue"],
        Name.Function.Magic: p["sky"],
        Name.Label: p["peach"],
        Name.Namespace: p["yellow"],
        Name.Property: p["teal"],
        Name.Tag: p["mauve"],
        Name.Variable: p["text"],
        Name.Variable.Magic: p["red"],
        Number: p["peach"],
        PygmentsLiteral: p["peach"],
        String: p["green"],
        String.Doc: p["overlay0"],
        String.Escape: p["pink"],
        String.Interpol: p["pink"],
        String.Regex: p["pink"],
        String.Symbol: p["red"],
        Generic.Deleted: p["red"],
        Generic.Inserted: p["green"],
        Generic.Heading: f"bold {p['blue']}",
        Generic.Subheading: f"bold {p['blue']}",
        Generic.Strong: "bold",
        Generic.Emph: p["text"],  # italic intentionally omitted
        Generic.Error: p["red"],
        Generic.Traceback: p["red"],
        Error: p["red"],
    }


class CatppuccinMochaStyle(PygmentsStyle):
    name = "catppuccin-mocha"
    background_color = _CATPPUCCIN_MOCHA["base"]
    styles = _catppuccin_styles(_CATPPUCCIN_MOCHA)


class CatppuccinLatteStyle(PygmentsStyle):
    name = "catppuccin-latte"
    background_color = _CATPPUCCIN_LATTE["base"]
    styles = _catppuccin_styles(_CATPPUCCIN_LATTE)


class CatppuccinFrappeStyle(PygmentsStyle):
    name = "catppuccin-frappe"
    background_color = _CATPPUCCIN_FRAPPE["base"]
    styles = _catppuccin_styles(_CATPPUCCIN_FRAPPE)


class CatppuccinMacchiatoStyle(PygmentsStyle):
    name = "catppuccin-macchiato"
    background_color = _CATPPUCCIN_MACCHIATO["base"]
    styles = _catppuccin_styles(_CATPPUCCIN_MACCHIATO)


CATPPUCCIN_MOCHA_THEME = PygmentsSyntaxTheme(CatppuccinMochaStyle)
CATPPUCCIN_LATTE_THEME = PygmentsSyntaxTheme(CatppuccinLatteStyle)
CATPPUCCIN_FRAPPE_THEME = PygmentsSyntaxTheme(CatppuccinFrappeStyle)
CATPPUCCIN_MACCHIATO_THEME = PygmentsSyntaxTheme(CatppuccinMacchiatoStyle)

_BUILTIN_CATPPUCCIN_THEMES: dict[str, PygmentsSyntaxTheme] = {
    CATPPUCCIN_MOCHA_THEME_NAME: CATPPUCCIN_MOCHA_THEME,
    CATPPUCCIN_LATTE_THEME_NAME: CATPPUCCIN_LATTE_THEME,
    CATPPUCCIN_FRAPPE_THEME_NAME: CATPPUCCIN_FRAPPE_THEME,
    CATPPUCCIN_MACCHIATO_THEME_NAME: CATPPUCCIN_MACCHIATO_THEME,
}


def resolve_code_theme(theme: str | SyntaxTheme) -> str | SyntaxTheme:
    if isinstance(theme, str):
        name = theme.lower()
        if name == PYTHINKER_ANSI_THEME_NAME:
            return PYTHINKER_ANSI_THEME
        if name == CATPPUCCIN_ADAPTIVE_THEME_NAME:
            # Follow the active UI theme: Latte on light terminals, Mocha
            # otherwise. Imported lazily to avoid a circular import at module
            # load (ui.theme is a higher layer).
            from pythinker_code.ui.theme import get_active_theme

            if get_active_theme() == "light":
                return CATPPUCCIN_LATTE_THEME
            return CATPPUCCIN_MOCHA_THEME
        if name in _BUILTIN_CATPPUCCIN_THEMES:
            return _BUILTIN_CATPPUCCIN_THEMES[name]
        from pythinker_code.ui.theme.pythinker_themes import PYGMENTS_THEME_ALIASES

        alias = PYGMENTS_THEME_ALIASES.get(name, name)
        if alias == PYTHINKER_ANSI_THEME_NAME:
            return PYTHINKER_ANSI_THEME
        if alias in _BUILTIN_CATPPUCCIN_THEMES:
            return _BUILTIN_CATPPUCCIN_THEMES[alias]
        return alias
    return theme


def available_code_themes() -> list[str]:
    """Accepted ``code_theme`` values: pythinker-x bundled names, sentinels, custom, Pygments."""
    from pygments.styles import get_all_styles

    from pythinker_code.ui.theme.pythinker_themes import list_syntax_theme_names

    bundled = list_syntax_theme_names()
    extras = [
        CATPPUCCIN_ADAPTIVE_THEME_NAME,
        PYTHINKER_ANSI_THEME_NAME,
        *sorted(get_all_styles()),
    ]
    merged = sorted(set(bundled) | set(extras), key=str.casefold)
    return merged


def list_picker_code_themes(share_dir: Path | None = None) -> list[str]:
    """Themes shown in ``/theme code``: validator union plus share-dir ``.tmTheme`` files."""
    from pythinker_code.ui.theme.pythinker_themes import discover_custom_syntax_themes

    custom = discover_custom_syntax_themes(share_dir)
    return sorted(set(available_code_themes()) | set(custom), key=str.casefold)


def code_themes_match_for_picker(theme: str, configured: str) -> bool:
    """Whether *theme* should appear selected for a configured ``code_theme`` value."""
    if theme.casefold() == configured.casefold():
        return True
    resolved_theme = resolve_code_theme(theme)
    resolved_configured = resolve_code_theme(configured)
    if isinstance(resolved_theme, str) and isinstance(resolved_configured, str):
        return resolved_theme.casefold() == resolved_configured.casefold()
    if not isinstance(resolved_theme, str) and not isinstance(resolved_configured, str):
        return type(resolved_theme) is type(resolved_configured)
    return False


# Process-wide default code-fence theme, resolved once at shell startup from
# ``config.tui.code_theme``. Mirrors ``ui.theme`` set_active_theme/get_active_theme
# so renderers pick up the configured theme without threading config through
# every call site. ``CATPPUCCIN_ADAPTIVE_THEME_NAME`` is the default and follows
# the active light/dark UI theme (Mocha on dark, Latte on light).
_active_code_theme: str = CATPPUCCIN_ADAPTIVE_THEME_NAME


def set_active_code_theme(theme: str) -> None:
    """Set the process-wide default code-fence theme (Pygments style name or ANSI sentinel)."""
    global _active_code_theme
    _active_code_theme = theme


def get_active_code_theme() -> str:
    """Return the active code-fence theme name (defaults to the ANSI sentinel)."""
    return _active_code_theme


class PythinkerSyntax(Syntax):
    def __init__(self, code: str, lexer: str, **kwargs: Any) -> None:
        if "theme" not in kwargs or kwargs["theme"] is None:
            kwargs["theme"] = resolve_code_theme(
                get_active_code_theme() or PYTHINKER_ANSI_THEME_NAME
            )
        super().__init__(code, lexer, **kwargs)


if __name__ == "__main__":
    from rich.console import Console
    from rich.text import Text

    console = Console()

    examples = [
        ("diff", "diff", "@@ -1,2 +1,2 @@\n-line one\n+line uno\n"),
        (
            "python",
            "python",
            'def greet(name: str) -> str:\n    return f"Hi, {name}!"\n',
        ),
        ("bash", "bash", "set -euo pipefail\nprintf '%s\\n' \"hello\"\n"),
    ]

    for idx, (title, lexer, code) in enumerate(examples):
        if idx:
            console.print()
        console.print(Text(f"[{title}]", style="bold"))
        console.print(PythinkerSyntax(code, lexer))
