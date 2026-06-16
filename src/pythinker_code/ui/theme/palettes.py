"""Canonical hex palettes — the only place UI color literals should live."""

from __future__ import annotations

from .pythinker_themes import DIFF_HEX_DARK, DIFF_HEX_LIGHT
from .spec import (
    BrandToken,
    MarkdownAnsiToken,
    MCPPromptColors,
    PromptToken,
    StatusLineColors,
    ThemeMode,
    ThemeSpec,
    ToolbarColors,
    TuiTokens,
)

# Shared selection-row backgrounds (wired to CoreToken.SELECTED_BG).
SELECTED_BG_DARK = "#252944"
SELECTED_BG_LIGHT = "#E7E9F9"

# Theme-independent robot mark (see BrandToken).
BRAND: dict[BrandToken, str] = {
    BrandToken.NAVY: "#213853",
    BrandToken.FACE: "#F9F2F5",
    BrandToken.CORAL: "#EE9983",
    BrandToken.CORAL_LIT: "#FFB9A3",
    BrandToken.IRIS: "#AFE3F1",
}

# Dark core tokens — WCAG AA on #141414 (see design spec §16).
_CORE_DARK: dict[str, str] = {
    "accent": "#A9B4FF",
    "border": "#9AA3AD",
    "border_accent": "#7C88DE",
    "border_muted": "#5D6570",
    "info": "#8FDDEA",
    "success": "#7CCF8A",
    "error": "#FF7A7A",
    "warning": "#FFD166",
    "muted": "#8F969E",
    "dim": "#6F767E",
    "secondary": "#AAB0B6",
    "text": "#D7DBDF",
    "thinking_text": "#D4D4D4",
    "activity_label": "#F1F3F5",
    "activity_verb": "#C68D7E",
    "activity_verb_mid": "#D8AC9E",
    "activity_verb_highlight": "#E9CDC2",
    "activity_spinner": "#A8ADB4",
    "selected_bg": SELECTED_BG_DARK,
    "user_message_bg": "#333333",
    "user_message_text": "",
    "custom_message_bg": "#16242E",
    "custom_message_text": "",
    "custom_message_label": "#8FDDEA",
    "tool_pending_bg": "#1B2230",
    "tool_error_bg": "#2E1D24",
    "tool_title": "#F1F3F5",
    "tool_output": "#D7DBDF",
    "tool_diff_added": "#81C784",
    "tool_diff_removed": "#E57373",
    "tool_diff_context": "",
    "bash_mode": "#7CCF8A",
    "code_block_bg": "#1B1D2B",
}

_CORE_LIGHT: dict[str, str] = {
    "accent": "#0B114E",
    "border": "#495F7C",
    "border_accent": "#3B469B",
    "border_muted": "#C8BEC0",
    "info": "#176B7E",
    "success": "#2C7A39",
    "error": "#C0392B",
    "warning": "#9A6B18",
    "muted": "#666666",
    "dim": "#8A93A0",
    "secondary": "#8A93A0",
    "text": "#213853",
    "thinking_text": "#7A7A7A",
    "activity_label": "#213853",
    "activity_verb": "#B26A52",
    "activity_verb_mid": "#9E563E",
    "activity_verb_highlight": "#82412D",
    "activity_spinner": "#6B7280",
    "selected_bg": SELECTED_BG_LIGHT,
    "user_message_bg": "#E0E0E0",
    "user_message_text": "",
    "custom_message_bg": "#E6F2F6",
    "custom_message_text": "",
    "custom_message_label": "#176B7E",
    "tool_pending_bg": "#EFE7E8",
    "tool_error_bg": "#F6E3E3",
    "tool_title": "#213853",
    "tool_output": "#666666",
    "tool_diff_added": "#2C7A39",
    "tool_diff_removed": "#C0392B",
    "tool_diff_context": "#213853",
    "bash_mode": "#2C7A39",
    "code_block_bg": "#f1f5f9",
}

_PROMPT_HEX_DARK: dict[PromptToken, str] = {
    PromptToken.SLASH_COMMAND: "#6CA1F5",
    PromptToken.MENTION: "#56C7B0",
    PromptToken.BASH_PREFIX: "#E5C07B",
    PromptToken.GHOST_TEXT: "#6B7280",
    PromptToken.PROMPT_GLYPH: "#F1F3F5",
    # Border-family prompt tokens track the canonical core tokens so the
    # prompt_toolkit and Rich layers render identical border hues (enforced by
    # test_dark_theme_ptk_border_tracks_token).
    PromptToken.FRAME: "#9AA3AD",  # == _CORE_DARK["border"]
    PromptToken.EFFORT: "#A3A3A3",
    PromptToken.PLACEHOLDER: "#A3A3A3",
    PromptToken.SEPARATOR: "#5D6570",  # == _CORE_DARK["border_muted"]
    PromptToken.MENU_MATCH: "#8FDDEA",
    PromptToken.MENU_TEXT: "#F4F4F5",
    PromptToken.MENU_META: "#A3A3A3",
    PromptToken.DIALOG_TEXT: "#F4F4F5",
    PromptToken.DIALOG_BORDER: "#5D6570",  # == _CORE_DARK["border_muted"]
    PromptToken.FOOTER_KEY: "#8FDDEA",
    PromptToken.FOOTER_META: "#A3A3A3",
}

