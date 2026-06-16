"""prompt_toolkit adapters."""

from __future__ import annotations

from prompt_toolkit.styles import Style as PTKStyle

from pythinker_code.ui.terminal_capabilities import colors_disabled

from ..palettes import THEME_SPECS
from ..registry import strip_ptk_style_map
from ..spec import ThemeMode


def _fg(hex_color: str) -> str:
    return f"fg:{hex_color}" if hex_color else ""


def build_task_browser_style(mode: ThemeMode) -> PTKStyle:
    tokens = THEME_SPECS[mode].tokens
    if mode is ThemeMode.LIGHT:
        styles = {
            "header": "bg:#e5e7eb #1f2937",
            "header.title": f"bg:#e5e7eb {_fg(tokens.tool_title)} bold",
            "header.meta": "bg:#e5e7eb #666666",
            "status.running": f"bg:#e5e7eb {_fg(tokens.success)} bold",
            "status.success": f"bg:#e5e7eb {_fg(tokens.success)}",
            "status.warning": f"bg:#e5e7eb {_fg(tokens.warning)}",
            "status.error": f"bg:#e5e7eb {_fg(tokens.error)}",
            "status.info": f"bg:#e5e7eb {_fg(tokens.info)}",
            "task-list": "bg:#f9fafb #374151",
            "task-list.checked": "bg:#cffafe #164e63 bold",
            "frame.border": tokens.border,
            "frame.label": f"bg:#f1f5f9 {_fg(tokens.tool_title)} bold",
            "footer": "bg:#f1f5f9 #475569",
            "footer.key": f"bg:#f1f5f9 {_fg(tokens.info)} bold",
            "footer.text": "bg:#f1f5f9 #475569",
            "footer.warning": f"bg:#fee2e2 {_fg(tokens.error)} bold",
            "footer.meta": "bg:#f1f5f9 #64748b",
        }
    else:
        styles = {
            "header": "bg:#1f2937 #e5e7eb",
            "header.title": f"bg:#1f2937 {_fg(tokens.tool_title)} bold",
            "header.meta": "bg:#1f2937 #A3A3A3",
            "status.running": f"bg:#1f2937 {_fg(tokens.success)} bold",
            "status.success": f"bg:#1f2937 {_fg(tokens.success)}",
            "status.warning": f"bg:#1f2937 {_fg(tokens.warning)}",
            "status.error": f"bg:#1f2937 {_fg(tokens.error)}",
            "status.info": f"bg:#1f2937 {_fg(tokens.info)}",
            "task-list": "bg:#111827 #d1d5db",
            "task-list.checked": "bg:#164e63 #ecfeff bold",
            "frame.border": "#3A506D",
            "frame.label": f"bg:#17182a {_fg(tokens.tool_title)} bold",
            "footer": "bg:#17182a #A3A3A3",
            "footer.key": f"bg:#17182a {_fg(tokens.info)} bold",
            "footer.text": "bg:#17182a #A3A3A3",
            "footer.warning": f"bg:#4a3315 {_fg(tokens.warning)} bold",
            "footer.meta": "bg:#17182a #5F6B7E",
        }
    if colors_disabled():
        styles = strip_ptk_style_map(styles)
    return PTKStyle.from_dict(styles)
