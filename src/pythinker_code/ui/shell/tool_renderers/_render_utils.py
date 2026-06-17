"""Shared helpers for Pythinker tool renderers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.style import Style as RichStyle
from rich.table import Table
from rich.text import Text

from pythinker_code.ui.shell.components import sanitize_ansi
from pythinker_code.ui.shell.glyphs import TRANSCRIPT_ASSISTANT_MARKER
from pythinker_code.ui.shell.motion import blink_visible
from pythinker_code.ui.shell.render_constants import LISTING_LINE_NUMBER_MIN_WIDTH
from pythinker_code.ui.theme import tui_rich_style

__all__ = [
    "as_str",
    "fg",
    "fg_subject",
    "format_lines_block",
    "format_numbered_lines_block",
    "invalid_arg",
    "missing_required_arg",
    "loading_marker",
    "pending_tool_call_header",
    "running_spinner",
    "safe_arg_keys_summary",
    "shorten_path",
    "tab_to_spaces",
    "tool_call_header",
    "tool_title",
]


def as_str(value: Any) -> str | None:
    """the ``str(...)`` helper: keep strings, return ``None`` for anything else.

    The renderer treats ``None`` as "missing" (placeholder) and a non-string
    value as "invalid".
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


def fg(token: str, content: str | Text) -> Text:
    """Wrap *content* in a Rich Text styled by a TUI theme *token*.

    Mirrors the ``theme.fg(token, text)``. ``content`` may already be a
    ``Text`` (style is applied non-destructively).
    """
    style = tui_rich_style(token)
    if isinstance(content, Text):
        out = content.copy()
        out.stylize(style)
        return out
    return Text(sanitize_ansi(content), style=style)


def fg_subject(content: str | Text) -> Text:
    """Brand periwinkle highlight for tool-call subjects (paths, patterns, URLs)."""
    return fg("accent", content)


def tool_title(label: str) -> Text:
    """Bold tool-name title ."""
    base = tui_rich_style("tool_title")
    return Text(sanitize_ansi(label), style=base + RichStyle(bold=True))


def _status_marker(style_token: str) -> str:
    return "✘" if style_token == "error" else TRANSCRIPT_ASSISTANT_MARKER


# Normalized agent/task status labels for user-facing TUI rows.
AGENT_STATUS_LABELS: dict[str, str] = {
    "queued": "queued",
    "created": "queued",
    "starting": "starting",
    "running": "running",
    "launched": "running",
    "awaiting_approval": "waiting",
    "waiting": "waiting",
    "completed": "completed",
    "success": "completed",
    "succeeded": "completed",
    "failed": "failed",
    "failure": "failed",
    "error": "failed",
    "cancelled": "cancelled",
    "killed": "cancelled",
    "timed_out": "timed out",
    "deferred": "queued",
    "lost": "failed",
    "recoverable": "failed",
}


def normalize_agent_status(status: str) -> str:
    """Map backend status strings to a single user-facing label."""
    normalized = status.strip().lower().replace(" ", "_")
    return AGENT_STATUS_LABELS.get(normalized, status.strip() or "unknown")


def agent_status_glyph(status: str) -> str:
    """Icon for an agent/task row based on normalized status."""
    label = normalize_agent_status(status)
    if label in {"failed", "timed out"}:
        return "✘"
    if label == "completed":
        return "✓"
    if label in {"starting", "running", "waiting", "queued"}:
        return "●"
    if label == "cancelled":
        return "○"
    return "○"


def agent_status_style_token(status: str) -> str:
    """Theme token for a normalized agent/task status."""
    label = normalize_agent_status(status)
    if label in {"failed", "timed out"}:
        return "error"
    if label == "completed":
        return "success"
    if label in {"starting", "running", "waiting", "queued"}:
        return "success"
    if label == "cancelled":
        return "muted"
    return "muted"


