"""Pythinker tool execution card.

Wraps a registered :class:`ToolRenderDefinition` and renders it as a compact
shell tool card row.

The card lifecycle:

* arguments stream in        → status = ``PENDING``,  no background tint
* execution starts           → status = ``RUNNING``,  no background tint
* result arrives, no error   → status = ``SUCCESS``,  no background tint
* result arrives, error      → status = ``ERROR``,    bg = ``tool_error_bg``
* user cancels / denies      → status = ``CANCELLED`` / ``DENIED``

If the renderer produces no visible output for the current state, the
component renders an empty string (the ``hidden-component`` behavior).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.style import Style
from rich.text import Text

from pythinker_code.ui.shell.components.key_hints import key_hint
from pythinker_code.ui.shell.components.render_utils import render_message_response
from pythinker_code.ui.shell.glyphs import TRANSCRIPT_ACTIVE_MARKER, TRANSCRIPT_ASSISTANT_MARKER
from pythinker_code.ui.shell.motion import blink_visible
from pythinker_code.ui.shell.spacing import TINTED_CARD_PADDING
from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolRenderDefinition,
    ToolResultPayload,
)
from pythinker_code.ui.theme import tui_rich_style


class ToolExecutionStatus(Enum):
    """Lifecycle states of a tool call card."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    CANCELLED = "cancelled"


_MAX_RESULT_LINES = 60
_MAX_RESULT_CHARS = 4000


@dataclass(slots=True)
class _CallState:
    """Snapshot of the inputs the component needs to compose a render."""

    tool_name: str
    tool_call_id: str
    cwd: str = ""
    args: dict[str, Any] | None = None
    args_complete: bool = False
    execution_started: bool = False
    expanded: bool = False
    result: ToolResultPayload | None = None
    is_partial: bool = False


