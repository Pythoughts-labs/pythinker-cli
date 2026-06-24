"""Pythinker renderer for the ``ImplementAndJudge`` chain tool.

The generic fallback renders this as ``ImplementAndJudge(4 args: acceptance,
base_prompt, brief, scope)`` — an unreadable arg dump. This dedicated renderer
shows the chain as ``⏺ Implement & Judge — <brief>`` so the card reads as a
single clear action instead of leaking the parameter names.
"""

from __future__ import annotations

from rich.console import Group, RenderableType
from rich.text import Text

from pythinker_code.ui.shell.components.render_utils import sanitize_ansi
from pythinker_code.ui.shell.glyphs import TRANSCRIPT_ASSISTANT_MARKER
from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolRenderDefinition,
    ToolResultPayload,
)
from pythinker_code.ui.shell.tool_renderers._render_utils import (
    as_str,
    fg,
    format_lines_block,
    missing_required_arg,
    pending_tool_call_header,
    running_spinner,
    tool_title,
)
from pythinker_code.ui.theme import tui_rich_style

# Internal tool name stays ``ImplementAndJudge`` (registry/config/tests); only
# the on-screen label is the friendlier form. Kept in sync with
# ``pythinker_code.tools.agent.IMPLEMENT_JUDGE_NAME`` by a focused test.
_TOOL_NAME = "ImplementAndJudge"
_DISPLAY_NAME = "Implement & Judge"
_BRIEF_MAX_CHARS = 80
_COLLAPSED_LINES = 8


def _compact(text: str, *, max_chars: int = _BRIEF_MAX_CHARS) -> str:
    """Collapse whitespace and ellipsize a possibly multi-line brief."""
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    if max_chars <= 1:
        return "…"
    return compact[: max_chars - 1].rstrip() + "…"


def _render_call(ctx: ToolRenderContext) -> RenderableType:
    args = ctx.args or {}
    brief = as_str(args.get("brief"))
    style_token = "error" if ctx.is_error else "success" if ctx.has_result else "muted"

    if brief is None:
        # Streamed args still incomplete, or the required brief is missing.
        if ctx.has_result:
            header: RenderableType = Group(
                _label_header(style_token), missing_required_arg("brief")
            )
        else:
            header = pending_tool_call_header(_DISPLAY_NAME)
        return running_spinner(
            header,
            execution_started=ctx.execution_started,
            has_result=ctx.has_result,
            marker_style_token="muted",
        )

    header = _label_header(style_token)
    header.append(" — ", style=tui_rich_style("muted"))
    header.append(sanitize_ansi(_compact(brief)), style=tui_rich_style("thinking_text"))
    header.no_wrap = True
    header.overflow = "ellipsis"
    return running_spinner(
        header,
        execution_started=ctx.execution_started,
        has_result=ctx.has_result,
        marker_style_token="muted",
    )


def _label_header(style_token: str) -> Text:
    marker = "✘" if style_token == "error" else TRANSCRIPT_ASSISTANT_MARKER
    header = Text()
    header.append(f"{marker} ", style=tui_rich_style(style_token))
    header.append_text(tool_title(_DISPLAY_NAME))
    return header


def _render_result(ctx: ToolRenderContext, result: ToolResultPayload) -> RenderableType | None:
    text = sanitize_ansi(result.text or "").rstrip("\n")
    if not text:
        return None
    body, remaining = format_lines_block(
        text,
        expanded=ctx.expanded,
        collapsed_max_lines=_COLLAPSED_LINES,
        style_token="error" if result.is_error else "tool_output",
    )
    if remaining > 0:
        ctx.state["__suppress_generic_expand_hint__"] = True
        return Group(body, fg("muted", f"… ({remaining} more lines, ctrl+o to expand)"))
    return body


IMPLEMENT_JUDGE_RENDERER = ToolRenderDefinition(
    name=_TOOL_NAME,
    label=_DISPLAY_NAME,
    render_shell="default",
    render_call=_render_call,
    render_result=_render_result,
)
