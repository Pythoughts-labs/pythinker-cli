"""Pythinker renderer for the ``LSP`` tool."""

from __future__ import annotations

from typing import cast

from rich.console import Group, RenderableType
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
    shorten_path,
    tool_call_header,
)
from pythinker_code.ui.theme import tui_rich_style

_TOOL_NAME = "LSP"

_POSITION_OPERATIONS = {
    "goToDefinition",
    "findReferences",
    "hover",
    "goToImplementation",
}
_LABELS: dict[str, tuple[str, str, str | None]] = {
    "goToDefinition": ("definition", "definitions", None),
    "findReferences": ("reference", "references", None),
    "documentSymbol": ("symbol", "symbols", None),
    "workspaceSymbol": ("symbol", "symbols", None),
    "hover": ("hover info", "hover info", "available"),
    "goToImplementation": ("implementation", "implementations", None),
    "prepareCallHierarchy": ("call item", "call items", None),
    "incomingCalls": ("caller", "callers", None),
    "outgoingCalls": ("callee", "callees", None),
}


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _count_detail(details: dict[str, object], *keys: str) -> int | None:
    extras_raw = details.get("extras")
    extras = cast("dict[str, object]", extras_raw) if isinstance(extras_raw, dict) else {}
    for source in (extras, details):
        for key in keys:
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def _operation_detail(details: dict[str, object], ctx: ToolRenderContext) -> str:
    extras_raw = details.get("extras")
    extras = cast("dict[str, object]", extras_raw) if isinstance(extras_raw, dict) else {}
    for source in (extras, details):
        value = source.get("operation")
        if isinstance(value, str) and value:
            return value
    # ctx.args may be None in the result-only render path; fall back to an
    # empty dict so the .get call never raises AttributeError.
    return as_str((ctx.args or {}).get("operation")) or "result"


def _render_call(ctx: ToolRenderContext) -> RenderableType | None:
    args = ctx.args or {}
    operation = as_str(args.get("operation"))
    summary = Text()

    if operation is None:
        if "operation" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append_text(missing_required_arg("operation"))
        else:
            line = pending_tool_call_header("LSP")
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    else:
        summary.append_text(fg("tool_output", f'operation: "{operation}"'))
        file_path = as_str(args.get("file_path"))
        if file_path is None:
            file_path = as_str(args.get("filePath"))
        line = _as_int(args.get("line"))
        character = _as_int(args.get("character"))
        if file_path:
            summary.append_text(fg("muted", ", "))
            display_path = shorten_path(file_path, cwd=ctx.cwd)
            summary.append_text(fg("tool_output", f'file: "{display_path}"'))
        if operation in _POSITION_OPERATIONS and line is not None and character is not None:
            summary.append_text(fg("muted", ", "))
            summary.append_text(fg("tool_output", f"position: {line}:{character}"))

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("LSP", summary, style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _render_result(ctx: ToolRenderContext, result: ToolResultPayload) -> RenderableType | None:
    details = result.details
    operation = _operation_detail(details, ctx)
    result_count = _count_detail(details, "result_count", "resultCount")
    file_count = _count_detail(details, "file_count", "fileCount")
    if result_count is None or file_count is None:
        if not result.text:
            return None
        style_token = "error" if result.is_error else "tool_output"
        return fg(style_token, result.text)

    singular, plural, special = _LABELS.get(operation, ("result", "results", None))
    if result_count == 0:
        if result.text:
            style_token = "error" if result.is_error else "tool_output"
            return fg(style_token, result.text)
        return Text(f"No {plural} found", style=tui_rich_style("tool_output"))

    count_label = singular if result_count == 1 else plural
    summary = Text(style=tui_rich_style("tool_output"))
    if operation == "hover" and result_count > 0 and special:
        summary.append(f"Hover info {special}")
    else:
        summary.append("Found ")
        summary.append(str(result_count), style=tui_rich_style("tool_title"))
        summary.append(f" {count_label}")
    if file_count > 1:
        summary.append(" across ")
        summary.append(str(file_count), style=tui_rich_style("tool_title"))
        summary.append(" files")

    if not ctx.expanded:
        if result_count > 0:
            ctx.state["__suppress_generic_expand_hint__"] = True
            if result.text:
                ctx.state["__has_expandable_payload__"] = True
        return summary
    if not result.text:
        return summary
    return Group(summary, fg("tool_output", result.text))


LSP_RENDERER = ToolRenderDefinition(
    name=_TOOL_NAME,
    label="LSP",
    render_shell="default",
    render_call=_render_call,
    render_result=_render_result,
)
