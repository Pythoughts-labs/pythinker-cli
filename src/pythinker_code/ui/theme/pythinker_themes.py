"""pythinker-x (TUI) theme constants — ported verbatim where possible.
"""

from __future__ import annotations

from pathlib import Path

from pythinker_code.ui.color_utils import blend, to_hex_color

# highlight.rs BUILTIN_THEME_NAMES (32 bundled syntax themes, sorted).
BUNDLED_SYNTAX_THEME_NAMES: tuple[str, ...] = (
    "1337",
    "ansi",
    "base16",
    "base16-256",
    "base16-eighties-dark",
    "base16-mocha-dark",
    "base16-ocean-dark",
    "base16-ocean-light",
    "catppuccin-frappe",
    "catppuccin-latte",
    "catppuccin-macchiato",
    "catppuccin-mocha",
    "coldark-cold",
    "coldark-dark",
    "dark-neon",
    "dracula",
    "github",
    "gruvbox-dark",
    "gruvbox-light",
    "inspired-github",
    "monokai-extended",
    "monokai-extended-bright",
    "monokai-extended-light",
    "monokai-extended-origin",
    "nord",
    "one-half-dark",
    "one-half-light",
    "solarized-dark",
    "solarized-light",
    "sublime-snazzy",
    "two-dark",
    "zenburn",
)

# diff_render.rs truecolor palette (GitHub-style light, muted dark tints).
DIFF_HEX_DARK: dict[str, str] = {
    "add_bg": "#213A2B",
    "del_bg": "#4A221D",
    "add_hl": "#2E6B4A",
    "del_hl": "#6B3430",
}

DIFF_HEX_LIGHT: dict[str, str] = {
    "add_bg": "#dafbe1",
    "del_bg": "#ffebe9",
    "add_hl": "#aceebb",
    "del_hl": "#ffcecb",
}

# style.rs adaptive accent + user-message blend parameters.
LIGHT_BG_ACCENT_RGB = (0, 95, 135)
USER_MESSAGE_BLEND_LIGHT = ((0, 0, 0), 0.04)
USER_MESSAGE_BLEND_DARK = ((255, 255, 255), 0.12)
TABLE_SEPARATOR_FG_ALPHA = 0.20

# ponytail: Pygments lacks two_face's bat themes; map bundled names to closest stock styles.
PYGMENTS_THEME_ALIASES: dict[str, str] = {
    "1337": "monokai",
    "ansi": "pythinker-ansi",
    "base16": "default",
    "base16-256": "default",
    "base16-eighties-dark": "default",
    "base16-mocha-dark": "default",
    "base16-ocean-dark": "default",
    "base16-ocean-light": "default",
    "catppuccin-frappe": "catppuccin-frappe",
    "catppuccin-latte": "catppuccin-latte",
    "catppuccin-macchiato": "catppuccin-macchiato",
    "catppuccin-mocha": "catppuccin-mocha",
    "coldark-cold": "default",
    "coldark-dark": "default",
    "dark-neon": "dracula",
    "dracula": "dracula",
    "github": "github-dark",
    "gruvbox-dark": "gruvbox-dark",
    "gruvbox-light": "gruvbox-light",
    "inspired-github": "github-dark",
    "monokai-extended": "monokai",
    "monokai-extended-bright": "monokai",
    "monokai-extended-light": "monokai",
    "monokai-extended-origin": "monokai",
    "nord": "nord",
    "one-half-dark": "native",
    "one-half-light": "native",
    "solarized-dark": "solarized-dark",
    "solarized-light": "solarized-light",
    "sublime-snazzy": "monokai",
    "two-dark": "monokai",
    "zenburn": "zenburn",
}


def user_message_bg_for_terminal(bg_rgb: tuple[int, int, int]) -> str:
    """Terminal-adaptive user bubble bg (style.rs ``user_message_bg``)."""
    from pythinker_code.ui.color_utils import is_light

    top, alpha = USER_MESSAGE_BLEND_LIGHT if is_light(bg_rgb) else USER_MESSAGE_BLEND_DARK
    return to_hex_color(blend(top, bg_rgb, alpha))


def discover_custom_syntax_themes(share_dir: Path | None) -> list[str]:
    """Custom ``.tmTheme`` stems under ``{share_dir}/themes/`` (picker listing only)."""
    if share_dir is None:
        return []
    themes_dir = share_dir / "themes"
    if not themes_dir.is_dir():
        return []
    names: list[str] = []
    for path in sorted(themes_dir.glob("*.tmTheme")):
        stem = path.stem
        if stem and stem not in BUNDLED_SYNTAX_THEME_NAMES:
            names.append(stem)
    return names


def list_syntax_theme_names(share_dir: Path | None = None) -> list[str]:
    """Bundled + custom theme names, sorted case-insensitively like pythinker-x."""
    custom = discover_custom_syntax_themes(share_dir)
    merged = sorted(set(BUNDLED_SYNTAX_THEME_NAMES) | set(custom), key=str.casefold)
    return merged