_PROMPT_HEX_LIGHT: dict[PromptToken, str] = {
    PromptToken.SLASH_COMMAND: "#1D63D8",
    PromptToken.MENTION: "#0E8C7A",
    PromptToken.BASH_PREFIX: "#B45309",
    PromptToken.GHOST_TEXT: "#8A93A0",
    PromptToken.PROMPT_GLYPH: "#213853",
    PromptToken.FRAME: "#495F7C",
    PromptToken.EFFORT: "#666666",
    PromptToken.PLACEHOLDER: "#666666",
    PromptToken.SEPARATOR: "#C8BEC0",
    PromptToken.MENU_MATCH: "#176B7E",
    PromptToken.MENU_TEXT: "#4b5563",
    PromptToken.MENU_META: "#666666",
    PromptToken.DIALOG_TEXT: "#374151",
    PromptToken.DIALOG_BORDER: "#C8BEC0",
    PromptToken.FOOTER_KEY: "#176B7E",
    PromptToken.FOOTER_META: "#666666",
}

_MARKDOWN_ANSI = {
    MarkdownAnsiToken.LINK: "bright_blue",
    MarkdownAnsiToken.QUOTE: "green",
    MarkdownAnsiToken.ORDERED_MARKER: "bright_blue",
}

_THINKING_FRAME_SCALE: dict[str, str] = {
    "off": "#64748b",
    "min": "#60a5fa",
    "minimal": "#60a5fa",
    "low": "#2dd4bf",
    "medium": "#fbbf24",
    "high": "#f97316",
    "xhigh": "#b91c1c",
    "max": "#7f1d1d",
}


def _tokens_from_core(core: dict[str, str]) -> TuiTokens:
    return TuiTokens(**core)


def _statusline(mode: ThemeMode) -> StatusLineColors:
    if mode is ThemeMode.LIGHT:
        return StatusLineColors(
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
            usage_ok="fg:#9aa0ac",
            usage_mid="fg:#9a6b18",
            usage_high="fg:#b05a10",
            usage_crit="fg:#c01818",
        )
    return StatusLineColors(
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
        usage_ok="fg:#505564",
        usage_mid="fg:#f0c850",
        usage_high="fg:#ffa046",
        usage_crit="fg:#ff5050",
    )


