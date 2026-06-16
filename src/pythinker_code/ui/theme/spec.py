"""Theme token enums and spec types."""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Literal

from rich.style import Style as RichStyle

type ThemeName = Literal["dark", "light"]


class ThemeMode(StrEnum):
    DARK = "dark"
    LIGHT = "light"


class CoreToken(StrEnum):
    """Semantic Rich/TUI tokens — values map to ``TuiTokens`` field names."""

    ACCENT = "accent"
    BORDER = "border"
    BORDER_ACCENT = "border_accent"
    BORDER_MUTED = "border_muted"
    INFO = "info"
    SUCCESS = "success"
    ERROR = "error"
    WARNING = "warning"
    MUTED = "muted"
    DIM = "dim"
    TEXT = "text"
    THINKING_TEXT = "thinking_text"
    ACTIVITY_LABEL = "activity_label"
    ACTIVITY_VERB = "activity_verb"
    ACTIVITY_VERB_MID = "activity_verb_mid"
    ACTIVITY_VERB_HIGHLIGHT = "activity_verb_highlight"
    ACTIVITY_SPINNER = "activity_spinner"
    SELECTED_BG = "selected_bg"
    USER_MESSAGE_BG = "user_message_bg"
    USER_MESSAGE_TEXT = "user_message_text"
    CUSTOM_MESSAGE_BG = "custom_message_bg"
    CUSTOM_MESSAGE_TEXT = "custom_message_text"
    CUSTOM_MESSAGE_LABEL = "custom_message_label"
    TOOL_PENDING_BG = "tool_pending_bg"
    TOOL_ERROR_BG = "tool_error_bg"
    TOOL_TITLE = "tool_title"
    TOOL_OUTPUT = "tool_output"
    TOOL_DIFF_ADDED = "tool_diff_added"
    TOOL_DIFF_REMOVED = "tool_diff_removed"
    TOOL_DIFF_CONTEXT = "tool_diff_context"
    BASH_MODE = "bash_mode"
    CODE_BLOCK_BG = "code_block_bg"


class PromptToken(StrEnum):
    SLASH_COMMAND = "slash_command"
    MENTION = "mention"
    BASH_PREFIX = "bash_prefix"
    GHOST_TEXT = "ghost_text"
    PROMPT_GLYPH = "prompt_glyph"
    FRAME = "frame"
    EFFORT = "effort"
    PLACEHOLDER = "placeholder"
    SEPARATOR = "separator"
    MENU_MATCH = "menu_match"
    MENU_TEXT = "menu_text"
    MENU_META = "menu_meta"
    DIALOG_TEXT = "dialog_text"
    DIALOG_BORDER = "dialog_border"
    FOOTER_KEY = "footer_key"
    FOOTER_META = "footer_meta"


class BrandToken(StrEnum):
    NAVY = "navy"
    FACE = "face"
    CORAL = "coral"
    CORAL_LIT = "coral_lit"
    IRIS = "iris"


class MarkdownAnsiToken(StrEnum):
    LINK = "link"
    QUOTE = "quote"
    ORDERED_MARKER = "ordered_marker"


@dataclass(frozen=True, slots=True)
class TuiTokens:
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
    tool_diff_added: str
    tool_diff_removed: str
    tool_diff_context: str
    bash_mode: str
    code_block_bg: str


TUI_TOKEN_NAMES = frozenset(field.name for field in fields(TuiTokens))
CORE_TOKEN_BY_FIELD = {token.value: token for token in CoreToken}


@dataclass(frozen=True, slots=True)
class DiffColors:
    add_bg: RichStyle
    del_bg: RichStyle
    add_hl: RichStyle
    del_hl: RichStyle


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


@dataclass(frozen=True, slots=True)
class StatusLineColors:
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


@dataclass(frozen=True, slots=True)
class MCPPromptColors:
    text: str
    detail: str
    connected: str
    connecting: str
    pending: str
    failed: str


@dataclass(frozen=True, slots=True)
class ThemeSpec:
    """Single source of truth for one resolved theme mode."""

    mode: ThemeMode
    tokens: TuiTokens
    prompt: dict[PromptToken, str]
    prompt_classes: dict[str, str]
    status: StatusLineColors
    toolbar: ToolbarColors
    mcp: MCPPromptColors
    brand: dict[BrandToken, str]
    markdown_ansi: dict[MarkdownAnsiToken, str]
    diff_hex: dict[str, str]
    thinking_frame: dict[str, str]
