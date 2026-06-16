"""Pythinker renderer for the ``ToolSearch`` tool.

The model-facing tool returns name + description lines for LLM consumption.
The TUI shows a compact discovery summary by default and tool names only on
expand — never the full description catalog.
"""

from __future__ import annotations

import re

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
    invalid_arg,
    pending_tool_call_header,
    running_spinner,
    tool_call_header,
)
from pythinker_code.ui.theme import tui_rich_style

_TOOL_NAME = "ToolSearch"
_DISPLAY_NAME = "Tools"
_COLLAPSED_NAME_PREVIEW = 5
_LIST_LINE_RE = re.compile(r"^- (.+?) - .+$")


def _parse_tool_names(text: str) -> list[str]:
    names: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        if match := _LIST_LINE_RE.match(line):
            names.append(match.group(1))
            continue
        rest = line[2:].strip()
        if " - " in rest:
            names.append(rest.split(" - ", 1)[0].strip())
    return names


def _render_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    query = as_str(args.get("query"))
    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"

    if query is None:
        if "query" in args:
            summary: str | Text = invalid_arg()
        elif ctx.has_result:
            summary = fg("muted", "search")
        else:
            header = pending_tool_call_header(_DISPLAY_NAME, action="Searching")
            return running_spinner(
                header,
                execution_started=ctx.execution_started,
                has_result=ctx.has_result,
            )
    else:
        summary = fg_subject(query) if query else fg("muted", "search")

    header = tool_call_header(_DISPLAY_NAME, summary, style_token=style_token)
    return running_spinner(
        header, execution_started=ctx.execution_started, has_result=ctx.has_result
    )


def _render_result(ctx: ToolRenderContext, result: ToolResultPayload) -> RenderableType | None:
    text = (result.text or "").strip()
    if not text:
        message = result.details.get("message") if result.details else None
        if isinstance(message, str) and message.strip():
            return fg("error" if result.is_error else "muted", message.strip())
        return None

    if text.startswith(("No visible tools", "No visible tools matched")):
        ctx.state["__suppress_generic_expand_hint__"] = True
        return fg("error" if result.is_error else "muted", text)

    names = _parse_tool_names(text)
    if not names:
        ctx.state["__suppress_generic_expand_hint__"] = True
        return fg("error" if result.is_error else "tool_output", text.splitlines()[0])

    count = len(names)
    if count > _COLLAPSED_NAME_PREVIEW:
        ctx.state["__has_expandable_payload__"] = True

    if ctx.expanded:
        ctx.state["__suppress_generic_expand_hint__"] = True
        return fg("tool_output", f"Tools discovered: {', '.join(names)}")

    preview = names[:_COLLAPSED_NAME_PREVIEW]
    suffix = ""
    if count > len(preview):
        suffix = f", +{count - len(preview)} more"
    names_part = ", ".join(preview) + suffix

    line = Text()
    line.append("✓ ", style=tui_rich_style("success"))
    label = "tool" if count == 1 else "tools"
    line.append(f"{count} {label} discovered", style=tui_rich_style("tool_output"))
    if names_part:
        line.append(f" ({names_part})", style=tui_rich_style("muted"))
    ctx.state["__suppress_generic_expand_hint__"] = count <= _COLLAPSED_NAME_PREVIEW
    return line


TOOL_SEARCH_RENDERER = ToolRenderDefinition(
    name=_TOOL_NAME,
    label="tools",
    render_shell="default",
    render_call=_render_call,
    render_result=_render_result,
)
