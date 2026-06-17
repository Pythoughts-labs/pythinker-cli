"""Pythinker renderers for session worktree tools."""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.text import Text

from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolRenderDefinition,
    ToolResultPayload,
)
from pythinker_code.ui.shell.tool_renderers._render_utils import (
    fg,
    running_spinner,
    tool_call_header,
)
from pythinker_code.ui.theme import tui_rich_style


def _metadata(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            meta[key.strip()] = value.strip()
    return meta


def _render_call(label: str, summary: str, ctx: ToolRenderContext) -> RenderableType:
    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header(label, fg("tool_output", summary), style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _render_enter_call(ctx: ToolRenderContext) -> RenderableType:
    return _render_call("Worktree", "Creating worktree…", ctx)


def _render_exit_call(ctx: ToolRenderContext) -> RenderableType:
    return _render_call("Worktree", "Exiting worktree…", ctx)


def _render_enter_result(
    _ctx: ToolRenderContext, result: ToolResultPayload
) -> RenderableType | None:
    if not result.text:
        return None
    # Errors must not be reported as a successful switch — surface the raw
    # error text and skip the "Switched to worktree" header. C01.
    if result.is_error:
        return fg("error", result.text.rstrip("\n"))
    meta = _metadata(result.text)
    path = meta.get("worktree_path", "")
    header = Text("Switched to worktree", style=tui_rich_style("tool_output"))
    if not path:
        return header
    return Group(header, Text(path, style=tui_rich_style("muted")))


def _render_exit_result(
    _ctx: ToolRenderContext, result: ToolResultPayload
) -> RenderableType | None:
    if not result.text:
        return None
    # Errors must not be reported as a successful keep/remove — surface the
    # raw error text and skip the keep/remove header. C01.
    if result.is_error:
        return fg("error", result.text.rstrip("\n"))
    meta = _metadata(result.text)
    retained = meta.get("retained", "").lower() == "true"
    label = "Kept worktree" if retained else "Removed worktree"
    header = Text(label, style=tui_rich_style("tool_output"))
    original = meta.get("restored_work_dir") or meta.get("original_work_dir")
    if not original:
        return header
    return Group(header, Text(f"Returned to {original}", style=tui_rich_style("muted")))


ENTER_WORKTREE_RENDERER = ToolRenderDefinition(
    name="EnterWorktree",
    label="Worktree",
    render_shell="default",
    render_call=_render_enter_call,
    render_result=_render_enter_result,
)

EXIT_WORKTREE_RENDERER = ToolRenderDefinition(
    name="ExitWorktree",
    label="Worktree",
    render_shell="default",
    render_call=_render_exit_call,
    render_result=_render_exit_result,
)
