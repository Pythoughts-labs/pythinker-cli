"""Renderer for Pythinker's ``ReadMediaFile`` tool."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from rich.console import RenderableType
from rich.text import Text

from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolRenderDefinition,
    ToolResultPayload,
)
from pythinker_code.ui.shell.tool_renderers._render_utils import (
    as_str,
    fg,
    fg_subject,
    format_byte_size,
    format_lines_block,
    invalid_arg,
    missing_required_arg,
    pending_tool_call_header,
    running_spinner,
    shorten_path,
    tool_call_header,
)
from pythinker_code.ui.theme import tui_rich_style

_TOOL_NAME = "ReadMediaFile"
_LOADED_RE = re.compile(
    r"Loaded (?P<kind>image|video) file `[^`]+` "
    r"\((?P<mime>[^,\)]+), (?P<bytes>\d+) bytes"
    r"(?:, original size (?P<width>\d+)x(?P<height>\d+)px)?\)"
)
_DATA_URL_RE = re.compile(r"data:(?P<mime>image/[^;]+|video/[^;]+);base64,", re.IGNORECASE)


@dataclass(slots=True, frozen=True)
class _MediaSummary:
    kind: str
    mime_type: str | None = None
    byte_size: int | None = None
    width: int | None = None
    height: int | None = None


def _render_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    raw_path = as_str(args.get("path"))
    summary = Text()
    if raw_path is None:
        if "path" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append_text(missing_required_arg("path"))
        else:
            line = pending_tool_call_header("ReadMedia")
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    else:
        summary.append_text(fg_subject(shorten_path(raw_path, cwd=ctx.cwd)))

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("ReadMedia", summary, style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _summary_from_details(result: ToolResultPayload) -> _MediaSummary | None:
    extras_raw = result.details.get("extras")
    extras = cast("dict[str, object]", extras_raw) if isinstance(extras_raw, dict) else {}
    kind_raw = extras.get("kind")
    if not isinstance(kind_raw, str) or kind_raw not in {"image", "video"}:
        return None
    mime_type = extras.get("mime_type")
    byte_size = extras.get("byte_size")
    width = extras.get("width")
    height = extras.get("height")
    return _MediaSummary(
        kind=kind_raw,
        mime_type=mime_type if isinstance(mime_type, str) else None,
        byte_size=byte_size if isinstance(byte_size, int) and byte_size >= 0 else None,
        width=width if isinstance(width, int) and width > 0 else None,
        height=height if isinstance(height, int) and height > 0 else None,
    )


def _summary_from_message(text: str) -> _MediaSummary | None:
    match = _LOADED_RE.search(text)
    if not match:
        return None
    width = match.group("width")
    height = match.group("height")
    return _MediaSummary(
        kind=match.group("kind"),
        mime_type=match.group("mime"),
        byte_size=int(match.group("bytes")),
        width=int(width) if width else None,
        height=int(height) if height else None,
    )


def _summary_from_payload(text: str) -> _MediaSummary | None:
    if "<image" in text or text.startswith("data:image/"):
        kind = "image"
    elif "<video" in text or text.startswith("data:video/"):
        kind = "video"
    else:
        return None
    match = _DATA_URL_RE.search(text)
    return _MediaSummary(kind=kind, mime_type=match.group("mime") if match else None)


def _media_summary(result: ToolResultPayload) -> _MediaSummary | None:
    if summary := _summary_from_details(result):
        return summary
    message = result.details.get("message")
    if isinstance(message, str) and (summary := _summary_from_message(message)):
        return summary
    return _summary_from_message(result.text) or _summary_from_payload(result.text)


def _render_summary(summary: _MediaSummary) -> Text:
    out = Text()
    out.append("Read ", style=tui_rich_style("tool_output"))
    out.append(summary.kind, style=tui_rich_style("tool_title"))
    extras: list[str] = []
    if summary.mime_type:
        extras.append(summary.mime_type)
    if summary.byte_size is not None:
        extras.append(format_byte_size(summary.byte_size))
    if summary.width is not None and summary.height is not None:
        extras.append(f"{summary.width}x{summary.height}")
    if extras:
        out.append(f" ({', '.join(extras)})", style=tui_rich_style("muted"))
    return out


def _render_result(ctx: ToolRenderContext, result: ToolResultPayload) -> RenderableType | None:
    ctx.state["__suppress_generic_expand_hint__"] = True
    if result.is_error:
        message = result.details.get("message")
        text = message if isinstance(message, str) and message else result.text
        body, _remaining = format_lines_block(
            text,
            expanded=True,
            collapsed_max_lines=0,
            style_token="error",
        )
        return body if body.plain else fg("error", "Error reading media file")

    if summary := _media_summary(result):
        return _render_summary(summary)
    return fg("tool_output", "Read media file")


READ_MEDIA_RENDERER = ToolRenderDefinition(
    name=_TOOL_NAME,
    label="read media",
    render_shell="default",
    render_call=_render_call,
    render_result=_render_result,
)
