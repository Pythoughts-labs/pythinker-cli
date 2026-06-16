"""Pythinker renderers for Pythinker's background-task tools.

Covers ``TaskList``, ``TaskOutput``, and ``TaskStop``.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Group, RenderableType
from rich.text import Text

from pythinker_code.tools.display import BackgroundTaskDisplayBlock
from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolRenderDefinition,
    ToolResultPayload,
)
from pythinker_code.ui.shell.tool_renderers._file_diff import display_blocks_from_result
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

# Process-wide resolver: task_id -> human description. Registered by the shell
# from the runtime's background-task store so a TaskOutput/TaskStop header can
# show the friendly name even while the task is still running (before any
# result arrives). Returns None when unknown or unset.
_task_label_resolver: Callable[[str], str | None] | None = None


def set_task_label_resolver(resolver: Callable[[str], str | None] | None) -> None:
    """Register (or clear) the call-time task-id → description resolver."""
    global _task_label_resolver
    _task_label_resolver = resolver


def _resolve_task_label(ctx: ToolRenderContext, task_id: str) -> str | None:
    """Resolve and cache a task's human description for *task_id*.

    Caches in ``ctx.state`` so the store is read at most once per card (the
    description is immutable), avoiding a filesystem read on every redraw.
    """
    cached = ctx.state.get("task_label")
    if cached:
        return cached if cached != task_id else None
    if _task_label_resolver is not None:
        label = _task_label_resolver(task_id)
        if label and label != task_id:
            ctx.state["task_label"] = label
            return label
    return None


def _stash_task_label(ctx: ToolRenderContext, result: ToolResultPayload) -> None:
    """Remember a task's human description so the header can show it instead
    of the opaque ``agent-xxxx`` / ``bash-xxxx`` id."""
    if ctx.state.get("task_label"):
        return
    for block in display_blocks_from_result(result):
        if isinstance(block, BackgroundTaskDisplayBlock) and block.description:
            ctx.state["task_label"] = block.description
            break


def _render_call_with_id(
    label: str, ctx: ToolRenderContext, *, extras: list[str]
) -> RenderableType:
    args = ctx.args or {}
    task_id = as_str(args.get("task_id"))
    summary = Text()
    if task_id is None:
        if "task_id" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append_text(missing_required_arg("task_id"))
        else:
            line = pending_tool_call_header(label)
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    else:
        # Prefer the task's human description (resolved from the store at
        # call-time, or stashed from the result) over the opaque id; keep the
        # id as a dim suffix for traceability.
        task_label = _resolve_task_label(ctx, task_id)
        if task_label:
            summary.append_text(fg("info", task_label))
            summary.append_text(fg("muted", f" · {task_id}"))
        else:
            summary.append_text(fg("info", task_id))
    for extra in extras:
        summary.append_text(fg("muted", f" · {extra}"))
    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header(label, summary, style_token=style_token)
    return running_spinner(
        line,
        execution_started=ctx.execution_started,
        has_result=ctx.has_result,
    )


def _render_block_result(
    ctx: ToolRenderContext,
    result: ToolResultPayload,
    *,
    collapsed_lines: int = 12,
) -> RenderableType | None:
    _stash_task_label(ctx, result)
    if not result.text:
        return None
    body, remaining = format_lines_block(
        result.text,
        expanded=ctx.expanded,
        collapsed_max_lines=collapsed_lines,
        style_token="error" if result.is_error else "tool_output",
    )
    if not body.plain:
        return None
    if remaining > 0:
        return Group(body, fg("muted", f"... ({remaining} more lines, ctrl+o to expand)"))
    return body


# ---------------------------------------------------------------------------
# TaskList
# ---------------------------------------------------------------------------


def _render_task_list_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    active_only = bool(args.get("active_only", True))
    limit = args.get("limit")
    summary = Text("active" if active_only else "all")
    if isinstance(limit, int) and limit != 20:
        summary.append_text(fg("muted", f" · limit {limit}"))
    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("Tasks", summary, style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


TASK_LIST_RENDERER = ToolRenderDefinition(
    name="TaskList",
    label="tasks",
    render_shell="default",
    render_call=_render_task_list_call,
    render_result=_render_block_result,
)


# ---------------------------------------------------------------------------
# TaskOutput
# ---------------------------------------------------------------------------


def _render_task_output_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    extras: list[str] = []
    if args.get("block"):
        timeout = args.get("timeout")
        extras.append(
            f"block, timeout {timeout}s" if isinstance(timeout, int) and timeout != 30 else "block"
        )
    return _render_call_with_id("TaskOutput", ctx, extras=extras)


TASK_OUTPUT_RENDERER = ToolRenderDefinition(
    name="TaskOutput",
    label="task output",
    render_shell="default",
    render_call=_render_task_output_call,
    render_result=lambda ctx, r: _render_block_result(ctx, r, collapsed_lines=20),
)


# ---------------------------------------------------------------------------
# TaskStop
# ---------------------------------------------------------------------------


def _render_task_stop_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    extras: list[str] = []
    reason = as_str(args.get("reason"))
    if reason and reason != "Stopped by TaskStop":
        extras.append(reason)
    return _render_call_with_id("TaskStop", ctx, extras=extras)


TASK_STOP_RENDERER = ToolRenderDefinition(
    name="TaskStop",
    label="task stop",
    render_shell="default",
    render_call=_render_task_stop_call,
    render_result=_render_block_result,
)
