"""Pythinker renderer for Pythinker's ``StrReplaceFile`` tool.

 .

Source tool name → Pythinker tool name: ``edit`` → ``StrReplaceFile``.
Param shape: Pythinker uses ``edit: Edit | list[Edit]`` where each ``Edit``
has ``{old, new, replace_all}``.

Pythinker's tool produces the diff out-of-band (via display blocks). The
renderer prefers those real diff blocks after execution and only uses call args
as a pending/fallback preview.
"""

from __future__ import annotations

from typing import Any, cast

from rich.console import Group, RenderableType
from rich.text import Text

from pythinker_code.ui.shell.components import compute_edit_diff_string
from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolRenderDefinition,
    ToolResultPayload,
)
from pythinker_code.ui.shell.tool_renderers._file_diff import (
    change_summary_text,
    diff_frame,
    preview_from_result,
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

_TOOL_NAME = "StrReplaceFile"


def _normalize_edits(edit_arg: Any) -> list[dict[str, Any]]:
    """Coerce ``args["edit"]`` into a list of dicts, ignoring junk."""
    if edit_arg is None:
        return []
    items: list[Any] = cast("list[Any]", edit_arg) if isinstance(edit_arg, list) else [edit_arg]
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            out.append(cast("dict[str, Any]", item))
    return out


def _build_combined_diff(edits: list[dict[str, Any]]) -> str:
    """Render each edit's (old → new) as a Pythinker-format diff block, joined.

    For multi-edit calls we render them sequentially with a blank separator.
    Each block is line-numbered relative to its own ``old`` text — we don't
    have the file content here, so absolute line numbers aren't possible.
    """
    blocks: list[str] = []
    for edit in edits:
        old = edit.get("old")
        new = edit.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            continue
        result = compute_edit_diff_string(old, new)
        if result.diff:
            blocks.append(result.diff)
    return "\n\n".join(blocks)


def _render_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    raw_path = as_str(args.get("path"))

    summary = Text()
    if raw_path is None:
        if "path" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append_text(missing_required_arg("path"))
        else:
            line = pending_tool_call_header("Update")
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    else:
        summary.append_text(fg("info", shorten_path(raw_path, cwd=ctx.cwd)))

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    header = tool_call_header("Update", summary, style_token=style_token)

    edits = _normalize_edits(args.get("edit"))
    if len(edits) > 1:
        header.append_text(fg("tool_output", f" ({len(edits)} edits)"))

    head = running_spinner(
        header,
        execution_started=ctx.execution_started,
        has_result=ctx.has_result,
    )

    # The reference renderer keeps the tool-use row compact and places the
    # actual diff in the result/rejection message.  While the tool is still
    # running we keep a small args-based preview so long approvals do not look
    # empty; once a result exists, render_result prefers the real display diff.
    if ctx.has_result:
        return head
    diff_text = _build_combined_diff(edits)
    if not diff_text:
        return head
    added, removed = _fallback_diff_counts(diff_text)
    return Group(
        head,
        change_summary_text(added, removed),
        diff_frame(
            diff_text,
            width=ctx.width or 80,
            expanded=ctx.expanded,
            state=ctx.state,
        ),
    )


def _fallback_diff_counts(diff_text: str) -> tuple[int, int]:
    added = removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _friendly_error(text: str) -> str:
    lowered = text.lower()
    if "not been read" in lowered or ("read" in lowered and "first" in lowered):
        return "File must be read first"
    if "does not exist" in lowered or "file not found" in lowered:
        return "File not found"
    if text.strip():
        return text.rstrip("\n")
    return "Error editing file"


def _render_result(ctx: ToolRenderContext, result: ToolResultPayload) -> RenderableType | None:
    if result.is_error:
        return fg("error", _friendly_error(result.text))

    preview = preview_from_result(result)
    if preview is None:
        edits = _normalize_edits(ctx.args.get("edit"))
        diff_text = _build_combined_diff(edits)
        if not diff_text:
            message = result.details.get("message")
            return fg("muted", str(message)) if message else None
        added, removed = _fallback_diff_counts(diff_text)
        preview_diff = diff_text
    else:
        added, removed = preview.added, preview.removed
        preview_diff = preview.diff_text

    return Group(
        change_summary_text(added, removed),
        diff_frame(
            preview_diff,
            width=ctx.width or 80,
            expanded=ctx.expanded,
            state=ctx.state,
        ),
    )


EDIT_RENDERER = ToolRenderDefinition(
    name=_TOOL_NAME,
    label="edit",
    render_shell="default",
    render_call=_render_call,
    render_result=_render_result,
)
