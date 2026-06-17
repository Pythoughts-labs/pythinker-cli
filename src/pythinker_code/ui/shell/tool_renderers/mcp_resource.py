"""Pythinker renderers for MCP resource tools."""

from __future__ import annotations

import json

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
    invalid_arg,
    missing_required_arg,
    pending_tool_call_header,
    running_spinner,
    tool_call_header,
)
from pythinker_code.ui.theme import tui_rich_style


def _pretty_json_or_text(text: str) -> str:
    if not text.strip():
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    return json.dumps(parsed, indent=2, ensure_ascii=False)


def _render_list_call(ctx: ToolRenderContext) -> RenderableType:
    server = as_str((ctx.args or {}).get("server"))
    summary = f'List MCP resources from server "{server}"' if server else "List all MCP resources"
    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("MCPResources", fg("tool_output", summary), style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _render_read_call(ctx: ToolRenderContext) -> RenderableType | None:
    args = ctx.args or {}
    server = as_str(args.get("server"))
    uri = as_str(args.get("uri"))
    summary = Text()
    if uri is None or server is None:
        if ("uri" in args and uri is None) or ("server" in args and server is None):
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            missing = "uri" if uri is None else "server"
            summary.append_text(missing_required_arg(missing))
        else:
            line = pending_tool_call_header("MCPResource")
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    else:
        summary.append_text(fg("tool_output", f'Read resource "{uri}" from server "{server}"'))

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("MCPResource", summary, style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _render_jsonish_result(
    ctx: ToolRenderContext, result: ToolResultPayload
) -> RenderableType | None:
    text = _pretty_json_or_text(result.text)
    if not text:
        return Text("(No content)", style=tui_rich_style("muted"))
    if text.count("\n") > 4 or len(text) > 240:
        ctx.state["__suppress_generic_expand_hint__"] = True
    if not ctx.expanded:
        lines = text.splitlines()
        shown = "\n".join(lines[:6])
        if len(lines) > 6:
            shown += f"\n... ({len(lines) - 6} more lines, ctrl+o to expand)"
        return Text(shown, style=tui_rich_style("tool_output"))
    return Text(text, style=tui_rich_style("tool_output"))


LIST_MCP_RESOURCES_RENDERER = ToolRenderDefinition(
    name="ListMcpResources",
    label="MCPResources",
    render_shell="default",
    render_call=_render_list_call,
    render_result=_render_jsonish_result,
)

READ_MCP_RESOURCE_RENDERER = ToolRenderDefinition(
    name="ReadMcpResource",
    label="MCPResource",
    render_shell="default",
    render_call=_render_read_call,
    render_result=_render_jsonish_result,
)
