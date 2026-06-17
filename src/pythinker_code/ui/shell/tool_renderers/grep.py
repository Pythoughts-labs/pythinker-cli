"""Pythinker renderer for Pythinker's ``Grep`` tool.

Blackbox-style search cards keep the call row compact and summarize results
first. Expanded cards show the raw matches under the same response gutter.
"""

from __future__ import annotations

import re

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

_TOOL_NAME = "Grep"
_DEFAULT_COLLAPSED_LINES = 15
# Ripgrep content mode emits `path:line:match` and context rows as
# `path-line-context`. Match the separator that is immediately followed by a
# line number so paths containing `:` or `-` are not split accidentally.
_RG_CONTENT_PATH_RE = re.compile(r"^(.+?)(?::\d+:|-\d+-)")


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    return singular if count == 1 else plural or f"{singular}s"


def _nonempty_lines(text: str) -> list[str]:
    return [line for line in (text or "").splitlines() if line.strip()]


def _file_count_for_content(lines: list[str]) -> int:
    files: set[str] = set()
    for line in lines:
        if line == "--":
            continue
        match = _RG_CONTENT_PATH_RE.match(line)
        if match:
            files.add(match.group(1))
    return len(files)


def _render_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    pattern = as_str(args.get("pattern"))
    raw_path = as_str(args.get("path"))
    glob = as_str(args.get("glob"))
    head_limit = args.get("head_limit")
    output_mode = as_str(args.get("output_mode"))

    summary = Text()
    if pattern is None:
        if "pattern" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append_text(missing_required_arg("pattern"))
        else:
            line = pending_tool_call_header("Search")
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    else:
        summary.append_text(fg_subject(f"/{pattern}/"))

    path_display = shorten_path(raw_path or ".", cwd=ctx.cwd) if raw_path is not None else None
    summary.append_text(fg("tool_output", " in "))
    if "path" in args and raw_path is None:
        summary.append_text(invalid_arg())
    else:
        summary.append_text(fg("tool_output", path_display or "."))

    extras: list[str] = []
    if glob:
        extras.append(glob)
    if output_mode and output_mode != "files_with_matches":
        extras.append(output_mode)
    if isinstance(head_limit, int) and head_limit not in (0, 250):
        extras.append(f"limit {head_limit}")
    for extra in extras:
        summary.append_text(fg("muted", f" · {extra}"))

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("Search", summary, style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _summary_line(ctx: ToolRenderContext, result: ToolResultPayload, lines: list[str]) -> Text:
    mode = as_str((ctx.args or {}).get("output_mode")) or "files_with_matches"
    out = Text()
    if result.is_error:
        out.append("Error searching files", style=tui_rich_style("error"))
        return out

    if mode == "count_matches":
        total_matches = 0
        file_count = 0
        for line in lines:
            _, sep, count_text = line.rpartition(":")
            if sep and count_text.isdigit():
                total_matches += int(count_text)
                file_count += 1
        out.append("Found ", style=tui_rich_style("tool_output"))
        out.append(str(total_matches), style=tui_rich_style("tool_title"))
        out.append(f" {_plural(total_matches, 'match')}", style=tui_rich_style("tool_output"))
        out.append(" across ", style=tui_rich_style("muted"))
        out.append(str(file_count), style=tui_rich_style("tool_title"))
        out.append(f" {_plural(file_count, 'file')}", style=tui_rich_style("muted"))
        return out

    count = len(lines)
    label = "line" if mode == "content" else "file"
    out.append("Found ", style=tui_rich_style("tool_output"))
    out.append(str(count), style=tui_rich_style("tool_title"))
    out.append(f" {_plural(count, label)}", style=tui_rich_style("tool_output"))
    if mode == "content":
        file_count = _file_count_for_content(lines)
        if file_count:
            out.append(" across ", style=tui_rich_style("muted"))
            out.append(str(file_count), style=tui_rich_style("tool_title"))
            out.append(f" {_plural(file_count, 'file')}", style=tui_rich_style("muted"))
    return out


def _render_result(ctx: ToolRenderContext, result: ToolResultPayload) -> RenderableType | None:
    if not result.text:
        return None
    lines = _nonempty_lines(result.text)
    summary = _summary_line(ctx, result, lines)
    if result.is_error:
        body, remaining = format_lines_block(
            result.text,
            expanded=ctx.expanded,
            collapsed_max_lines=_DEFAULT_COLLAPSED_LINES,
            style_token="error",
        )
        return Group(summary, body) if body.plain else summary

    if not lines:
        return summary

    ctx.state["__suppress_generic_expand_hint__"] = True
    if not ctx.expanded:
        hint = key_hint("ctrl+o", "expand")
        row = summary.copy()
        row.append(" ")
        row.append_text(hint)
        return row

    body, remaining = format_lines_block(
        result.text,
        expanded=True,
        collapsed_max_lines=_DEFAULT_COLLAPSED_LINES,
        style_token="tool_output",
    )
    children: list[RenderableType] = [summary]
    if body.plain:
        children.append(body)
    if remaining > 0:
        children.append(fg("muted", expand_hint(remaining)))
    return Group(*children)


GREP_RENDERER = ToolRenderDefinition(
    name=_TOOL_NAME,
    label="search",
    render_shell="default",
    render_call=_render_call,
    render_result=_render_result,
)
