"""Pythinker renderers for Pythinker's ``FetchURL`` and ``SearchWeb`` tools."""

from __future__ import annotations

from typing import cast

from rich.console import Group, RenderableType
from rich.text import Text

from pythinker_code.ui.shell.components.key_hints import key_hint
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


def _shorten_url(url: str, *, max_chars: int = 60) -> str:
    if len(url) <= max_chars:
        return url
    return url[: max_chars - 1].rstrip() + "…"


def _plural(count: int, singular: str) -> str:
    return singular if count == 1 else f"{singular}s"


def _nonempty_lines(text: str) -> list[str]:
    return [line for line in (text or "").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# FetchURL
# ---------------------------------------------------------------------------


def _render_fetch_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    url = as_str(args.get("url"))
    summary = Text()
    if url is None:
        if "url" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append_text(missing_required_arg("url"))
        else:
            line = pending_tool_call_header("Fetch")
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    else:
        summary.append_text(fg_subject(_shorten_url(url)))
    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("Fetch", summary, style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _render_fetch_result(
    ctx: ToolRenderContext, result: ToolResultPayload
) -> RenderableType | None:
    if not result.text:
        return None
    if result.is_error:
        body, remaining = format_lines_block(
            result.text,
            expanded=ctx.expanded,
            collapsed_max_lines=15,
            style_token="error",
        )
        if not body.plain:
            return None
        if remaining > 0:
            return Group(body, fg("muted", f"... ({remaining} more lines, ctrl+o to expand)"))
        return body

    byte_count = len(result.text.encode("utf-8"))
    line_count = len(_nonempty_lines(result.text))
    summary = Text()
    summary.append("Received ", style=tui_rich_style("tool_output"))
    summary.append(f"{byte_count:,}", style=tui_rich_style("tool_title"))
    summary.append(" bytes", style=tui_rich_style("tool_output"))
    if line_count:
        summary.append(" · ", style=tui_rich_style("muted"))
        summary.append(str(line_count), style=tui_rich_style("tool_title"))
        summary.append(f" {_plural(line_count, 'line')}", style=tui_rich_style("muted"))

    ctx.state["__suppress_generic_expand_hint__"] = True
    if not ctx.expanded:
        summary.append(" ")
        summary.append_text(key_hint("ctrl+o", "expand"))
        return summary

    body, remaining = format_lines_block(
        result.text,
        expanded=True,
        collapsed_max_lines=15,
        style_token="tool_output",
    )
    children: list[RenderableType] = [summary]
    if body.plain:
        children.append(body)
    if remaining > 0:
        children.append(fg("muted", f"... ({remaining} more lines, ctrl+o to expand)"))
    return Group(*children)


FETCH_RENDERER = ToolRenderDefinition(
    name="FetchURL",
    label="fetch",
    render_shell="default",
    render_call=_render_fetch_call,
    render_result=_render_fetch_result,
)


# ---------------------------------------------------------------------------
# SearchWeb
# ---------------------------------------------------------------------------


def _render_search_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    query = as_str(args.get("query"))
    limit = args.get("limit")
    include_content = bool(args.get("include_content"))

    summary = Text()
    if query is None:
        if "query" in args:
            summary.append_text(invalid_arg())
        elif ctx.has_result:
            summary.append_text(missing_required_arg("query"))
        else:
            line = pending_tool_call_header("WebSearch")
            return running_spinner(
                line, execution_started=ctx.execution_started, has_result=ctx.has_result
            )
    else:
        summary.append_text(fg_subject(f'"{query}"'))
    extras: list[str] = []
    if isinstance(limit, int) and limit != 5:
        extras.append(f"limit {limit}")
    if include_content:
        extras.append("with content")
    for extra in extras:
        summary.append_text(fg("muted", f" · {extra}"))

    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"
    line = tool_call_header("WebSearch", summary, style_token=style_token)
    return running_spinner(line, execution_started=ctx.execution_started, has_result=ctx.has_result)


def _search_result_count(text: str) -> int:
    """Count results emitted by SearchWeb's `Title/Date/URL/Summary` blocks."""
    blocks = [block for block in text.split("\n---\n\n") if block.strip()]
    structured_count = sum(
        1
        for block in blocks
        if any(line.startswith("Title: ") for line in block.splitlines())
        and any(line.startswith("URL: ") for line in block.splitlines())
    )
    if structured_count:
        return structured_count
    return len([line for line in text.splitlines() if line.strip()])


def _allowlist_filtered_count(result: ToolResultPayload) -> int:
    """Number of search results dropped by the domain allowlist, if any."""
    extras_raw = result.details.get("extras")
    extras = cast("dict[str, object]", extras_raw) if isinstance(extras_raw, dict) else {}
    value = extras.get("allowlist_filtered")
    if isinstance(value, int) and value > 0:
        return value
    return 0


def _explicit_result_count(result: ToolResultPayload) -> int | None:
    """The tool's own result count, if it emitted one (preferred over text)."""
    extras_raw = result.details.get("extras")
    extras = cast("dict[str, object]", extras_raw) if isinstance(extras_raw, dict) else {}
    value = extras.get("returned_results")
    return value if isinstance(value, int) and value >= 0 else None


def _render_search_result(
    ctx: ToolRenderContext, result: ToolResultPayload
) -> RenderableType | None:
    if not result.text:
        return None
    if result.is_error:
        body, remaining = format_lines_block(
            result.text,
            expanded=ctx.expanded,
            collapsed_max_lines=15,
            style_token="error",
        )
        if not body.plain:
            return None
        if remaining > 0:
            return Group(body, fg("muted", f"... ({remaining} more lines, ctrl+o to expand)"))
        return body

    explicit = _explicit_result_count(result)
    count = explicit if explicit is not None else _search_result_count(result.text)
    summary = Text()
    summary.append("Found ", style=tui_rich_style("tool_output"))
    summary.append(str(count), style=tui_rich_style("tool_title"))
    summary.append(f" {_plural(count, 'result')}", style=tui_rich_style("tool_output"))
    if filtered := _allowlist_filtered_count(result):
        summary.append(f" · {filtered} filtered to allowlist", style=tui_rich_style("muted"))
    ctx.state["__suppress_generic_expand_hint__"] = True
    if count and not ctx.expanded:
        summary.append(" ")
        summary.append_text(key_hint("ctrl+o", "expand"))
        return summary
    if not ctx.expanded:
        return summary

    body, remaining = format_lines_block(
        result.text,
        expanded=True,
        collapsed_max_lines=15,
        style_token="tool_output",
    )
    children: list[RenderableType] = [summary]
    if body.plain:
        children.append(body)
    if remaining > 0:
        children.append(fg("muted", f"... ({remaining} more lines, ctrl+o to expand)"))
    return Group(*children)


SEARCH_RENDERER = ToolRenderDefinition(
    name="SearchWeb",
    label="search",
    render_shell="default",
    render_call=_render_search_call,
    render_result=_render_search_result,
)