class ToolExecutionComponent:
    """Single tool invocation rendered as a Pythinker card."""

    def __init__(
        self,
        tool_name: str,
        tool_call_id: str,
        *,
        definition: ToolRenderDefinition,
        cwd: str = "",
    ) -> None:
        self._definition = definition
        self._state = _CallState(tool_name=tool_name, tool_call_id=tool_call_id, cwd=cwd)
        self._started_at = time.monotonic()
        self._finished_elapsed_s: float | None = None
        self._renderer_state: dict[str, Any] = {"__tool_name__": tool_name}
        self._status = ToolExecutionStatus.PENDING

    # -- Mutators ------------------------------------------------------------

    def update_args(self, args: dict[str, Any]) -> None:
        self._state.args = args

    def set_args_complete(self) -> None:
        self._state.args_complete = True

    def mark_execution_started(self) -> None:
        self._state.execution_started = True
        if self._status == ToolExecutionStatus.PENDING:
            self._status = ToolExecutionStatus.RUNNING

    def set_result(
        self,
        result: ToolResultPayload,
        *,
        is_partial: bool = False,
    ) -> None:
        self._state.result = result
        self._state.is_partial = is_partial
        if is_partial:
            self._status = ToolExecutionStatus.RUNNING
        else:
            if self._finished_elapsed_s is None:
                self._finished_elapsed_s = max(0.0, time.monotonic() - self._started_at)
            self._status = (
                ToolExecutionStatus.ERROR if result.is_error else ToolExecutionStatus.SUCCESS
            )

    def set_status(self, status: ToolExecutionStatus) -> None:
        """Force a specific status (e.g. DENIED, CANCELLED)."""
        self._status = status

    def set_expanded(self, expanded: bool) -> None:
        self._state.expanded = expanded

    @property
    def status(self) -> ToolExecutionStatus:
        return self._status

    @property
    def expanded(self) -> bool:
        return self._state.expanded

    @property
    def can_expand(self) -> bool:
        return self._state.expanded or self._has_expandable_payload()

    def toggle_expanded(self) -> None:
        self._state.expanded = not self._state.expanded

    @property
    def tool_call_id(self) -> str:
        return self._state.tool_call_id

    def invalidate(self) -> None:  # pragma: no cover — protocol stub
        """Drop cached output. Currently a no-op (no caching layer yet)."""

    # -- Rendering -----------------------------------------------------------

    def render(self, width: int = 0) -> RenderableType:  # noqa: ARG002 — width reserved
        """Return the card renderable for the current state.

        *width* is accepted for protocol compatibility; Rich console width
        is the source of truth at print time.
        """
        if width <= 0:
            try:
                from pythinker_code.ui.shell.console import console

                width = console.size.width
            except Exception:  # noqa: BLE001 - rendering must not fail on width lookup
                width = 100
        self._renderer_state.pop("__has_expandable_payload__", None)
        self._renderer_state.pop("__suppress_generic_expand_hint__", None)
        ctx = self._build_context(width=width)
        children: list[RenderableType] = []

        # Render the result first so a renderer can stash state (e.g. a resolved
        # task label) that its call header reads in the same frame; append in
        # display order (header above result) below.
        result = self._state.result
        rendered_result: RenderableType | None = None
        if result is not None:
            if self._definition.render_result is not None:
                try:
                    rendered_result = self._definition.render_result(ctx, result)
                except Exception:  # noqa: BLE001
                    rendered_result = self._result_fallback()
            else:
                rendered_result = self._result_fallback()

        if self._definition.render_call is not None:
            try:
                call = self._definition.render_call(ctx)
            except Exception:  # noqa: BLE001 — renderer crash falls back to header
                call = self._call_fallback()
            if call is not None:
                children.append(self._blink_running_marker(call))
        else:
            children.append(self._call_fallback())

        if rendered_result is not None:
            children.append(rendered_result)

        if not self._state.expanded and self._is_truncatable():
            children.append(key_hint("app.tools.expand", "expand"))

        if not children:
            return Text("")

        if len(children) <= 1:
            body: RenderableType = children[0] if children else Text("")
        else:
            # Reference layout: tool header first, then response/progress rows
            # immediately below under the dim ``⎿`` gutter. No spacer — the
            # gutter is the visual separation.
            body = Group(
                children[0],
                *(render_message_response(child) for child in children[1:]),
            )

        if self._definition.render_shell == "self":
            return body

        bg_style = self._background_style()
        if bg_style is None:
            return body
        # Error/denied rows retain a subtle tint. Normal pending/running rows
        # intentionally do not: normal rows sit directly on the terminal
        # background unless a message is selected.
        return Padding(body, TINTED_CARD_PADDING, style=bg_style)

    # -- Internals -----------------------------------------------------------

    def _blink_running_marker(self, call: RenderableType) -> RenderableType:
        """Blink the leading row marker while the tool is still running.

        Applies centrally so every registered renderer gets the running blink
        without threading status through each ``render_call``; completed rows
        keep the renderer's own (green/red) marker untouched.
        """
        if self._status not in (ToolExecutionStatus.PENDING, ToolExecutionStatus.RUNNING):
            return call
        if not isinstance(call, Text):
            return call
        plain = call.plain
        if not plain.startswith(f"{TRANSCRIPT_ASSISTANT_MARKER} "):
            return call
        if blink_visible():
            return call
        blinked = call.copy()
        # Off-beat of the blink: hide the marker, keep the column stable.
        blinked.plain = f" {plain[1:]}"
        return blinked

    def _build_context(self, *, width: int = 0) -> ToolRenderContext:
        return ToolRenderContext(
            args=self._state.args or {},
            tool_call_id=self._state.tool_call_id,
            cwd=self._state.cwd,
            execution_started=self._state.execution_started,
            args_complete=self._state.args_complete,
            is_partial=self._state.is_partial,
            has_result=self._state.result is not None,
            expanded=self._state.expanded,
            is_error=self._state.result.is_error if self._state.result else False,
            elapsed_s=self._elapsed_s(),
            width=width if width > 0 else 100,
            state=self._renderer_state,
        )

    def _elapsed_s(self) -> float | None:
        if self._finished_elapsed_s is not None:
            return self._finished_elapsed_s
        if self._state.execution_started:
            return max(0.0, time.monotonic() - self._started_at)
        return None

    def _background_style(self) -> Style | None:
        if self._status in (ToolExecutionStatus.ERROR, ToolExecutionStatus.DENIED):
            return tui_rich_style("tool_error_bg")
        return None

    def _call_fallback(self) -> RenderableType:
        label = self._definition.label or self._state.tool_name
        if self._status == ToolExecutionStatus.SUCCESS:
            glyph = f"{TRANSCRIPT_ASSISTANT_MARKER} "
            glyph_style = tui_rich_style("success") + Style(bold=True)
        elif self._status in (ToolExecutionStatus.ERROR, ToolExecutionStatus.DENIED):
            glyph = "✘ "
            glyph_style = tui_rich_style("error") + Style(bold=True)
        elif self._status == ToolExecutionStatus.CANCELLED:
            glyph = f"{TRANSCRIPT_ACTIVE_MARKER} "
            glyph_style = tui_rich_style("warning") + Style(bold=True)
        else:
            active = blink_visible()
            glyph = f"{TRANSCRIPT_ASSISTANT_MARKER if active else ' '} "
            glyph_style = tui_rich_style("muted") + Style(bold=True)
        header = Text()
        header.append(glyph, style=glyph_style)
        header.append(label, style=Style(bold=True))
        return header

    def _result_fallback(self) -> RenderableType | None:
        result = self._state.result
        if result is None or not result.text:
            return None
        text = result.text
        style = tui_rich_style("error") if result.is_error else tui_rich_style("muted")
        lines = text.splitlines()
        within_limits = len(lines) <= _MAX_RESULT_LINES and len(text) <= _MAX_RESULT_CHARS
        if self._state.expanded or within_limits:
            return Text(text, style=style)

        # Head-tail truncation: keep the start (identifies the command/result)
        # and the end (usually the actionable part), omitting the middle.
        if len(lines) > _MAX_RESULT_LINES:
            head_count = _MAX_RESULT_LINES // 2
            tail_count = _MAX_RESULT_LINES - head_count
            omitted = len(lines) - head_count - tail_count
            head = "\n".join(lines[:head_count])
            tail = "\n".join(lines[-tail_count:])
            notice = f"\n… {omitted} line{'s' if omitted != 1 else ''} omitted …\n"
        else:
            # Few lines but a huge body (e.g. one giant line): split the
            # character budget between the start and the end.
            head = text[: _MAX_RESULT_CHARS // 2]
            tail = text[-(_MAX_RESULT_CHARS // 2) :]
            notice = "\n… middle omitted …\n"
        # Each side gets half the character budget so the combined render can
        # never exceed _MAX_RESULT_CHARS even for line-heavy output.
        body = Text(head[: _MAX_RESULT_CHARS // 2], style=style)
        body.append(notice, style=tui_rich_style("muted") + Style(italic=True))
        body.append(tail[-(_MAX_RESULT_CHARS // 2) :], style=style)
        body.append(
            "\n… output truncated for display; full result preserved in session.",
            style=tui_rich_style("muted") + Style(italic=True),
        )
        return body

    def _has_expandable_payload(self) -> bool:
        """Heuristic: return True when expanding can plausibly reveal more payload."""
        if self._renderer_state.get("__has_expandable_payload__"):
            return True
        result = self._state.result
        if result is None or not result.text:
            return False
        text = result.text
        return len(text) > 240 or text.count("\n") > 4

    def _is_truncatable(self) -> bool:
        """Only show the generic expand hint when a renderer did not show its own."""
        if self._renderer_state.get("__suppress_generic_expand_hint__"):
            return False
        return self._has_expandable_payload()
