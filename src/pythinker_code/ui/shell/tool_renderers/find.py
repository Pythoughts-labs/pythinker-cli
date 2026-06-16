"""Pythinker renderer for Pythinker's ``Glob`` tool.

Source tool name → Pythinker tool name: ``find`` → ``Glob``.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.text import Text

from pythinker_code.ui.shell.components.key_hints import key_hint
from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolRenderDefinition,
    ToolResultPayload,
)
from pythinker_code.ui.shell.tool_renderers._render_utils import (
    as_str,
    fg,
    format_lines_block,
    invalid_arg,
    missing_required_arg,
    pending_tool_call_header,
    running_spinner,
    shorten_path,
    tool_call_header,
)
from pythinker_code.ui.theme import tui_rich_style

_TOOL_NAME = "Glob"
_DEFAULT_COLLAPSED_LINES = 20


def _nonempty_lines(text: str) -> list[str]:
    return [line for line in (text or "").splitlines() if line.strip()]


def _plural(count: int, singular: str) -> str:
    return singular if count == 1 else f"{singular}s"


def _render_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    pattern = as_str(args.get("pattern"))
    raw_dir = as_str(args.get("directory"))

    summary = Text()
    if pattern is None:
        if "pattern" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append_text(missing_required_arg("pattern"))
        else:
            line = pending_tool_call_header("Find")
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    else:
        summary.append_text(fg("info", pattern))

    summary.append_text(fg("tool_output", " in "))
    if "directory" in args and raw_dir is None:
        summary.append_text(invalid_arg())
    else:
        summary.append_text(fg("tool_output", shorten_path(raw_dir or ".", cwd=ctx.cwd)))

    if args.get("include_dirs") is False:
        summary.append_text(fg("muted", " · files only"))

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("Find", summary, style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _render_result(ctx: ToolRenderContext, result: ToolResultPayload) -> RenderableType | None:
    if not result.text:
        return None
    if result.is_error:
        body, remaining = format_lines_block(
            result.text,
            expanded=ctx.expanded,
            collapsed_max_lines=_DEFAULT_COLLAPSED_LINES,
            style_token="error",
        )
        if not body.plain:
            return None
        if remaining > 0:
            return Group(body, fg("muted", f"... ({remaining} more lines, ctrl+o to expand)"))
        return body

    lines = _nonempty_lines(result.text)
    summary = Text()
    summary.append("Found ", style=tui_rich_style("tool_output"))
    summary.append(str(len(lines)), style=tui_rich_style("tool_title"))
    summary.append(f" {_plural(len(lines), 'file')}", style=tui_rich_style("tool_output"))
    if not lines:
        return summary

    ctx.state["__suppress_generic_expand_hint__"] = True
    if not ctx.expanded:
        summary.append(" ")
        summary.append_text(key_hint("ctrl+o", "expand"))
        return summary

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
        children.append(fg("muted", f"... ({remaining} more lines, ctrl+o to expand)"))
    return Group(*children)


FIND_RENDERER = ToolRenderDefinition(
    name=_TOOL_NAME,
    label="find",
    render_shell="default",
    render_call=_render_call,
    render_result=_render_result,
)