def format_byte_size(num_bytes: int) -> str:
    """Human-readable byte size (KB/MB)."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def shorten_home_path(path: str) -> str:
    """Shorten an absolute path with ``~/`` when under the user home directory."""
    if not path:
        return path
    try:
        resolved = Path(path).expanduser().resolve()
        home = Path.home().resolve()
        if resolved == home:
            return "~"
        if resolved.is_relative_to(home):
            return f"~/{resolved.relative_to(home)}"
    except (OSError, RuntimeError, ValueError):
        return path
    return path


def tool_call_header(
    name: str,
    summary: str | Text | None = None,
    *,
    style_token: str = "success",
    summary_style_token: str = "muted",
) -> Text:
    """Return the compact tool-use row.

    Shape: ``⏺ Tool(summary)`` for completed rows, ``✘ Tool(summary)`` for
    failed rows (reference-CLI parenthesized form). The surrounding
    ``ToolExecutionComponent`` owns result gutters; individual renderers
    should keep this row compact.
    """
    header = Text()
    header.append(f"{_status_marker(style_token)} ", style=tui_rich_style(style_token))
    header.append_text(tool_title(name))
    if summary is not None:
        header.append("(", style=tui_rich_style("muted"))
        if isinstance(summary, Text):
            header.append_text(summary)
        else:
            header.append(sanitize_ansi(summary), style=tui_rich_style(summary_style_token))
        header.append(")", style=tui_rich_style("muted"))
    # Single-line contract (reference CLI): long summaries ellipsize at the
    # terminal edge instead of wrapping the header onto a second row.
    header.no_wrap = True
    header.overflow = "ellipsis"
    return header


def pending_tool_call_header(
    name: str,
    *,
    action: str = "Preparing",
    style_token: str = "muted",
    action_style_token: str = "muted",
) -> Text:
    """Return a safe pending row while streamed args are still incomplete.

    This deliberately avoids ``Tool(...)`` placeholders and never renders raw
    argument values. Once a renderer has the required safe summary field it can
    switch to ``tool_call_header``.
    """
    header = Text()
    header.append(f"{TRANSCRIPT_ASSISTANT_MARKER} ", style=tui_rich_style(style_token))
    header.append(f"{action} ", style=tui_rich_style(action_style_token))
    header.append_text(tool_title(name))
    header.append("…", style=tui_rich_style(action_style_token))
    return header


def safe_arg_keys_summary(args: dict[str, Any], *, max_keys: int = 4) -> Text | None:
    """Summarize unknown-tool args without echoing values.

    Generic renderers cannot know which values are huge or sensitive, so they
    show only a compact key/count summary.
    """
    if not args:
        return None
    keys = sorted(str(key) for key in args)
    shown = keys[:max_keys]
    hidden = len(keys) - len(shown)
    label = "arg" if len(keys) == 1 else "args"
    summary = Text(f"{len(keys)} {label}", style=tui_rich_style("muted"))
    if shown:
        summary.append(": ", style=tui_rich_style("muted"))
        # Arg key names are model/MCP-controlled; strip ANSI so a crafted key like
        # "\x1b]0;title\x07" can't drive the terminal through the Text-summary path
        # (tool_call_header appends a Text summary verbatim).
        summary.append(sanitize_ansi(", ".join(shown)), style=tui_rich_style("tool_output"))
    if hidden > 0:
        summary.append(f", +{hidden} more", style=tui_rich_style("muted"))
    return summary


def loading_marker(
    *,
    done: bool = False,
    pulse: bool = True,
    now: float | None = None,
    style_token: str = "muted",
) -> Text:
    """Return the app-wide task marker.

    Running tasks pulse the transcript marker in muted grey; completed tasks
    show the same marker as a static green dot. The animated braille spinner stays
    reserved for activity/status lines such as the pinned todo header.
    """
    if done:
        return Text(f"{TRANSCRIPT_ASSISTANT_MARKER} ", style=tui_rich_style("success"))
    if not pulse:
        return Text(f"{TRANSCRIPT_ASSISTANT_MARKER} ", style=tui_rich_style(style_token))
    glyph = TRANSCRIPT_ASSISTANT_MARKER if blink_visible(now) else " "
    return Text(f"{glyph} ", style=tui_rich_style(style_token))


def running_spinner(
    renderable: RenderableType,
    *,
    execution_started: bool,
    has_result: bool,
    marker_style_token: str = "muted",
) -> RenderableType:
    """Wrap *renderable* in the animated tool marker while executing.

    Tool-use headers already render a static transcript marker for completed/error states.
    While a tool is running, that static marker would sit immediately after the
    animated marker. Strip the static header marker in the
    running state and place the content in a two-column grid so wrapped command
    text remains indented under the tool text instead of jumping to column 0.
    """
    if not (execution_started and not has_result):
        return renderable

    marker = loading_marker(style_token=marker_style_token)
    content = _strip_running_static_marker(renderable)
    table = Table.grid(padding=0)
    table.add_column(width=2, no_wrap=True)
    table.add_column(ratio=1)
    table.add_row(marker, content)
    return Group(table)


def _strip_running_static_marker(renderable: RenderableType) -> RenderableType:
    """Remove one leading static bullet from the first visible child.

    Most tool renderers now use ``tool_call_header()`` for completed states.
    Running rows add their animated marker outside the renderer, so the static
    header marker must be stripped even when the renderer returns a ``Group``
    or ``Padding`` around the header and follow-up rows.
    """
    if isinstance(renderable, Text):
        marker_prefixes = (f"{TRANSCRIPT_ASSISTANT_MARKER} ", "● ", "• ")
        if not renderable.plain.startswith(marker_prefixes):
            return renderable
        parts = renderable.divide([2])
        return parts[1] if len(parts) > 1 else Text("")
    if isinstance(renderable, Group) and renderable.renderables:
        first, *rest = renderable.renderables
        return Group(_strip_running_static_marker(first), *rest, fit=renderable.fit)
    if isinstance(renderable, Padding):
        return Padding(
            _strip_running_static_marker(renderable.renderable),
            (renderable.top, renderable.right, renderable.bottom, renderable.left),
            style=renderable.style,
            expand=renderable.expand,
        )
    return renderable


def invalid_arg() -> Text:
    """Placeholder rendered when an arg is not a valid string."""
    return fg("error", "<invalid>")


def missing_required_arg(name: str) -> Text:
    """Clear placeholder for a required arg absent from a finished invalid call."""
    return fg("error", f"<missing {name}>")


def shorten_path(path: str, *, cwd: str | None = None) -> str:
    """Display-shorten an absolute path in the standard way.

    * paths inside ``cwd`` are made relative;
    * paths inside ``$HOME`` are prefixed with ``~``;
    * other absolute paths stay absolute.
    """
    if not path:
        return path
    cwd = cwd or os.getcwd()
    try:
        p = Path(path)
        if p.is_absolute():
            try:
                rel = p.relative_to(cwd)
                s = str(rel)
                return s if s != "." else path
            except ValueError:
                home = Path.home()
                try:
                    rel = p.relative_to(home)
                    return f"~/{rel}" if str(rel) != "." else "~"
                except ValueError:
                    return path
        return path
    except (TypeError, ValueError):
        return path


def tab_to_spaces(text: str, *, tab_size: int = 4) -> str:
    """Replace tabs with spaces; preserves newlines."""
    if "\t" not in text:
        return text
    return text.expandtabs(tab_size)


def format_lines_block(
    text: str,
    *,
    expanded: bool,
    collapsed_max_lines: int,
    style_token: str = "tool_output",
) -> tuple[Text, int]:
    """Render *text* as a block of styled lines, capped at *collapsed_max_lines*.

    Returns a ``(rendered, remaining)`` tuple where *remaining* is the number
    of lines hidden by the collapsed view (``0`` when expanded or short).
    Always strips ANSI from input to keep layout safe.
    """
    cleaned = sanitize_ansi(text or "").rstrip("\n")
    if not cleaned:
        return Text(""), 0
    lines = cleaned.split("\n")
    max_lines = len(lines) if expanded else max(0, collapsed_max_lines)
    shown = lines[:max_lines] if max_lines else []
    remaining = max(0, len(lines) - len(shown))
    body = Text("\n".join(tab_to_spaces(line) for line in shown))
    body.stylize(tui_rich_style(style_token))
    return body, remaining


def format_numbered_lines_block(
    text: str,
    *,
    expanded: bool,
    collapsed_max_lines: int,
    start_line: int = 1,
    style_token: str = "tool_output",
) -> tuple[Text, int, int]:
    """Render source text with dim line numbers, capped like shell code previews.

    Returns ``(rendered, remaining, total_lines)``.  A trailing newline is a
    terminator, not an extra empty source line, matching editor line numbering.
    """
    cleaned = sanitize_ansi(text or "").rstrip("\n")
    if not cleaned:
        return Text(""), 0, 0
    lines = cleaned.split("\n")
    total_lines = len(lines)
    max_lines = total_lines if expanded else max(0, collapsed_max_lines)
    shown = lines[:max_lines] if max_lines else []
    remaining = max(0, total_lines - len(shown))
    number_width = max(
        LISTING_LINE_NUMBER_MIN_WIDTH, len(str(start_line + max(0, total_lines - 1)))
    )
    body = Text()
    number_style = tui_rich_style("muted")
    content_style = tui_rich_style(style_token)
    for index, line in enumerate(shown):
        if index:
            body.append("\n")
        body.append(f"{start_line + index:>{number_width}} ", style=number_style)
        body.append(tab_to_spaces(line), style=content_style)
    return body, remaining, total_lines
