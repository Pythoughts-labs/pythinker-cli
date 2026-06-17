"""Renderers for memory-family tools: ``Memory``, ``Recall``, and ``Scratchpad``."""

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
    tool_call_header,
)
from pythinker_code.ui.theme import tui_rich_style

_EXPANDED_LINES = 15
_SESSION_RE = re.compile(r"^\s*-\s*session_id:\s*(?P<id>\S+)", re.MULTILINE)
_NUMBERED_ENTRY_RE = re.compile(r"^\s*\d+[.)]\s+\S+")
_BULLET_ENTRY_RE = re.compile(r"^\s*[-*]\s+\S+")
_SCRATCHPAD_SUCCESS_RE = re.compile(r"^Note recorded \((?P<kind>[^)]+)\)\.?$")


def _plural(count: int, singular: str) -> str:
    if singular == "entry":
        return "entry" if count == 1 else "entries"
    return singular if count == 1 else f"{singular}s"


def _text_or_message(result: ToolResultPayload) -> str:
    if result.text:
        return result.text
    message = result.details.get("message")
    return message if isinstance(message, str) else ""


def _bounded_body(text: str, *, expanded: bool, style_token: str) -> tuple[Text, int]:
    return format_lines_block(
        text,
        expanded=False,
        collapsed_max_lines=_EXPANDED_LINES if expanded else 0,
        style_token=style_token,
    )


def _preserve_text(text: str, *, style_token: str) -> Text | None:
    body, _remaining = format_lines_block(
        text,
        expanded=True,
        collapsed_max_lines=0,
        style_token=style_token,
    )
    return body if body.plain else None


def _mark_expandable_payload(ctx: ToolRenderContext) -> None:
    ctx.state["__has_expandable_payload__"] = True


def _entry_count(text: str) -> int:
    lines = [line for line in text.splitlines() if line.strip()]
    entries = [
        line for line in lines if _NUMBERED_ENTRY_RE.match(line) or _BULLET_ENTRY_RE.match(line)
    ]
    return len(entries) if entries else len(lines)


def _target_label(target: str | None) -> str | None:
    if target == "memory":
        return "project memory"
    if target == "user":
        return "user memory"
    return None


def _memory_call_summary(ctx: ToolRenderContext) -> Text | RenderableType:
    args = ctx.args or {}
    action = as_str(args.get("action"))
    target = as_str(args.get("target"))
    summary = Text()

    if action is None:
        if "action" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append_text(missing_required_arg("action"))
        else:
            return pending_tool_call_header("Memory")
    else:
        summary.append_text(fg("tool_output", action))

    if target is None:
        if "target" in args:
            summary.append(" ", style=tui_rich_style("muted"))
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append(" ", style=tui_rich_style("muted"))
            summary.append_text(missing_required_arg("target"))
        else:
            return pending_tool_call_header("Memory")
        return summary

    label = _target_label(target)
    if label is None:
        summary.append(" ", style=tui_rich_style("muted"))
        summary.append_text(invalid_arg())
        return summary

    if action in {"add"}:
        connector = " to "
    elif action in {"replace"}:
        connector = " in "
    elif action in {"remove"}:
        connector = " from "
    else:
        connector = " "
    summary.append(connector, style=tui_rich_style("muted"))
    summary.append(label, style=tui_rich_style("tool_output"))
    return summary


def _render_memory_call(ctx: ToolRenderContext) -> RenderableType:
    summary = _memory_call_summary(ctx)
    if isinstance(summary, Text):
        style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
        line = tool_call_header("Memory", summary, style_token=style_token)
    else:
        line = summary
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _memory_action_result(action: str | None, target: str | None) -> str:
    label = _target_label(target) or "memory"
    if action == "add":
        return f"Added {label}"
    if action == "replace":
        return f"Updated {label}"
    if action == "remove":
        return f"Removed {label}"
    return label.capitalize()


def _render_memory_result(
    ctx: ToolRenderContext, result: ToolResultPayload
) -> RenderableType | None:
    text = _text_or_message(result)
    if not text:
        return None

    if result.is_error or text.startswith("Not saved to memory"):
        return _preserve_text(text, style_token="error" if result.is_error else "tool_output")

    ctx.state["__suppress_generic_expand_hint__"] = True
    action = as_str((ctx.args or {}).get("action"))
    target = as_str((ctx.args or {}).get("target"))
    label = _target_label(target) or "memory"

    if action == "list":
        count = _entry_count(text)
        summary = Text()
        summary.append(f"Listed {label}", style=tui_rich_style("tool_output"))
        summary.append(" · ", style=tui_rich_style("muted"))
        summary.append(str(count), style=tui_rich_style("tool_title"))
        summary.append(f" {_plural(count, 'entry')}", style=tui_rich_style("muted"))
        if not ctx.expanded:
            _mark_expandable_payload(ctx)
            summary.append(" ")
            summary.append_text(key_hint("ctrl+o", "expand"))
            return summary
        body, remaining = _bounded_body(text, expanded=True, style_token="tool_output")
        children: list[RenderableType] = [summary]
        if body.plain:
            children.append(body)
        if remaining > 0:
            children.append(fg("muted", expand_hint(remaining)))
        return Group(*children)

    return fg("tool_output", _memory_action_result(action, target))


