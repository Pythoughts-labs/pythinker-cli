"""Renderer for Pythinker's ``SmartSearch`` tool."""

from __future__ import annotations

import re
from typing import cast

from rich.console import Group, RenderableType
from rich.text import Text

from pythinker_code.ui.shell.components.key_hints import key_hint
from pythinker_code.ui.shell.render_constants import expand_hint
from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolRenderDefinition,
    ToolResultPayload,
)
from pythinker_code.ui.shell.tool_renderers._render_utils import (
    as_str,
    fg,
    fg_subject,
    format_lines_block,
    invalid_arg,
    missing_required_arg,
    pending_tool_call_header,
    running_spinner,
    shorten_path,
    tool_call_header,
)
from pythinker_code.ui.theme import tui_rich_style

_TOOL_NAME = "SmartSearch"
_DEFAULT_EXPANDED_LINES = 15
_RG_CONTENT_PATH_RE = re.compile(r"^(.+?)(?::\d+:|-\d+-)")


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else plural or f"{singular}s"


def _extras(result: ToolResultPayload) -> dict[str, object]:
    raw = result.details.get("extras")
    return cast("dict[str, object]", raw) if isinstance(raw, dict) else {}


def _nonempty_result_lines(text: str) -> list[str]:
    return [
        line
        for line in (text or "").splitlines()
        if line.strip() and not line.lstrip().startswith("## ")
    ]


def _file_count(lines: list[str]) -> int:
    files: set[str] = set()
    for line in lines:
        match = _RG_CONTENT_PATH_RE.match(line)
        if match:
            files.add(match.group(1))
    return len(files)


def _count_from_extras(extras: dict[str, object]) -> tuple[int | None, str]:
    for key, label in (
        ("line_count", "line"),
        ("result_count", "line"),
        ("returned_results", "line"),
        ("match_count", "match"),
    ):
        value = extras.get(key)
        if isinstance(value, int) and value >= 0:
            return value, label
    return None, "line"


def _render_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    query = as_str(args.get("query"))
    raw_path = as_str(args.get("path"))
    glob = as_str(args.get("glob"))
    type_filter = as_str(args.get("type"))
    max_results = args.get("max_results")

    summary = Text()
    if query is None:
        if "query" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append_text(missing_required_arg("query"))
        else:
            line = pending_tool_call_header("SmartSearch", action="Searching")
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    else:
        summary.append_text(fg_subject(f'"{query}"'))

    if "path" in args:
        summary.append_text(fg("tool_output", " in "))
        if raw_path is None:
            summary.append_text(invalid_arg())
        else:
            summary.append_text(fg("tool_output", shorten_path(raw_path, cwd=ctx.cwd)))

    extras: list[str] = []
    if glob:
        extras.append(glob)
    if type_filter:
        extras.append(type_filter)
    if isinstance(max_results, int) and max_results != 60:
        extras.append(f"limit {max_results}")
    for extra in extras:
        summary.append_text(fg("muted", f" · {extra}"))

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("SmartSearch", summary, style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _summary_line(result: ToolResultPayload, result_lines: list[str]) -> Text:
    summary = Text()
    extras = _extras(result)
    count, label = _count_from_extras(extras)
    if count is None:
        count = len(result_lines)
    file_count_raw = extras.get("file_count")
    file_count = (
        file_count_raw
        if isinstance(file_count_raw, int) and file_count_raw >= 0
        else _file_count(result_lines)
    )

    summary.append("Found ", style=tui_rich_style("tool_output"))
    summary.append(str(count), style=tui_rich_style("tool_title"))
    summary.append(f" {_plural(count, label)}", style=tui_rich_style("tool_output"))
    if file_count:
        summary.append(" across ", style=tui_rich_style("muted"))
        summary.append(str(file_count), style=tui_rich_style("tool_title"))
        summary.append(f" {_plural(file_count, 'file')}", style=tui_rich_style("muted"))
    return summary


def _render_result(ctx: ToolRenderContext, result: ToolResultPayload) -> RenderableType | None:
    if not result.text:
        return None

    if result.is_error:
        summary = fg("error", "Error searching files")
        body, remaining = format_lines_block(
            result.text,
            expanded=ctx.expanded,
            collapsed_max_lines=_DEFAULT_EXPANDED_LINES,
            style_token="error",
        )
        children: list[RenderableType] = [summary]
        if body.plain:
            children.append(body)
        if remaining > 0:
            children.append(fg("muted", expand_hint(remaining)))
        return Group(*children)

    ctx.state["__suppress_generic_expand_hint__"] = True
    if result.text.startswith("No matches found"):
        return fg("tool_output", result.text.rstrip("\n"))

    result_lines = _nonempty_result_lines(result.text)
    summary = _summary_line(result, result_lines)
    if not result_lines:
        return summary
    if not ctx.expanded:
        ctx.state["__has_expandable_payload__"] = True
        row = summary.copy()
        row.append(" ")
        row.append_text(key_hint("ctrl+o", "expand"))
        return row

    body, remaining = format_lines_block(
        result.text,
        expanded=False,
        collapsed_max_lines=_DEFAULT_EXPANDED_LINES,
        style_token="tool_output",
    )
    children = [summary]
    if body.plain:
        children.append(body)
    if remaining > 0:
        children.append(fg("muted", expand_hint(remaining)))
    return Group(*children)


SMART_SEARCH_RENDERER = ToolRenderDefinition(
    name=_TOOL_NAME,
    label="smart search",
    render_shell="default",
    render_call=_render_call,
    render_result=_render_result,
)
