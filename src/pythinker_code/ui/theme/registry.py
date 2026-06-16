"""Active theme registry and public resolution helpers."""

from __future__ import annotations

import re
from dataclasses import fields, replace
from functools import lru_cache
from typing import Any, cast

from prompt_toolkit.styles import Style as PTKStyle
from rich.style import Style as RichStyle

from pythinker_code.ui.color_utils import blend, parse_hex_color, to_hex_color
from pythinker_code.ui.terminal_capabilities import color_depth, colors_disabled

from .capabilities import get_terminal_capabilities
from .palettes import THEME_SPECS
from .resolver import StyleResolver
from .spec import (
    TUI_TOKEN_NAMES,
    DiffColors,
    MarkdownAnsiToken,
    MarkdownColors,
    MCPPromptColors,
    StatusLineColors,
    ThemeMode,
    ThemeName,
    ThemeSpec,
    ToolbarColors,
    TuiTokens,
)

_PTK_COLOR_TOKEN_RE = re.compile(r"^(?:fg:|bg:)?#[0-9A-Fa-f]{6}$")

_active_theme: ThemeName = "dark"


def _strip_ptk_colors(style: str) -> str:
    if not style:
        return style
    return " ".join(part for part in style.split() if not _PTK_COLOR_TOKEN_RE.match(part))


def _strip_ptk_style_map(values: dict[str, str]) -> dict[str, str]:
    return {key: _strip_ptk_colors(value) for key, value in values.items()}


def _strip_color_dataclass[T](value: T) -> T:
    updates: dict[str, Any] = {}
    for field in fields(cast(Any, value)):
        current = getattr(value, field.name)
        if isinstance(current, str):
            updates[field.name] = _strip_ptk_colors(current)
    return replace(cast(Any, value), **updates)


def set_active_theme(theme: ThemeName) -> None:
    global _active_theme
    _active_theme = theme


def get_active_theme() -> ThemeName:
    return _active_theme


def _mode(name: ThemeName | None = None) -> ThemeMode:
    resolved = name if name is not None else _active_theme
    return ThemeMode.LIGHT if resolved == "light" else ThemeMode.DARK


def get_theme_spec(theme: ThemeName | None = None) -> ThemeSpec:
    return THEME_SPECS[_mode(theme)]


@lru_cache(maxsize=4)
def get_resolver(theme: ThemeName = "dark") -> StyleResolver:
    return StyleResolver(THEME_SPECS[_mode(theme)], get_terminal_capabilities())


def active_resolver() -> StyleResolver:
    return get_resolver(_active_theme)


def get_tui_tokens(theme: ThemeName | None = None) -> TuiTokens:
    return get_theme_spec(theme).tokens


def tui_rich_style(token: str, *, theme: ThemeName | None = None) -> RichStyle:
    if token not in TUI_TOKEN_NAMES:
        known = ", ".join(sorted(TUI_TOKEN_NAMES))
        raise ValueError(f"Unknown TUI token {token!r}. Known tokens: {known}")
    if colors_disabled():
        return RichStyle()
    resolver = get_resolver(theme or _active_theme)
    value = resolver.core_hex(token)
    if not value:
        return RichStyle()
    if token.endswith("_bg"):
        return RichStyle(bgcolor=value)
    return RichStyle(color=value)


def get_markdown_colors(theme: ThemeName | None = None) -> MarkdownColors:
    spec = get_theme_spec(theme)
    tokens = spec.tokens
    ansi = spec.markdown_ansi
    return MarkdownColors(
        heading=tokens.tool_title,
        emphasis=tokens.muted,
        strong=tokens.tool_title,
        inline_code=tokens.accent,
        link=ansi[MarkdownAnsiToken.LINK],
        quote=ansi[MarkdownAnsiToken.QUOTE],
        ordered_marker=ansi[MarkdownAnsiToken.ORDERED_MARKER],
        unordered_marker=tokens.muted,
        table_border=tokens.border_muted,
        code_block_border=tokens.border_muted,
        code_block_bg=tokens.code_block_bg,
        spinner_active=tokens.info,
        spinner_done=tokens.success,
        spinner_failed=tokens.error,
    )


