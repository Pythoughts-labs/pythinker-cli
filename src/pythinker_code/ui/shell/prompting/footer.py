"""Immutable footer snapshots and theme-neutral content policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from prompt_toolkit.utils import get_cwidth

from pythinker_code.ui.shell.prompting.toasts import ToastSnapshot
from pythinker_code.ui.shell.statusline import StatusLineContext

FooterContentKind = Literal["command", "extension", "background", "toast"]


@dataclass(frozen=True, slots=True)
class FooterViewModel:
    """All dynamic data consumed while rendering one prompt footer."""

    status: StatusLineContext
    command_line: str
    extension_statuses: tuple[tuple[str, str], ...]
    background_summary: str
    toast: ToastSnapshot | None
    update_notice: str | None
    # Left- and right-positioned toasts can be active at once; the right toast is
    # rendered by the right span, so it must be carried separately from ``toast``
    # (which feeds left-side content precedence) or it would be dropped.
    toast_right: ToastSnapshot | None = None


@dataclass(frozen=True, slots=True)
class FooterContent:
    """The winning left-side footer content after applying shared precedence."""

    kind: FooterContentKind
    text: str
    style: str = ""


def background_task_summary(*, bash: int, agent: int) -> str:
    """Return the stable user-facing summary for active background work."""
    total = bash + agent
    if total <= 0:
        return ""
    noun = "background task" if total == 1 else "background tasks"
    parts: list[str] = []
    if bash:
        parts.append(f"{bash} bash")
    if agent:
        parts.append(f"{agent} agent")
    detail = f" ({', '.join(parts)})" if parts else ""
    return f"{total} {noun} running{detail} · /task to view"


def select_footer_content(model: FooterViewModel) -> FooterContent | None:
    """Choose left content using command, extension, background, toast precedence."""
    if model.command_line:
        return FooterContent("command", model.command_line)
    if model.extension_statuses:
        text = " ".join(f"{key}:{value}" for key, value in model.extension_statuses)
        return FooterContent("extension", text)
    if model.background_summary:
        return FooterContent("background", model.background_summary)
    if model.toast is not None and model.toast.position == "left":
        return FooterContent("toast", model.toast.message, model.toast.style)
    return None


def _display_width(text: str) -> int:
    return sum(get_cwidth(character) for character in text)


def truncate_footer_right(text: str, width: int, *, ascii_only: bool) -> str:
    """Fit footer text from the right with a capability-safe ellipsis."""
    if width <= 0:
        return ""
    if _display_width(text) <= width:
        return text
    ellipsis = "..." if ascii_only else "…"
    ellipsis_width = _display_width(ellipsis)
    if width <= ellipsis_width:
        return "." * width
    budget = width - ellipsis_width
    chars: list[str] = []
    used = 0
    for character in text:
        char_width = get_cwidth(character)
        if used + char_width > budget:
            break
        chars.append(character)
        used += char_width
    return "".join(chars) + ellipsis


def truncate_footer_left(text: str, width: int, *, ascii_only: bool) -> str:
    """Fit footer text from the left with a capability-safe ellipsis."""
    if width <= 0:
        return ""
    if _display_width(text) <= width:
        return text
    ellipsis = "..." if ascii_only else "…"
    ellipsis_width = _display_width(ellipsis)
    if width <= ellipsis_width:
        return "." * width
    budget = width - ellipsis_width
    chars: list[str] = []
    used = 0
    for character in reversed(text):
        char_width = get_cwidth(character)
        if used + char_width > budget:
            break
        chars.append(character)
        used += char_width
    return ellipsis + "".join(reversed(chars))


__all__ = (
    "FooterContent",
    "FooterContentKind",
    "FooterViewModel",
    "background_task_summary",
    "select_footer_content",
    "truncate_footer_left",
    "truncate_footer_right",
)
