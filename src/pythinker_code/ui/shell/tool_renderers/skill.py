"""Pythinker renderer for the ``ReadSkill`` tool.

The model-facing tool keeps the explicit ``ReadSkill`` name, but the terminal
row should read like the user action: ``Skill <name>``.
"""

from __future__ import annotations

from rich.console import Group, RenderableType

from pythinker_code.ui.shell.components.render_utils import sanitize_ansi
from pythinker_code.ui.shell.render_constants import expand_hint
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
    tool_call_header,
)

_TOOL_NAME = "ReadSkill"
_COLLAPSED_LINES = 15


def _render_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    skill_name = as_str(args.get("skill_name"))
    if skill_name is None:
        if "skill_name" in args:
            summary = invalid_arg()
        elif ctx.has_result:
            summary = missing_required_arg("skill_name")
        else:
            header = pending_tool_call_header("Skill")
            return running_spinner(
                header,
                execution_started=ctx.execution_started,
                has_result=ctx.has_result,
            )
    else:
        summary = fg("info", skill_name)

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    header = tool_call_header("Skill", summary, style_token=style_token)
    return running_spinner(
        header, execution_started=ctx.execution_started, has_result=ctx.has_result
    )


def _render_result(ctx: ToolRenderContext, result: ToolResultPayload) -> RenderableType | None:
    text = sanitize_ansi(result.text or "").rstrip("\n")
    if not text:
        return fg(
            "error" if result.is_error else "tool_output",
            "Skill read failed" if result.is_error else "Skill loaded",
        )

    body, remaining = format_lines_block(
        text,
        expanded=ctx.expanded,
        collapsed_max_lines=_COLLAPSED_LINES,
        style_token="error" if result.is_error else "tool_output",
    )
    if remaining > 0:
        ctx.state["__suppress_generic_expand_hint__"] = True
        return Group(body, fg("muted", expand_hint(remaining)))
    return body if body.plain else None


SKILL_RENDERER = ToolRenderDefinition(
    name=_TOOL_NAME,
    label="skill",
    render_shell="default",
    render_call=_render_call,
    render_result=_render_result,
)
