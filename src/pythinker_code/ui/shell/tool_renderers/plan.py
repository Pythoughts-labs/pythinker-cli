"""Pythinker renderers for Pythinker's plan-mode tools."""

from __future__ import annotations

from typing import Any, cast

from rich.console import Group, RenderableType

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
    running_spinner,
    tool_call_header,
)

# ---------------------------------------------------------------------------
# EnterPlanMode
# ---------------------------------------------------------------------------


def _render_enter_call(ctx: ToolRenderContext) -> RenderableType:
    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("Plan", fg("muted", "entering"), style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _render_plan_result(ctx: ToolRenderContext, result: ToolResultPayload) -> RenderableType | None:
    if not result.text:
        return None
    body, remaining = format_lines_block(
        result.text,
        expanded=ctx.expanded,
        collapsed_max_lines=8,
        style_token="error" if result.is_error else "tool_output",
    )
    if not body.plain:
        return None
    if remaining > 0:
        return Group(body, fg("muted", f"... ({remaining} more lines, ctrl+o to expand)"))
    return body


ENTER_PLAN_RENDERER = ToolRenderDefinition(
    name="EnterPlanMode",
    label="plan mode",
    render_shell="default",
    render_call=_render_enter_call,
    render_result=_render_plan_result,
)


# ---------------------------------------------------------------------------
# ExitPlanMode
# ---------------------------------------------------------------------------


def _render_exit_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    options = args.get("options")
    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("Plan", fg("muted", "exiting"), style_token=style_token)

    if not isinstance(options, list) or not options:
        return running_spinner(
            line, execution_started=ctx.execution_started, has_result=ctx.has_result
        )
    options_list = cast("list[Any]", options)
    opts: list[dict[str, Any]] = [
        cast("dict[str, Any]", o) for o in options_list if isinstance(o, dict)
    ]
    if not opts:
        return running_spinner(
            line, execution_started=ctx.execution_started, has_result=ctx.has_result
        )

    children: list[RenderableType] = [line]
    for opt in opts[:3]:
        label = as_str(opt.get("label")) or "?"
        children.append(fg_subject(f"  • {label}"))
    rendered = Group(*children)
    return running_spinner(
        rendered, execution_started=ctx.execution_started, has_result=ctx.has_result
    )


EXIT_PLAN_RENDERER = ToolRenderDefinition(
    name="ExitPlanMode",
    label="plan mode",
    render_shell="default",
    render_call=_render_exit_call,
    render_result=_render_plan_result,
)
