"""Public theme API — backward-compatible re-exports."""

from __future__ import annotations

from .palettes import (
    BRAND,
    PROMPT_STYLE_DARK,
    PROMPT_STYLE_LIGHT,
    SELECTED_BG_DARK,
    SELECTED_BG_LIGHT,
    THEME_SPECS,
)
from .registry import (
    active_resolver,
    get_active_theme,
    get_diff_colors,
    get_markdown_colors,
    get_mcp_prompt_colors,
    get_prompt_style,
    get_resolver,
    get_statusline_colors,
    get_task_browser_style,
    get_theme_spec,
    get_toolbar_colors,
    get_tui_tokens,
    markdown_rich_style,
    set_active_theme,
    strip_ptk_colors,
    strip_ptk_style_map,
    theme_doctor_report,
    thinking_dot_style,
    thinking_frame_color,
    thinking_frame_style,
    tui_rich_style,
)
from .spec import (
    TUI_TOKEN_NAMES,
    BrandToken,
    CoreToken,
    DiffColors,
    MarkdownColors,
    MCPPromptColors,
    PromptToken,
    StatusLineColors,
    ThemeMode,
    ThemeName,
    ThemeSpec,
    ToolbarColors,
    TuiTokens,
)

# Back-compat private names referenced by tests.
_PROMPT_STYLE_DARK = PROMPT_STYLE_DARK
_PROMPT_STYLE_LIGHT = PROMPT_STYLE_LIGHT
_SELECTED_BG_DARK = SELECTED_BG_DARK
_SELECTED_BG_LIGHT = SELECTED_BG_LIGHT
_TUI_TOKENS_DARK = THEME_SPECS[ThemeMode.DARK].tokens
_TUI_TOKENS_LIGHT = THEME_SPECS[ThemeMode.LIGHT].tokens
_strip_ptk_colors = strip_ptk_colors
_strip_ptk_style_map = strip_ptk_style_map

__all__ = [
    "BRAND",
    "BrandToken",
    "CoreToken",
    "DiffColors",
    "MarkdownColors",
    "MCPPromptColors",
    "PromptToken",
    "StatusLineColors",
    "ThemeMode",
    "ThemeName",
    "ThemeSpec",
    "ToolbarColors",
    "TUI_TOKEN_NAMES",
    "TuiTokens",
    "active_resolver",
    "get_active_theme",
    "get_diff_colors",
    "get_markdown_colors",
    "get_mcp_prompt_colors",
    "get_prompt_style",
    "get_resolver",
    "get_statusline_colors",
    "get_task_browser_style",
    "get_theme_spec",
    "get_toolbar_colors",
    "get_tui_tokens",
    "markdown_rich_style",
    "set_active_theme",
    "theme_doctor_report",
    "thinking_dot_style",
    "thinking_frame_color",
    "thinking_frame_style",
    "tui_rich_style",
]