MEMORY_RENDERER = ToolRenderDefinition(
    name="Memory",
    label="memory",
    render_shell="default",
    render_call=_render_memory_call,
    render_result=_render_memory_result,
)


def _render_recall_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    mode = as_str(args.get("mode"))
    summary = Text()
    if mode is None:
        if "mode" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append_text(missing_required_arg("mode"))
        else:
            line = pending_tool_call_header("Recall")
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    elif mode == "search":
        query = as_str(args.get("query"))
        summary.append("search", style=tui_rich_style("tool_output"))
        if query:
            summary.append(" ")
            summary.append_text(fg_subject(f'"{query}"'))
    elif mode == "read":
        session_id = as_str(args.get("session_id"))
        summary.append("read", style=tui_rich_style("tool_output"))
        if session_id:
            summary.append(" ", style=tui_rich_style("muted"))
            summary.append_text(fg("tool_output", session_id))
        elif "session_id" in args:
            summary.append(" ", style=tui_rich_style("muted"))
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append(" ", style=tui_rich_style("muted"))
            summary.append_text(missing_required_arg("session_id"))
        offset = args.get("message_offset")
        limit = args.get("max_messages")
        if isinstance(offset, int) and offset:
            summary.append(f" · offset {offset}", style=tui_rich_style("muted"))
        if isinstance(limit, int):
            summary.append(f" · limit {limit}", style=tui_rich_style("muted"))
    else:
        summary.append_text(invalid_arg())

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("Recall", summary, style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _message_text(result: ToolResultPayload) -> str | None:
    message = result.details.get("message")
    return message.strip() if isinstance(message, str) and message.strip() else None


def _recall_search_count(text: str, result: ToolResultPayload) -> int:
    message = _message_text(result)
    if message:
        match = re.search(r"Found\s+(\d+)\s+prior session", message)
        if match:
            return int(match.group(1))
    return len(_SESSION_RE.findall(text))


def _render_recall_result(
    ctx: ToolRenderContext, result: ToolResultPayload
) -> RenderableType | None:
    text = _text_or_message(result)
    if not text:
        return None

    if result.is_error:
        return _preserve_text(text, style_token="error")

    ctx.state["__suppress_generic_expand_hint__"] = True
    mode = as_str((ctx.args or {}).get("mode"))
    if mode == "search":
        if text.startswith("No matching prior sessions"):
            return fg("tool_output", text.rstrip("\n"))
        count = _recall_search_count(text, result)
        summary = Text()
        summary.append("Found ", style=tui_rich_style("tool_output"))
        summary.append(str(count), style=tui_rich_style("tool_title"))
        summary.append(f" prior {_plural(count, 'session')}", style=tui_rich_style("tool_output"))
        if not ctx.expanded:
            if count:
                _mark_expandable_payload(ctx)
                summary.append(" ")
                summary.append_text(key_hint("ctrl+o", "expand"))
            return summary
        body, remaining = _bounded_body(text, expanded=True, style_token="tool_output")
        children: list[RenderableType] = [summary]
        if body.plain:
            children.append(body)
        if remaining > 0:
            children.append(fg("muted", expand_hint(remaining)))
        return Group(*children)

    if mode == "read":
        session_id = as_str((ctx.args or {}).get("session_id")) or "session"
        message = _message_text(result)
        summary = fg("tool_output", message if message else f"Read session {session_id}.")
        if not ctx.expanded:
            _mark_expandable_payload(ctx)
            summary.append(" ")
            summary.append_text(key_hint("ctrl+o", "expand"))
            return summary
        body, remaining = _bounded_body(text, expanded=True, style_token="tool_output")
        children = [summary]
        if body.plain:
            children.append(body)
        if remaining > 0:
            children.append(fg("muted", expand_hint(remaining)))
        return Group(*children)

    return _preserve_text(text, style_token="tool_output")


RECALL_RENDERER = ToolRenderDefinition(
    name="Recall",
    label="recall",
    render_shell="default",
    render_call=_render_recall_call,
    render_result=_render_recall_result,
)


def _render_scratchpad_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    kind = as_str(args.get("kind"))
    summary = Text()
    if kind is None:
        if "kind" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append("note", style=tui_rich_style("tool_output"))
        else:
            line = pending_tool_call_header("Scratchpad")
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    else:
        summary.append_text(fg("tool_output", kind))

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("Scratchpad", summary, style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _render_scratchpad_result(
    _ctx: ToolRenderContext, result: ToolResultPayload
) -> RenderableType | None:
    text = _text_or_message(result)
    if not text:
        return None
    if result.is_error:
        return _preserve_text(text, style_token="error")
    if match := _SCRATCHPAD_SUCCESS_RE.match(text.strip()):
        kind = match.group("kind")
        return fg("tool_output", f"Recorded {kind} note")
    return _preserve_text(text, style_token="tool_output")


SCRATCHPAD_RENDERER = ToolRenderDefinition(
    name="Scratchpad",
    label="scratchpad",
    render_shell="default",
    render_call=_render_scratchpad_call,
    render_result=_render_scratchpad_result,
)