def _toolbar(mode: ThemeMode, tokens: TuiTokens) -> ToolbarColors:
    if mode is ThemeMode.LIGHT:
        return ToolbarColors(
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
    return ToolbarColors(
        separator="fg:#2B3A52",
        yolo_label="bold fg:#EAB85F",
        auto_label="bold fg:#7CCF8A",
        plan_label="bold fg:#8FDDEA",
        plan_prompt="fg:#8FDDEA",
        cwd=f"fg:{tokens.muted}",
        bg_tasks=f"fg:{tokens.muted}",
        tip=f"fg:{tokens.muted}",
        tip_key=f"fg:{tokens.muted} bold",
    )


def _mcp(mode: ThemeMode, tokens: TuiTokens) -> MCPPromptColors:
    if mode is ThemeMode.LIGHT:
        return MCPPromptColors(
            text="fg:#213853",
            detail="fg:#666666",
            connected="fg:#2C7A39",
            connecting="fg:#176B7E",
            pending="fg:#9A6B18",
            failed="fg:#C0392B",
        )
    return MCPPromptColors(
        text="fg:#d4d4d4",
        detail="fg:#A3A3A3",
        connected=f"fg:{tokens.success}",
        connecting=f"fg:{tokens.info}",
        pending=f"fg:{tokens.warning}",
        failed=f"fg:{tokens.error}",
    )


def _build_prompt_classes(
    mode: ThemeMode,
    prompt: dict[PromptToken, str],
    tokens: TuiTokens,
) -> dict[str, str]:
    selected_bg = tokens.selected_bg
    p = prompt
    success = tokens.success
    menu_warning = "#B69B64" if mode is ThemeMode.DARK else "#9A6B18"
    dialog_title = p[PromptToken.MENU_TEXT] if mode is ThemeMode.DARK else tokens.tool_title
    dialog_option = p[PromptToken.MENU_META]
    footer_warning = tokens.warning if mode is ThemeMode.DARK else "#9A6B18"
    footer_error = tokens.error if mode is ThemeMode.DARK else "#C0392B"
    return {
        "bottom-toolbar": "noreverse",
        "compact-input": "",
        "compact-input.prompt": f"fg:{p[PromptToken.PROMPT_GLYPH]} bold",
        "compact-input.frame": f"fg:{p[PromptToken.FRAME]}",
        "compact-input.effort": f"fg:{p[PromptToken.EFFORT]}",
        "running-prompt-placeholder": f"fg:{p[PromptToken.PLACEHOLDER]} italic",
        "running-prompt-separator": f"fg:{p[PromptToken.SEPARATOR]}",
        "slash-command": f"fg:{p[PromptToken.SLASH_COMMAND]}",
        "slash-arg": f"fg:{p[PromptToken.SLASH_COMMAND]}",
        "file-mention": f"fg:{p[PromptToken.MENTION]}",
        "bash-prefix": f"fg:{p[PromptToken.BASH_PREFIX]}",
        "auto-suggestion": f"fg:{p[PromptToken.GHOST_TEXT]}",
        "slash-completion-menu": "",
        "slash-completion-menu.separator": f"fg:{p[PromptToken.SEPARATOR]}",
        "slash-completion-menu.marker": f"fg:{p[PromptToken.SEPARATOR]}",
        "slash-completion-menu.marker.current": f"fg:{p[PromptToken.MENU_MATCH]} bold",
        "slash-completion-menu.command": f"fg:{p[PromptToken.MENU_TEXT]}",
        "slash-completion-menu.command.match": f"fg:{p[PromptToken.MENU_MATCH]} bold",
        "slash-completion-menu.meta": f"fg:{p[PromptToken.MENU_META]}",
        "slash-completion-menu.meta.success": f"fg:{success}",
        "slash-completion-menu.meta.warning": f"fg:{menu_warning}",
        "slash-completion-menu.command.current": (
            f"bg:{selected_bg} fg:{p[PromptToken.MENU_TEXT]} bold"
        ),
        "slash-completion-menu.command.match.current": (
            f"bg:{selected_bg} fg:{p[PromptToken.MENU_MATCH]} bold"
        ),
        "slash-completion-menu.meta.current": f"bg:{selected_bg} fg:{p[PromptToken.MENU_META]}",
        "slash-completion-menu.meta.success.current": f"bg:{selected_bg} fg:{success}",
        "slash-completion-menu.meta.warning.current": f"bg:{selected_bg} fg:{menu_warning}",
        "slash-completion-menu.row.current": f"bg:{selected_bg}",
        "file-completion-menu": "",
        "file-completion-menu.marker": f"fg:{p[PromptToken.SEPARATOR]}",
        "file-completion-menu.marker.current": f"fg:{p[PromptToken.MENU_MATCH]} bold",
        "file-completion-menu.name": f"fg:{p[PromptToken.MENU_META]}",
        "file-completion-menu.name.current": f"fg:{p[PromptToken.MENU_MATCH]} bold",
        "file-completion-menu.detail": f"fg:{p[PromptToken.MENU_META]}",
        "file-completion-menu.detail.current": f"fg:{p[PromptToken.MENU_MATCH]}",
        "file-completion-menu.count": "fg:#5F6B7E" if mode is ThemeMode.DARK else "fg:#8A93A0",
        "shell-dialog": f"fg:{p[PromptToken.DIALOG_TEXT]}",
        "shell-dialog.title": f"fg:{dialog_title} bold",
        "shell-dialog.border": f"fg:{p[PromptToken.DIALOG_BORDER]}",
        "shell-dialog.option": f"fg:{dialog_option}",
        "shell-dialog.option.current": f"bg:{selected_bg} fg:{p[PromptToken.MENU_TEXT]} bold",
        "shell-footer.key": f"fg:{p[PromptToken.FOOTER_KEY]} bold",
        "shell-footer.meta": f"fg:{p[PromptToken.FOOTER_META]}",
        "shell-footer.warning": f"fg:{footer_warning}",
        "shell-footer.error": f"fg:{footer_error}",
    }


def build_theme_spec(mode: ThemeMode) -> ThemeSpec:
    core = _CORE_DARK if mode is ThemeMode.DARK else _CORE_LIGHT
    tokens = _tokens_from_core(core)
    prompt = _PROMPT_HEX_DARK if mode is ThemeMode.DARK else _PROMPT_HEX_LIGHT
    return ThemeSpec(
        mode=mode,
        tokens=tokens,
        prompt=dict(prompt),
        prompt_classes=_build_prompt_classes(mode, prompt, tokens),
        status=_statusline(mode),
        toolbar=_toolbar(mode, tokens),
        mcp=_mcp(mode, tokens),
        brand=dict(BRAND),
        markdown_ansi=dict(_MARKDOWN_ANSI),
        diff_hex=DIFF_HEX_DARK if mode is ThemeMode.DARK else DIFF_HEX_LIGHT,
        thinking_frame=dict(_THINKING_FRAME_SCALE),
    )


THEME_SPECS: dict[ThemeMode, ThemeSpec] = {
    ThemeMode.DARK: build_theme_spec(ThemeMode.DARK),
    ThemeMode.LIGHT: build_theme_spec(ThemeMode.LIGHT),
}

# Back-compat aliases for tests that import private prompt style dicts.
PROMPT_STYLE_DARK = THEME_SPECS[ThemeMode.DARK].prompt_classes
PROMPT_STYLE_LIGHT = THEME_SPECS[ThemeMode.LIGHT].prompt_classes
