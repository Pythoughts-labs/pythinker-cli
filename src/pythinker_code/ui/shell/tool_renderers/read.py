"""Pythinker renderer for Pythinker's ``ReadFile`` tool.

The call row shows a compact path/range summary. Results use typed summaries
(``Read N lines``, ``File not found``, etc.) rather than echoing the entire
file body into the terminal transcript.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from rich.console import Group, RenderableType
from rich.text import Text

from pythinker_code.ui.shell.components import sanitize_ansi
from pythinker_code.ui.shell.components.render_utils import truncate_to_width
from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolRenderDefinition,
    ToolResultPayload,
)
from pythinker_code.ui.shell.tool_renderers._render_utils import (
    as_str,
    fg,
    fg_subject,
    format_numbered_lines_block,
    invalid_arg,
    missing_required_arg,
    pending_tool_call_header,
    running_spinner,
    shorten_path,
    tab_to_spaces,
    tool_call_header,
)
from pythinker_code.ui.theme import tui_rich_style

_TOOL_NAME = "ReadFile"
# Compact collapsed preview: a few leading lines so humans/models can confirm
# content was returned without dumping the file. Expanded mode shows it all.
_PREVIEW_MAX_LINES = 4
_LINES_READ_RE = re.compile(r"(\d+)\s+lines?\s+read")


def _format_line_range(args: dict[str, Any]) -> Text | None:
    offset = args.get("line_offset")
    limit = args.get("n_lines")
    if offset in (None, 1) and limit is None:
        return None
    try:
        start = int(offset) if offset is not None else 1  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # A negative offset is tail mode (read the last N lines from EOF). A forward
    # ``start-end`` range is meaningless here, so show it as ``tail N`` instead
    # of the confusing ``:-100--81``.
    if start < 0:
        text = f":tail {abs(start)}"
        if isinstance(limit, int):
            text += f" · limit {limit}"
        return fg("warning", text)
    if limit is None:
        return fg("warning", f":{start}")
    try:
        end = start + int(limit) - 1  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fg("warning", f":{start}")
    return fg("warning", f":{start}-{end}")


def _render_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    raw_path = as_str(args.get("path"))
    summary = Text()
    if raw_path is None:
        # Either missing (still streaming) or wrong type.
        if "path" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append_text(missing_required_arg("path"))
        else:
            line = pending_tool_call_header("Read")
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    else:
        summary.append_text(fg_subject(shorten_path(raw_path, cwd=ctx.cwd)))

    range_text = _format_line_range(args)
    if range_text is not None:
        summary.append_text(range_text)
    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("Read", summary, style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _friendly_error(text: str) -> str:
    lowered = text.lower()
    if "does not exist" in lowered or "file not found" in lowered:
        return "File not found"
    if "not a file" in lowered or "invalid path" in lowered:
        return "Invalid path"
    if "sensitive" in lowered:
        return "Sensitive file"
    if text.strip():
        return text.rstrip("\n")
    return "Error reading file"


def _basename(path: Any) -> str | None:
    """Display basename for a read path, or ``None`` when unavailable.

    The call row already shows the (shortened) path, so the result summary only
    needs the leaf name — and never a fuller path that would leak more than the
    call row already does. Handle both POSIX and Windows separators since the
    path is model-supplied text, not a resolved local path.
    """
    raw = as_str(path)
    if raw is None or not raw.strip():
        return None
    name = PureWindowsPath(PurePosixPath(raw).name).name
    return name or None


def _line_count(message: str | None, output_text: str) -> int | None:
    """Lines read in this call: prefer the tool message, else count the body.

    Returns ``None`` only when the count is genuinely unknowable (no message and
    no body) — callers must then avoid asserting a count rather than lie with 0.
    """
    if message:
        match = _LINES_READ_RE.search(message)
        if match:
            return int(match.group(1))
        if "no lines read" in message.lower():
            return 0
    if output_text:
        cleaned = output_text.rstrip("\n")
        return cleaned.count("\n") + 1 if cleaned else 0
    return None


def _summary_text(count: int | None, basename: str | None) -> str:
    if count is None:
        head = "Read file content"
    else:
        head = f"Read {count} {'line' if count == 1 else 'lines'}"
    if basename:
        head += f" from {basename}"
    return head


def _preview(output_text: str, width: int) -> Text | None:
    """A few leading body lines, ANSI-stripped and width-capped per line.

    Caps both the number of visual lines (``_PREVIEW_MAX_LINES``) and each
    line's width so a file with very long lines can never produce giant
    collapsed output. Returns ``None`` when there is nothing friendly to show.
    """
    cleaned = sanitize_ansi(output_text or "").rstrip("\n")
    if not cleaned:
        return None
    # Width ceiling keyed off terminal cell width, leaving room for the card
    # gutter so each preview line stays on a single visual row. On a terminal
    # too narrow to show anything useful, skip the preview entirely.
    limit = min(max(width - 6, 0), 200)
    if limit < 12:
        return None
    out = Text(style=tui_rich_style("tool_output"))
    for index, line in enumerate(cleaned.split("\n")[:_PREVIEW_MAX_LINES]):
        if index:
            out.append("\n")
        # Cell-width aware: a wide-glyph / CJK / emoji line is truncated by the
        # space it actually occupies, not its character count.
        out.append(truncate_to_width(tab_to_spaces(line), limit))
    out.no_wrap = True
    out.overflow = "ellipsis"
    return out if out.plain else None


def _render_result(ctx: ToolRenderContext, result: ToolResultPayload) -> RenderableType | None:
    ctx.state["__suppress_generic_expand_hint__"] = True
    if result.is_error:
        message = result.details.get("message")
        error_text = message if isinstance(message, str) and message else result.text
        return fg("error", _friendly_error(error_text))

    message = result.details.get("message")
    if isinstance(message, str) and message.startswith("Directory listing for `"):
        return fg("tool_output", "Listed 1 directory")

    # An empty string is a valid (empty-file) body — only fall back to the
    # flattened text when ``output`` is absent/non-string, so an empty read
    # never inherits unrelated metadata from ``result.text``.
    output = result.details.get("output")
    output_text = output if isinstance(output, str) else result.text

    count = _line_count(message if isinstance(message, str) else None, output_text)
    basename = _basename(ctx.args.get("path"))
    summary = _summary_text(count, basename)

    if not output_text:
        # Nothing to preview or expand (empty file / no body): truthful summary only.
        return fg("tool_output", summary)

    if not ctx.expanded:
        collapsed = fg("tool_output", f"{summary} (ctrl+o to expand)")
        preview = _preview(output_text, ctx.width)
        return Group(collapsed, preview) if preview is not None else collapsed

    start_line = 1
    offset = ctx.args.get("line_offset")
    if isinstance(offset, int) and offset > 0:
        start_line = offset
    body, _remaining, _total = format_numbered_lines_block(
        output_text,
        expanded=True,
        collapsed_max_lines=0,
        start_line=start_line,
        style_token="tool_output",
    )
    return Group(fg("tool_output", summary), body) if body.plain else fg("tool_output", summary)


READ_RENDERER = ToolRenderDefinition(
    name=_TOOL_NAME,
    label="read",
    render_shell="default",
    render_call=_render_call,
    render_result=_render_result,
)