def markdown_rich_style(token: str, *, theme: ThemeName | None = None) -> RichStyle:
    if colors_disabled():
        return RichStyle()
    colors = get_markdown_colors(theme)
    value = getattr(colors, token)
    if not value:
        return RichStyle()
    if token.endswith("_bg"):
        return RichStyle(bgcolor=value)
    return RichStyle(color=value)


def get_statusline_colors() -> StatusLineColors:
    colors = get_theme_spec().status
    return _strip_color_dataclass(colors) if colors_disabled() else colors


def get_toolbar_colors() -> ToolbarColors:
    colors = get_theme_spec().toolbar
    return _strip_color_dataclass(colors) if colors_disabled() else colors


def get_mcp_prompt_colors() -> MCPPromptColors:
    colors = get_theme_spec().mcp
    return _strip_color_dataclass(colors) if colors_disabled() else colors


def get_prompt_style() -> PTKStyle:
    spec = get_theme_spec()
    styles = spec.prompt_classes
    if colors_disabled():
        styles = _strip_ptk_style_map(styles)
    return PTKStyle.from_dict(styles)


_DIFF_PLAIN = DiffColors(
    add_bg=RichStyle(),
    del_bg=RichStyle(),
    add_hl=RichStyle(),
    del_hl=RichStyle(),
)

_DIFF_ANSI16 = DiffColors(
    add_bg=RichStyle(color="green"),
    del_bg=RichStyle(color="red"),
    add_hl=RichStyle(color="green", bold=True),
    del_hl=RichStyle(color="red", bold=True),
)


def get_diff_colors() -> DiffColors:
    if colors_disabled():
        return _DIFF_PLAIN
    if color_depth() == "16":
        return _DIFF_ANSI16
    spec = get_theme_spec()
    hx = spec.diff_hex
    return DiffColors(
        add_bg=RichStyle(bgcolor=hx["add_bg"]),
        del_bg=RichStyle(bgcolor=hx["del_bg"]),
        add_hl=RichStyle(bgcolor=hx["add_hl"]),
        del_hl=RichStyle(bgcolor=hx["del_hl"]),
    )


def get_task_browser_style() -> PTKStyle:
    from .adapters.task_browser import build_task_browser_style

    return build_task_browser_style(_mode())


def thinking_frame_color(level: str, *, theme: ThemeName | None = None) -> str:
    spec = get_theme_spec(theme)
    return spec.thinking_frame.get(level) or get_tui_tokens(theme).border


@lru_cache(maxsize=32)
def _dimmed_frame_hex(level: str, name: ThemeName) -> str:
    color = thinking_frame_color(level, theme=name)
    rgb = parse_hex_color(color)
    if rgb is not None:
        pole = (255, 255, 255) if name == "light" else (0, 0, 0)
        color = to_hex_color(blend(rgb, pole, 0.7))
    return color


def thinking_frame_style(level: str, *, theme: ThemeName | None = None) -> str:
    if colors_disabled():
        return ""
    name = theme if theme is not None else _active_theme
    return f"fg:{_dimmed_frame_hex(level, name)}"


def thinking_dot_style(level: str, *, theme: ThemeName | None = None) -> str:
    if colors_disabled():
        return ""
    return f"fg:{thinking_frame_color(level, theme=theme)}"


def strip_ptk_colors(style: str) -> str:
    """Remove prompt_toolkit color directives while preserving weight/style."""
    return _strip_ptk_colors(style)


def strip_ptk_style_map(values: dict[str, str]) -> dict[str, str]:
    return _strip_ptk_style_map(values)


def theme_doctor_report(*, configured: str, config_path: str | None) -> str:
    caps = get_terminal_capabilities()
    spec = get_theme_spec()
    lines = [
        f"Theme: {get_active_theme()}",
        f"Configured: {configured}",
        f"Resolved from: {config_path or '(defaults)'}",
        f"Color enabled: {'yes' if caps.color_enabled else 'no'}",
        f"Truecolor: {'yes' if caps.truecolor else 'no'}",
        f"256-color: {'yes' if caps.color_256 else 'no'}",
        f"TERM dumb: {'yes' if caps.dumb else 'no'}",
        f"Core tokens: {len(TUI_TOKEN_NAMES)}",
        f"Prompt classes: {len(spec.prompt_classes)}",
        f"Brand tokens: {len(spec.brand)}",
    ]
    return "\n".join(lines)
