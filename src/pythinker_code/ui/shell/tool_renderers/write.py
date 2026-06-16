"""Blackbox-style renderer for Pythinker's ``WriteFile`` tool.

The tool-use row stays compact (``write path`` / ``append path``). Success
results render like the reference file-write UI: created files
show ``Wrote N lines to path`` plus a capped content preview, while updates
prefer the real diff display blocks returned by the Python tool.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.text import Text

from pythinker_code.tools.display import DiffDisplayBlock
from pythinker_code.ui.shell.render_constants import expand_hint
from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolRenderDefinition,
    ToolResultPayload,
)
from pythinker_code.ui.shell.tool_renderers._file_diff import (
    change_summary_text,
    diff_blocks_from_result,
    diff_frame,
    preview_from_diff_blocks,
)
from pythinker_code.ui.shell.spacing import blank_row
from pythinker_code.ui.shell.tool_renderers._render_utils import (
    as_str,
    fg,
    format_lines_block,
    format_numbered_lines_block,
    invalid_arg,
    missing_required_arg,
    pending_tool_call_header,
    running_spinner,
    shorten_path,
    tool_call_header,
)

_TOOL_NAME = "WriteFile"
_DEFAULT_PREVIEW_LINES = 10


def _render_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    raw_path = as_str(args.get("path"))
    mode = args.get("mode")

    summary = Text()
    if raw_path is None:
        if "path" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append_text(missing_required_arg("path"))
        else:
            line = pending_tool_call_header("Append" if mode == "append" else "Write")
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    else:
        summary.append_text(fg("info", shorten_path(raw_path, cwd=ctx.cwd)))

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header(
        "Append" if mode == "append" else "Write",
        summary,
        style_token=style_token,
    )
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _count_visible_lines(text: str) -> int:
    if not text:
        return 0
    parts = text.split("\n")
    return len(parts) - 1 if text.endswith("\n") else len(parts)


def _render_created_or_appended(
    ctx: ToolRenderContext, content: str, *, append: bool
) -> RenderableType:
    args = ctx.args or {}
    path = as_str(args.get("path")) or ""
    display_path = shorten_path(path, cwd=ctx.cwd) if path else "<missing path>"
    line_count = _count_visible_lines(content)
    noun = "line" if line_count == 1 else "lines"
    verb = "Appended" if append else "Wrote"

    summary = Text(f"{verb} ")
    summary.append(str(line_count), style="bold")
    summary.append(f" {noun} to ")
    summary.append(display_path, style="bold")

    preview_text = content or "(No content)"
    body, remaining, _total = format_numbered_lines_block(
        preview_text,
        expanded=ctx.expanded,
        collapsed_max_lines=_DEFAULT_PREVIEW_LINES,
        style_token="tool_output",
    )
    children: list[RenderableType] = [fg("tool_output", summary)]
    if body.plain:
        children.append(body)
    if remaining > 0:
        ctx.state["__suppress_generic_expand_hint__"] = True
        children.append(fg("muted", expand_hint(remaining)))
    return Group(*children)


def _is_existing_file_diff(blocks: list[DiffDisplayBlock]) -> bool:
    return any(block.is_summary or block.old_text.strip() for block in blocks)


def _render_result(ctx: ToolRenderContext, result: ToolResultPayload) -> RenderableType | None:
    if result.is_error:
        if not result.text:
            return fg("error", "Error writing file")
        body, _ = format_lines_block(
            result.text,
            expanded=True,
            collapsed_max_lines=0,
            style_token="error",
        )
        return body if body.plain else fg("error", result.text.rstrip("\n"))

    diff_blocks = diff_blocks_from_result(result)
    preview = preview_from_diff_blocks(diff_blocks)
    mode = ctx.args.get("mode")
    raw_content = as_str(ctx.args.get("content")) or ""

    if (
        preview is not None
        and not preview.is_new_file
        and (preview.removed > 0 or _is_existing_file_diff(diff_blocks))
    ):
        return Group(
            change_summary_text(preview.added, preview.removed),
            blank_row(),
            diff_frame(
                preview.diff_text,
                width=ctx.width or 80,
                expanded=ctx.expanded,
                state=ctx.state,
            ),
        )

    return _render_created_or_appended(ctx, raw_content, append=mode == "append")


WRITE_RENDERER = ToolRenderDefinition(
    name=_TOOL_NAME,
    label="write",
    render_shell="default",
    render_call=_render_call,
    render_result=_render_result,
)
