"""Pythinker renderers for Pythinker's background-task tools.

Covers ``TaskList``, ``TaskOutput``, ``TaskInput``, ``TaskHandoff``, and ``TaskStop``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import cast

from rich.console import Group, RenderableType
from rich.text import Text

from pythinker_code.tools.display import BackgroundTaskDisplayBlock
from pythinker_code.ui.shell.components.key_hints import key_display_text
from pythinker_code.ui.shell.keymap import key_text
from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolRenderDefinition,
    ToolResultPayload,
)
from pythinker_code.ui.shell.tool_renderers._file_diff import display_blocks_from_result
from pythinker_code.ui.shell.tool_renderers._render_utils import (
    as_str,
    fg,
    fg_subject,
    format_lines_block,
    invalid_arg,
    missing_required_arg,
    normalize_agent_status,
    pending_tool_call_header,
    running_spinner,
    shorten_path,
    tool_call_header,
)
from pythinker_code.ui.theme import tui_rich_style

_SECRET_LIKE_INPUT_RE = re.compile(
    r"(?i)(api[_-]?key|auth|bearer|credential|passwd|password|secret|token)"
)
_TASK_INPUT_PREVIEW_LIMIT = 120

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
            summary.append_text(fg_subject(task_label))
            summary.append_text(fg("muted", f" · {task_id}"))
        else:
            summary.append_text(fg_subject(task_id))
    for extra in extras:
        summary.append_text(fg("muted", f" · {extra}"))
    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header(label, summary, style_token=style_token)
    return running_spinner(
        line,
        execution_started=ctx.execution_started,
        has_result=ctx.has_result,
    )


def _parse_task_output(text: str) -> tuple[dict[str, str], str, bool]:
    """Split TaskOutput tool text into metadata and the ``[output]`` body."""
    meta: dict[str, str] = {}
    body_lines: list[str] = []
    in_output = False
    saw_output_marker = False
    for raw_line in text.splitlines():
        if raw_line.strip() == "[output]":
            in_output = True
            saw_output_marker = True
            continue
        if in_output:
            body_lines.append(raw_line)
            continue
        if ":" not in raw_line:
            continue
        key, _, value = raw_line.partition(":")
        key = key.strip()
        if key and " " not in key:
            meta[key] = value.strip()
    body = "\n".join(body_lines).strip()
    if body.startswith("[Truncated. Full output:"):
        _, _, rest = body.partition("]\n\n")
        if rest:
            body = rest.strip()
    return meta, body, saw_output_marker


def _parse_task_metadata(text: str) -> dict[str, str]:
    """Parse simple ``key: value`` metadata emitted by background tools."""
    meta: dict[str, str] = {}
    for raw_line in text.splitlines():
        if ":" not in raw_line:
            continue
        key, _, value = raw_line.partition(":")
        key = key.strip()
        if key and " " not in key:
            meta[key] = value.strip()
    return meta


def _result_extras(result: ToolResultPayload) -> dict[str, object]:
    extras = result.details.get("extras")
    return cast("dict[str, object]", extras) if isinstance(extras, dict) else {}


def _tool_status_from_result(result: ToolResultPayload, meta: dict[str, str]) -> str:
    status = _result_extras(result).get("status")
    if isinstance(status, str) and status:
        return status
    return meta.get("tool_status") or meta.get("status", "")


def _status_display(status: str) -> str:
    return status.replace("_", " ").strip()


def _safe_task_input_preview(text: str) -> str:
    if _SECRET_LIKE_INPUT_RE.search(text):
        return "[redacted: input looks secret-like]"
    single_line = " ".join(text.splitlines())
    if len(single_line) > _TASK_INPUT_PREVIEW_LIMIT:
        return single_line[: _TASK_INPUT_PREVIEW_LIMIT - 3] + "..."
    return single_line


def _render_expanded_metadata(
    summary: Text,
    text: str,
    *,
    collapsed_lines: int = 12,
) -> RenderableType:
    body, remaining = format_lines_block(
        text,
        expanded=True,
        collapsed_max_lines=collapsed_lines,
        style_token="tool_output",
    )
    children: list[RenderableType] = [summary]
    if body.plain:
        children.append(body)
    if remaining > 0:
        children.append(fg("muted", f"… ({remaining} more lines)"))
    return Group(*children)


def _read_output_collapsed_hint() -> Text:
    expand_key = key_display_text(key_text("app.tools.expand") or "ctrl+o")
    return fg("dim", f"Read output ({expand_key} to expand)")


def _task_output_is_running(meta: dict[str, str]) -> bool:
    retrieval = meta.get("retrieval_status", "").lower()
    if retrieval in {"not_ready", "timeout"}:
        return True
    status = normalize_agent_status(meta.get("status", ""))
    return status in {"starting", "running", "waiting", "queued"}


def _render_task_output_result(
    ctx: ToolRenderContext,
    result: ToolResultPayload,
    *,
    collapsed_lines: int = 12,
) -> RenderableType | None:
    _stash_task_label(ctx, result)
    if not result.text:
        return None
    if result.is_error:
        return _render_block_result(ctx, result, collapsed_lines=collapsed_lines)

    meta, body, saw_output_marker = _parse_task_output(result.text)
    if not meta or not saw_output_marker:
        return _render_block_result(ctx, result, collapsed_lines=collapsed_lines)

    description = (
        meta.get("description")
        or ctx.state.get("task_label")
        or _resolve_task_label(ctx, meta.get("task_id", ""))
        or meta.get("task_id", "task")
    )
    retrieval = meta.get("retrieval_status", "").lower()
    if not retrieval and normalize_agent_status(meta.get("status", "")) == "completed" and body:
        retrieval = "success"

    if _task_output_is_running(meta):
        ctx.state["__suppress_generic_expand_hint__"] = True
        return fg("dim", "Task is still running…")

    if retrieval != "success" or not body:
        ctx.state["__suppress_generic_expand_hint__"] = True
        if retrieval == "not_ready":
            return fg("dim", "Task is still running…")
        return fg("dim", "No task output available")

    if not ctx.expanded:
        ctx.state["__suppress_generic_expand_hint__"] = True
        return _read_output_collapsed_hint()

    line_count = body.count("\n") + 1 if body else 0
    children: list[RenderableType] = [
        Text(f"{description} ({line_count} lines)", style=tui_rich_style("tool_title"))
    ]
    body_block, remaining = format_lines_block(
        body,
        expanded=True,
        collapsed_max_lines=collapsed_lines,
        style_token="tool_output",
    )
    children.append(body_block)
    if remaining > 0:
        children.append(fg("muted", f"… ({remaining} more lines)"))
    error = meta.get("error")
    if error:
        children.append(fg("error", f"Error: {error}"))
    return Group(*children)


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
    render_result=lambda ctx, r: _render_task_output_result(ctx, r, collapsed_lines=20),
)


# ---------------------------------------------------------------------------
# TaskInput
# ---------------------------------------------------------------------------


def _render_task_input_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    extras: list[str] = []
    text = as_str(args.get("text"))
    if text is None:
        if "text" in args:
            extras.append("<invalid input>")
        elif ctx.has_result:
            extras.append("<missing text>")
    else:
        extras.append(_safe_task_input_preview(text))
    if args.get("newline") is False:
        extras.append("no newline")
    return _render_call_with_id("TaskInput", ctx, extras=extras)


def _render_task_input_result(
    ctx: ToolRenderContext,
    result: ToolResultPayload,
) -> RenderableType | None:
    _stash_task_label(ctx, result)
    if not result.text:
        return None
    if result.is_error:
        return _render_block_result(ctx, result)

    meta = _parse_task_metadata(result.text)
    if not meta:
        return _render_block_result(ctx, result)

    summary = Text("Input queued", style=tui_rich_style("tool_output"))
    if status := _status_display(_tool_status_from_result(result, meta)):
        summary.append_text(fg("muted", f" · {status}"))
    if newline := meta.get("newline"):
        summary.append_text(fg("muted", f" · newline {newline}"))

    if ctx.expanded:
        return _render_expanded_metadata(summary, result.text)

    ctx.state["__has_expandable_payload__"] = True
    return summary


TASK_INPUT_RENDERER = ToolRenderDefinition(
    name="TaskInput",
    label="task input",
    render_shell="default",
    render_call=_render_task_input_call,
    render_result=_render_task_input_result,
)


# ---------------------------------------------------------------------------
# TaskHandoff
# ---------------------------------------------------------------------------


def _render_task_handoff_call(ctx: ToolRenderContext) -> RenderableType:
    return _render_call_with_id("TaskHandoff", ctx, extras=[])


def _render_task_handoff_result(
    ctx: ToolRenderContext,
    result: ToolResultPayload,
) -> RenderableType | None:
    _stash_task_label(ctx, result)
    if not result.text:
        return None
    if result.is_error:
        return _render_block_result(ctx, result)

    meta = _parse_task_metadata(result.text)
    if not meta:
        return _render_block_result(ctx, result)

    summary = Text("Handoff details", style=tui_rich_style("tool_output"))
    for value in (
        _status_display(_tool_status_from_result(result, meta)),
        normalize_agent_status(meta.get("status", "")),
        meta.get("description", ""),
    ):
        if value:
            summary.append_text(fg("muted", f" · {value}"))
    if output_path := meta.get("output_path"):
        summary.append_text(fg("muted", f" · {shorten_path(output_path, cwd=ctx.cwd)}"))

    if ctx.expanded:
        return _render_expanded_metadata(summary, result.text)

    ctx.state["__has_expandable_payload__"] = True
    return summary


TASK_HANDOFF_RENDERER = ToolRenderDefinition(
    name="TaskHandoff",
    label="task handoff",
    render_shell="default",
    render_call=_render_task_handoff_call,
    render_result=_render_task_handoff_result,
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
