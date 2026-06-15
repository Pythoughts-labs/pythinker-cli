"""Centralized terminal color theme definitions.

All UI-facing colors live here so that switching between dark and light
terminal themes only requires changing the active ``ThemeName``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace
from functools import lru_cache
from typing import Any, Literal, cast

from prompt_toolkit.styles import Style as PTKStyle
from rich.style import Style as RichStyle

from pythinker_code.ui.color_utils import blend, parse_hex_color, to_hex_color
from pythinker_code.ui.terminal_capabilities import color_depth, colors_disabled

type ThemeName = Literal["dark", "light"]


# Intentionally strips only hex prompt_toolkit color tokens (for example
# ``#RRGGBB``, ``fg:#RRGGBB``, and ``bg:#RRGGBB``). Named/ANSI tokens such as
# ``fg:red`` or ``bg:ansired`` are preserved; if those need no-color support,
# extend this regex and keep ``_strip_ptk_colors`` in sync.
_PTK_COLOR_TOKEN_RE = re.compile(r"^(?:fg:|bg:)?#[0-9A-Fa-f]{6}$")


def _strip_ptk_colors(style: str) -> str:
    """Remove prompt_toolkit color directives while preserving weight/style."""
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
    result: T = replace(cast(Any, value), **updates)
    return result


# ---------------------------------------------------------------------------
# Diff colors (used by utils/rich/diff_render.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiffColors:
    add_bg: RichStyle
    del_bg: RichStyle
    add_hl: RichStyle
    del_hl: RichStyle


_DIFF_DARK = DiffColors(
    add_bg=RichStyle(bgcolor="#052e05"),
    del_bg=RichStyle(bgcolor="#3a0808"),
    add_hl=RichStyle(bgcolor="#0e5a0e"),
    del_hl=RichStyle(bgcolor="#6b1414"),
)

_DIFF_LIGHT = DiffColors(
    add_bg=RichStyle(bgcolor="#dafbe1"),
    del_bg=RichStyle(bgcolor="#ffebe9"),
    add_hl=RichStyle(bgcolor="#aff5b4"),
    del_hl=RichStyle(bgcolor="#ffc1c0"),
)

_DIFF_PLAIN = DiffColors(
    add_bg=RichStyle(),
    del_bg=RichStyle(),
    add_hl=RichStyle(),
    del_hl=RichStyle(),
)

# Basic 16-color terminals: the hex background tints above quantize into
# unreadable mud, so fall back to plain green/red foregrounds (the ANSI16
# diff tier). The fields still act as overlay styles for diff rows.
_DIFF_ANSI16 = DiffColors(
    add_bg=RichStyle(color="green"),
    del_bg=RichStyle(color="red"),
    add_hl=RichStyle(color="green", bold=True),
    del_hl=RichStyle(color="red", bold=True),
)


# ---------------------------------------------------------------------------
# Task browser colors (used by ui/shell/task_browser.py)
# ---------------------------------------------------------------------------


def _task_browser_style_dark() -> PTKStyle:
    styles = {
        "header": "bg:#1f2937 #e5e7eb",
        "header.title": "bg:#1f2937 #F4F4F5 bold",
        "header.meta": "bg:#1f2937 #A3A3A3",
        "status.running": "bg:#1f2937 #7BC97F bold",
        "status.success": "bg:#1f2937 #7BC97F",
        "status.warning": "bg:#1f2937 #E6B450",
        "status.error": "bg:#1f2937 #EF5E62",
        "status.info": "bg:#1f2937 #AFE3F1",
        "task-list": "bg:#111827 #d1d5db",
        "task-list.checked": "bg:#164e63 #ecfeff bold",
        "frame.border": "#3A506D",
        "frame.label": "bg:#17182a #F4F4F5 bold",
        "footer": "bg:#17182a #A3A3A3",
        "footer.key": "bg:#17182a #AFE3F1 bold",
        "footer.text": "bg:#17182a #A3A3A3",
        "footer.warning": "bg:#4a3315 #E6B450 bold",
        "footer.meta": "bg:#17182a #5F6B7E",
    }
    if colors_disabled():
        styles = _strip_ptk_style_map(styles)
    return PTKStyle.from_dict(styles)


def _task_browser_style_light() -> PTKStyle:
    styles = {
        "header": "bg:#e5e7eb #1f2937",
        "header.title": "bg:#e5e7eb #213853 bold",
        "header.meta": "bg:#e5e7eb #666666",
        "status.running": "bg:#e5e7eb #2C7A39 bold",
        "status.success": "bg:#e5e7eb #2C7A39",
        "status.warning": "bg:#e5e7eb #9A6B18",
        "status.error": "bg:#e5e7eb #C0392B",
        "status.info": "bg:#e5e7eb #176B7E",
        "task-list": "bg:#f9fafb #374151",
        "task-list.checked": "bg:#cffafe #164e63 bold",
        "frame.border": "#495F7C",
        "frame.label": "bg:#f1f5f9 #213853 bold",
        "footer": "bg:#f1f5f9 #475569",
        "footer.key": "bg:#f1f5f9 #176B7E bold",
        "footer.text": "bg:#f1f5f9 #475569",
        "footer.warning": "bg:#fee2e2 #C0392B bold",
        "footer.meta": "bg:#f1f5f9 #64748b",
    }
    if colors_disabled():
        styles = _strip_ptk_style_map(styles)
    return PTKStyle.from_dict(styles)


# ---------------------------------------------------------------------------
# Prompt / completion menu colors (used by ui/shell/prompt.py)
# ---------------------------------------------------------------------------


# Selection-row background (accent-family tint). Single source of truth for the
# prompt-toolkit completion/dialog selection styles below AND the `selected_bg`
# TuiTokens field — keep them wired so the two never drift.
_SELECTED_BG_DARK = "#21243B"
_SELECTED_BG_LIGHT = "#E7E9F9"

_PROMPT_STYLE_DARK = {
    "bottom-toolbar": "noreverse",
    # Input area — minimal: no background bar, only the prompt glyph is
    # colored. Lets the terminal background show through so the input row
    # reads as a single line of text rather than a chrome panel.
    "compact-input": "",
    "compact-input.prompt": "fg:#F4F4F5 bold",
    "compact-input.frame": "fg:#8a8d91",
    # Muted level word in the top-border effort label (the dot carries the color).
    "compact-input.effort": "fg:#A3A3A3",
    "running-prompt-placeholder": "fg:#A3A3A3 italic",
    "running-prompt-separator": "fg:#b8bcc0",
    # Recognized slash commands typed anywhere in the input area.
    "slash-command": "fg:#6CA1F5 bold",
    # "@file" path mentions typed in the input area.
    "file-mention": "fg:#56C7B0",
    # Leading "!" that turns the input into a one-shot shell command.
    "bash-prefix": "fg:#E5C07B bold",
    # Inline ghost text completing a partially typed slash command (Tab accepts).
    "auto-suggestion": "fg:#6B7280",
    # Slash completion menu — selected row gets the same selected-bg as cards.
    "slash-completion-menu": "",
    "slash-completion-menu.separator": "fg:#b8bcc0",
    "slash-completion-menu.marker": "fg:#b8bcc0",
    "slash-completion-menu.marker.current": "fg:#AFE3F1 bold",
    "slash-completion-menu.command": "fg:#F4F4F5",
    "slash-completion-menu.command.match": "fg:#AFE3F1 bold",
    "slash-completion-menu.meta": "fg:#A3A3A3",
    "slash-completion-menu.meta.success": "fg:#7BC97F",
    "slash-completion-menu.meta.warning": "fg:#B69B64",
    "slash-completion-menu.command.current": f"bg:{_SELECTED_BG_DARK} fg:#F4F4F5 bold",
    "slash-completion-menu.command.match.current": f"bg:{_SELECTED_BG_DARK} fg:#AFE3F1 bold",
    "slash-completion-menu.meta.current": f"bg:{_SELECTED_BG_DARK} fg:#A3A3A3",
    "slash-completion-menu.meta.success.current": f"bg:{_SELECTED_BG_DARK} fg:#7BC97F",
    "slash-completion-menu.meta.warning.current": f"bg:{_SELECTED_BG_DARK} fg:#B69B64",
    "slash-completion-menu.row.current": f"bg:{_SELECTED_BG_DARK}",
    "file-completion-menu": "",
    "file-completion-menu.marker": "fg:#b8bcc0",
    "file-completion-menu.marker.current": "fg:#AFE3F1 bold",
    "file-completion-menu.name": "fg:#A3A3A3",
    "file-completion-menu.name.current": "fg:#AFE3F1 bold",
    "file-completion-menu.detail": "fg:#A3A3A3",
    "file-completion-menu.detail.current": "fg:#AFE3F1",
    "file-completion-menu.count": "fg:#5F6B7E",
    "shell-dialog": "fg:#F4F4F5",
    "shell-dialog.title": "fg:#F4F4F5 bold",
    "shell-dialog.border": "fg:#b8bcc0",
    "shell-dialog.option": "fg:#A3A3A3",
    "shell-dialog.option.current": f"bg:{_SELECTED_BG_DARK} fg:#F4F4F5 bold",
    "shell-footer.key": "fg:#AFE3F1 bold",
    "shell-footer.meta": "fg:#A3A3A3",
    "shell-footer.warning": "fg:#E6B450",
    "shell-footer.error": "fg:#EF5E62",
}

_PROMPT_STYLE_LIGHT = {
    "bottom-toolbar": "noreverse",
    "compact-input": "",
    "compact-input.prompt": "fg:#213853 bold",
    "compact-input.frame": "fg:#495F7C",
    # Muted level word in the top-border effort label (the dot carries the color).
    "compact-input.effort": "fg:#666666",
    "running-prompt-placeholder": "fg:#666666 italic",
    "running-prompt-separator": "fg:#C8BEC0",
    # Recognized slash commands typed anywhere in the input area.
    "slash-command": "fg:#1D63D8 bold",
    # "@file" path mentions typed in the input area.
    "file-mention": "fg:#0E8C7A",
    # Leading "!" that turns the input into a one-shot shell command.
    "bash-prefix": "fg:#B45309 bold",
    # Inline ghost text completing a partially typed slash command (Tab accepts).
    "auto-suggestion": "fg:#8A93A0",
    "slash-completion-menu": "",
    "slash-completion-menu.separator": "fg:#C8BEC0",
    "slash-completion-menu.marker": "fg:#8A93A0",
    "slash-completion-menu.marker.current": "fg:#176B7E bold",
    "slash-completion-menu.command": "fg:#4b5563",
    "slash-completion-menu.command.match": "fg:#176B7E bold",
    "slash-completion-menu.meta": "fg:#666666",
    "slash-completion-menu.meta.success": "fg:#2C7A39",
    "slash-completion-menu.meta.warning": "fg:#9A6B18",
    "slash-completion-menu.command.current": f"bg:{_SELECTED_BG_LIGHT} fg:#213853 bold",
    "slash-completion-menu.command.match.current": f"bg:{_SELECTED_BG_LIGHT} fg:#176B7E bold",
    "slash-completion-menu.meta.current": f"bg:{_SELECTED_BG_LIGHT} fg:#666666",
    "slash-completion-menu.meta.success.current": f"bg:{_SELECTED_BG_LIGHT} fg:#2C7A39",
    "slash-completion-menu.meta.warning.current": f"bg:{_SELECTED_BG_LIGHT} fg:#9A6B18",
    "slash-completion-menu.row.current": f"bg:{_SELECTED_BG_LIGHT}",
    "file-completion-menu": "",
    "file-completion-menu.marker": "fg:#8A93A0",
    "file-completion-menu.marker.current": "fg:#176B7E bold",
    "file-completion-menu.name": "fg:#666666",
    "file-completion-menu.name.current": "fg:#176B7E bold",
    "file-completion-menu.detail": "fg:#666666",
    "file-completion-menu.detail.current": "fg:#176B7E",
    "file-completion-menu.count": "fg:#8A93A0",
    "shell-dialog": "fg:#374151",
    "shell-dialog.title": "fg:#213853 bold",
    "shell-dialog.border": "fg:#C8BEC0",
    "shell-dialog.option": "fg:#666666",
    "shell-dialog.option.current": f"bg:{_SELECTED_BG_LIGHT} fg:#213853 bold",
    "shell-footer.key": "fg:#176B7E bold",
    "shell-footer.meta": "fg:#666666",
    "shell-footer.warning": "fg:#9A6B18",
    "shell-footer.error": "fg:#C0392B",
}


# ---------------------------------------------------------------------------
# Bottom toolbar fragment colors (used by ui/shell/prompt.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolbarColors:
    separator: str
    yolo_label: str
    auto_label: str
    plan_label: str
    plan_prompt: str
    cwd: str
    bg_tasks: str
    tip: str
    tip_key: str


_TOOLBAR_DARK = ToolbarColors(
    separator="fg:#2B3A52",
    yolo_label="bold fg:#E6B450",
    auto_label="bold fg:#7BC97F",
    plan_label="bold fg:#AFE3F1",
    plan_prompt="fg:#AFE3F1",
    cwd="fg:#6F6F6F",
    bg_tasks="fg:#6F6F6F",
    tip="fg:#6F6F6F",
    tip_key="fg:#6F6F6F bold",
)

_TOOLBAR_LIGHT = ToolbarColors(
    separator="fg:#C8BEC0",
    yolo_label="bold fg:#9A6B18",
    auto_label="bold fg:#2C7A39",
    plan_label="bold fg:#176B7E",
    plan_prompt="fg:#176B7E",
    cwd="fg:#8A93A0",
    bg_tasks="fg:#666666",
    tip="fg:#666666",
    tip_key="fg:#666666 bold",
)


# ---------------------------------------------------------------------------
# Statusline v2 palette (used by ui/shell/statusline.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StatusLineColors:
    """Statusline v2 palette (prompt_toolkit style strings)."""

    model: str
    cost: str
    speed: str
    effort_hi: str
    effort_md: str
    effort_lo: str
    dir: str
    branch: str
    add: str
    delete: str
    label: str
    dim: str
    warn: str
    spinner: str
    spinner_idle: str
    time: str
    usage_ok: str
    usage_mid: str
    usage_high: str
    usage_crit: str


_STATUSLINE_DARK = StatusLineColors(
    model="bold fg:#dcb4ff",
    cost="fg:#ffc850",
    speed="fg:#78c8ff",
    effort_hi="fg:#78dc8c",
    effort_md="fg:#f0c850",
    effort_lo="fg:#8ca0b4",
    dir="fg:#82bef0",
    branch="fg:#64d2c8",
    add="fg:#78dc8c",
    delete="fg:#ff6e6e",
    label="fg:#a0a5b4",
    dim="fg:#505564",
    warn="bold fg:#ff5050",
    spinner="fg:#64b4ff",
    spinner_idle="fg:#505564",
    time="fg:#b4d2f0",
    usage_ok="fg:#64d2a0",
    usage_mid="fg:#f0c850",
    usage_high="fg:#ffa046",
    usage_crit="fg:#ff5050",
)

# Light variant: same hues darkened for contrast on light backgrounds.
_STATUSLINE_LIGHT = StatusLineColors(
    model="bold fg:#7a3fb0",
    cost="fg:#9a6b18",
    speed="fg:#1a6fb0",
    effort_hi="fg:#2c7a39",
    effort_md="fg:#9a6b18",
    effort_lo="fg:#5c6b7a",
    dir="fg:#2a6cb0",
    branch="fg:#17776b",
    add="fg:#2c7a39",
    delete="fg:#b03030",
    label="fg:#5c6370",
    dim="fg:#9aa0ac",
    warn="bold fg:#c01818",
    spinner="fg:#1a6fb0",
    spinner_idle="fg:#9aa0ac",
    time="fg:#3a5a80",
    usage_ok="fg:#2c7a39",
    usage_mid="fg:#9a6b18",
    usage_high="fg:#b05a10",
    usage_crit="fg:#c01818",
)


def get_statusline_colors() -> StatusLineColors:
    """Statusline palette for the active theme (dark default, light variant)."""
    colors = _STATUSLINE_LIGHT if _active_theme == "light" else _STATUSLINE_DARK
    return _strip_color_dataclass(colors) if colors_disabled() else colors


# ---------------------------------------------------------------------------
# Markdown / spinner palette (used by ui/shell markdown renderer and the
# turn-execution spinner). Foreground colors only; resolved to Rich styles
# by ``markdown_rich_style``. Values are Rich color names so they degrade
# gracefully on 16-color terminals.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarkdownColors:
    heading: str
    emphasis: str
    strong: str
    inline_code: str
    link: str
    quote: str
    ordered_marker: str
    unordered_marker: str
    table_border: str
    code_block_border: str
    code_block_bg: str
    spinner_active: str
    spinner_done: str
    spinner_failed: str


# Markdown/report role mapping. Headings/strong use primary text, emphasis and
# unordered bullets use muted grey, status accents stay green/red — all derived
# from TuiTokens. The four enumerated elements (inline code, links, blockquotes,
# ordered-list markers) instead use terminal-native ANSI names so they adapt to
# the user's terminal palette in both light and dark modes (see the design spec
# 2026-06-08).
def _build_markdown_colors(tokens: TuiTokens) -> MarkdownColors:
    return MarkdownColors(
        heading=tokens.tool_title,
        emphasis=tokens.muted,
        strong=tokens.tool_title,
        inline_code=tokens.accent,  # periwinkle accent — matches skill/branch highlight color
        link="cyan",  # cyan, rendered underlined
        quote="green",  # terminal-native ANSI green
        ordered_marker="bright_blue",  # ordered markers take the bright-blue accent
        unordered_marker=tokens.muted,  # unordered bullets stay muted
        table_border=tokens.border_muted,
        code_block_border=tokens.border_muted,
        code_block_bg=tokens.code_block_bg,
        spinner_active=tokens.info,
        spinner_done=tokens.success,
        spinner_failed=tokens.error,
    )


def get_markdown_colors(theme: ThemeName | None = None) -> MarkdownColors:
    name = theme if theme is not None else _active_theme
    tokens = _TUI_TOKENS_LIGHT if name == "light" else _TUI_TOKENS_DARK
    return _build_markdown_colors(tokens)


def markdown_rich_style(token: str, *, theme: ThemeName | None = None) -> RichStyle:
    """Resolve a MarkdownColors field name to a Rich Style.

    Background tokens (suffix ``_bg``) produce a style with ``bgcolor``;
    everything else produces a style with ``color``. Color is suppressed when
    the terminal environment requests plain output.
    """
    if colors_disabled():
        return RichStyle()
    colors = get_markdown_colors(theme)
    value = getattr(colors, token)
    if not value:
        return RichStyle()
    if token.endswith("_bg"):
        return RichStyle(bgcolor=value)
    return RichStyle(color=value)


# ---------------------------------------------------------------------------
# MCP status prompt colors (used by ui/shell/mcp_status.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MCPPromptColors:
    text: str
    detail: str
    connected: str
    connecting: str
    pending: str
    failed: str


_MCP_PROMPT_DARK = MCPPromptColors(
    text="fg:#d4d4d4",
    detail="fg:#A3A3A3",
    connected="fg:#7BC97F",
    connecting="fg:#AFE3F1",
    pending="fg:#E6B450",
    failed="fg:#EF5E62",
)

_MCP_PROMPT_LIGHT = MCPPromptColors(
    text="fg:#213853",
    detail="fg:#666666",
    connected="fg:#2C7A39",
    connecting="fg:#176B7E",
    pending="fg:#9A6B18",
    failed="fg:#C0392B",
)


# ---------------------------------------------------------------------------
# Public API — resolve by theme name
# ---------------------------------------------------------------------------

_active_theme: ThemeName = "dark"


def set_active_theme(theme: ThemeName) -> None:
    global _active_theme
    _active_theme = theme


def get_active_theme() -> ThemeName:
    return _active_theme


def get_diff_colors() -> DiffColors:
    if colors_disabled():
        return _DIFF_PLAIN
    if color_depth() == "16":
        return _DIFF_ANSI16
    return _DIFF_LIGHT if _active_theme == "light" else _DIFF_DARK


def get_task_browser_style() -> PTKStyle:
    return _task_browser_style_light() if _active_theme == "light" else _task_browser_style_dark()


def get_prompt_style() -> PTKStyle:
    d = _PROMPT_STYLE_LIGHT if _active_theme == "light" else _PROMPT_STYLE_DARK
    if colors_disabled():
        d = _strip_ptk_style_map(d)
    return PTKStyle.from_dict(d)


def get_toolbar_colors() -> ToolbarColors:
    colors = _TOOLBAR_LIGHT if _active_theme == "light" else _TOOLBAR_DARK
    return _strip_color_dataclass(colors) if colors_disabled() else colors


def get_mcp_prompt_colors() -> MCPPromptColors:
    colors = _MCP_PROMPT_LIGHT if _active_theme == "light" else _MCP_PROMPT_DARK
    return _strip_color_dataclass(colors) if colors_disabled() else colors


# ---------------------------------------------------------------------------
# Pythinker semantic TUI tokens (used by ui/shell/components/* and the tool
# renderer registry). Default semantic token palette
# and light themes so the Pythinker code path renders with the reference
# palette. Existing pythinker styles continue to work — these tokens add a
# parallel naming layer keyed by *semantic role* rather than concrete color.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TuiTokens:
    """Pythinker semantic theme tokens.

    Values are hex strings (``"#rrggbb"``) or the empty string for "use
    terminal default". Background tokens (``*_bg``) are intended for
    Rich ``bgcolor=`` arguments; foreground tokens for ``color=``.
    """

    # Core
    accent: str
    border: str
    border_accent: str
    border_muted: str
    info: str
    success: str
    error: str
    warning: str
    muted: str
    dim: str
    text: str
    thinking_text: str
    activity_label: str
    activity_verb: str
    activity_verb_mid: str
    activity_verb_highlight: str
    activity_spinner: str
    # Backgrounds
    selected_bg: str
    user_message_bg: str
    user_message_text: str
    custom_message_bg: str
    custom_message_text: str
    custom_message_label: str
    tool_pending_bg: str
    tool_error_bg: str
    tool_title: str
    tool_output: str
    # Diffs
    tool_diff_added: str
    tool_diff_removed: str
    tool_diff_context: str
    # Bash mode accent
    bash_mode: str
    # Code block background (used by markdown renderer)
    code_block_bg: str


TUI_TOKEN_NAMES = frozenset(field.name for field in fields(TuiTokens))


_TUI_TOKENS_DARK = TuiTokens(
    accent="#B3B9F4",
    border="#8a8d91",
    border_accent="#7C88DE",
    border_muted="#b8bcc0",
    info="#AFE3F1",
    success="#7BC97F",
    error="#EF5E62",
    warning="#E6B450",
    muted="#6F6F6F",
    dim="#5F5F5F",
    text="",
    thinking_text="#D4D4D4",
    activity_label="#F4F4F5",
    activity_verb="#C68D7E",
    activity_verb_mid="#D8AC9E",
    activity_verb_highlight="#E9CDC2",
    activity_spinner="#B8C0CC",
    selected_bg=_SELECTED_BG_DARK,
    user_message_bg="#333333",
    user_message_text="",
    custom_message_bg="#16242E",
    custom_message_text="",
    custom_message_label="#AFE3F1",
    tool_pending_bg="#1B2230",
    tool_error_bg="#2E1D24",
    tool_title="#F4F4F5",
    tool_output="#D4D4D4",
    tool_diff_added="#81C784",
    tool_diff_removed="#E57373",
    tool_diff_context="",  # match normal body text (terminal default fg), not muted grey
    bash_mode="#7BC97F",
    code_block_bg="#1f2030",
)


_TUI_TOKENS_LIGHT = TuiTokens(
    accent="#0B114E",
    border="#495F7C",
    border_accent="#3B469B",
    border_muted="#C8BEC0",
    info="#176B7E",
    success="#2C7A39",
    error="#C0392B",
    warning="#9A6B18",
    muted="#666666",
    dim="#8A93A0",
    text="#213853",
    thinking_text="#7A7A7A",
    activity_label="#213853",
    activity_verb="#B26A52",
    activity_verb_mid="#9E563E",
    activity_verb_highlight="#82412D",
    activity_spinner="#6B7280",
    selected_bg=_SELECTED_BG_LIGHT,
    user_message_bg="#E0E0E0",
    user_message_text="",
    custom_message_bg="#E6F2F6",
    custom_message_text="",
    custom_message_label="#176B7E",
    tool_pending_bg="#EFE7E8",
    tool_error_bg="#F6E3E3",
    tool_title="#213853",
    tool_output="#666666",
    tool_diff_added="#2C7A39",
    tool_diff_removed="#C0392B",
    tool_diff_context="#213853",  # match normal body text (theme `text`), not muted grey
    bash_mode="#2C7A39",
    code_block_bg="#f1f5f9",
)

# Pre-built markdown palettes derived from the canonical token instances.
_MARKDOWN_DARK = _build_markdown_colors(_TUI_TOKENS_DARK)
_MARKDOWN_LIGHT = _build_markdown_colors(_TUI_TOKENS_LIGHT)


def get_tui_tokens(theme: ThemeName | None = None) -> TuiTokens:
    """Return Pythinker semantic tokens for *theme* (defaults to active)."""
    name = theme if theme is not None else _active_theme
    return _TUI_TOKENS_LIGHT if name == "light" else _TUI_TOKENS_DARK


def tui_rich_style(token: str, *, theme: ThemeName | None = None) -> RichStyle:
    """Resolve a TuiTokens field name to a Rich Style.

    Background tokens (suffix ``_bg``) produce a style with ``bgcolor``;
    everything else produces a style with ``color``. Empty hex values
    (``""``) yield an empty style — Rich falls back to terminal defaults.
    Color is suppressed when the terminal environment requests plain output.

    Raises:
        ValueError: If *token* is not a known TuiTokens field.
    """
    if token not in TUI_TOKEN_NAMES:
        known = ", ".join(sorted(TUI_TOKEN_NAMES))
        raise ValueError(f"Unknown TUI token {token!r}. Known tokens: {known}")
    if colors_disabled():
        return RichStyle()
    tokens = get_tui_tokens(theme)
    value = getattr(tokens, token)
    if not value:
        return RichStyle()
    if token.endswith("_bg"):
        return RichStyle(bgcolor=value)
    return RichStyle(color=value)


# ---------------------------------------------------------------------------
# Thinking-level prompt frame colors (Shift+Tab cycle). Keyed by the plain level
# string to avoid a theme<->selector import cycle. ``minimal`` is the canonical
# ThinkingLevel value; ``min`` is accepted as the compact palette step alias.
# ---------------------------------------------------------------------------

# A single cold→hot gradient so the levels read as one dial: slate when off,
# cool blue/teal at low effort, warming amber/orange, ending on dark red.
_THINKING_FRAME_SCALE: dict[str, str] = {
    "off": "#64748b",  # muted grey / slate-500
    "min": "#60a5fa",  # cool blue / blue-400
    "minimal": "#60a5fa",  # canonical value for minimum
    "low": "#2dd4bf",  # teal / teal-400
    "medium": "#fbbf24",  # warm amber / amber-400
    "high": "#f97316",  # hot orange / orange-500
    "xhigh": "#b91c1c",  # dark red / red-700
    "max": "#7f1d1d",  # deepest red / red-900
}

_THINKING_FRAME_DARK: dict[str, str] = _THINKING_FRAME_SCALE
_THINKING_FRAME_LIGHT: dict[str, str] = _THINKING_FRAME_SCALE


def thinking_frame_color(level: str, *, theme: ThemeName | None = None) -> str:
    """Hex frame color for thinking *level*; unmapped levels fall back to ``border``."""
    name = theme if theme is not None else _active_theme
    table = _THINKING_FRAME_LIGHT if name == "light" else _THINKING_FRAME_DARK
    return table.get(level) or get_tui_tokens(theme).border


@lru_cache(maxsize=32)
def _dimmed_frame_hex(level: str, name: ThemeName) -> str:
    color = thinking_frame_color(level, theme=name)
    rgb = parse_hex_color(color)
    if rgb is not None:
        pole = (255, 255, 255) if name == "light" else (0, 0, 0)
        color = to_hex_color(blend(rgb, pole, 0.7))
    return color


def thinking_frame_style(level: str, *, theme: ThemeName | None = None) -> str:
    """prompt_toolkit input-bar style for *level*, or ``""`` when colors are off.

    The bars are chrome, not content: the level color is dimmed (blended
    toward the theme's background pole) so the input frame hints at the
    effort level without competing with the text being typed. The blend is
    cached — it is re-derived on every prompt_toolkit redraw otherwise.
    """
    if colors_disabled():
        return ""
    name = theme if theme is not None else _active_theme
    return f"fg:{_dimmed_frame_hex(level, name)}"


def thinking_dot_style(level: str, *, theme: ThemeName | None = None) -> str:
    """prompt_toolkit style for the small effort *dot* on the input top border.

    Unlike :func:`thinking_frame_style` (which dims the color because it paints
    a full-width bar), the dot is a single glyph, so it carries the level color
    at full strength — the one intentional accent on an otherwise static-grey
    border. Returns ``""`` when colors are disabled.
    """
    if colors_disabled():
        return ""
    return f"fg:{thinking_frame_color(level, theme=theme)}"
