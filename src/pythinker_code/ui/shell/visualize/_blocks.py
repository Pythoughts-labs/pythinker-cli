# pyright: reportUnusedClass=false
"""Renderable block components for the streaming agent view.

Each block receives data via method calls and produces Rich renderables.
They have no knowledge of the event loop or prompt_toolkit.
"""

from __future__ import annotations

import json
import random
import time
from collections import Counter, deque
from typing import Any, NamedTuple, cast

import streamingjson  # type: ignore[reportMissingTypeStubs]
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.style import Style
from rich.text import Text

from pythinker_code.soul import format_context_status, format_token_count
from pythinker_code.tools import extract_key_argument
from pythinker_code.tools.display import DiffDisplayBlock
from pythinker_code.ui.shell.components import ToolExecutionComponent
from pythinker_code.ui.shell.components.markdown import (
    PythinkerMarkdown as Markdown,
)
from pythinker_code.ui.shell.components.markdown import (
    markdown_commit_boundary,
)
from pythinker_code.ui.shell.components.render_utils import render_message_response, sanitize_ansi
from pythinker_code.ui.shell.components.report import render_agent_body
from pythinker_code.ui.shell.components.report_update import (
    ReportUpdateComponent,
    looks_like_report_update,
    parse_report_update,
)
from pythinker_code.ui.shell.console import current_console_width
from pythinker_code.ui.shell.glyphs import TRANSCRIPT_ASSISTANT_MARKER, TRANSCRIPT_STATUS_MARKER
from pythinker_code.ui.shell.mcp_status import mcp_startup_header
from pythinker_code.ui.shell.motion import (
    ActivitySnapshot,
    activity_status_line,
    append_streaming_caret,
    reduced_motion_enabled,
)
from pythinker_code.ui.shell.spacing import BLANK_ROW
from pythinker_code.ui.shell.tips import FEATURE_TIPS
from pythinker_code.ui.shell.tool_renderers import (
    ToolResultPayload,
    get_tool_renderer,
)
from pythinker_code.ui.shell.tool_renderers.generic import generic_renderer
from pythinker_code.ui.shell.visualize._activity_tree import ActivityRow, render_activity_tree
from pythinker_code.ui.shell.visualize._worklog import (
    WorkLogState,
    denied_error,
    render_display_blocks,
    render_worklog_entry,
    tool_style,
)
from pythinker_code.ui.theme import tui_rich_style
from pythinker_code.ui.tui_config import is_card_style
from pythinker_code.utils.datetime import format_elapsed
from pythinker_code.utils.rich.columns import BulletColumns
from pythinker_code.utils.trust import strip_untrusted_envelope
from pythinker_code.wire.types import (
    HookResolved,
    HookTriggered,
    MCPStatusSnapshot,
    Notification,
    ProgressNote,
    QuestionAnswered,
    StatusUpdate,
    Suggestion,
    ToolCall,
    ToolCallPart,
    ToolResult,
    ToolReturnValue,
)

_ELLIPSIS = "..."
_THINKING_PREVIEW_LINES = 6
_COMPOSING_PREVIEW_LINES = 12

# Smooth-streaming reveal pacing (composing text only). Deltas arrive bursty;
# instead of revealing each whole chunk at once, a paced reveal cursor advances
# a little per refresh tick so text flows smoothly. "Keep up" pacing: the step
# scales with the backlog so a fast model never lags noticeably behind.
_STREAM_REVEAL_MIN_CELLS = 2
_STREAM_REVEAL_CATCHUP_TICKS = 2
_TOKEN_RATE_WINDOW_S = 1.5
_TOKEN_RATE_MIN_SAMPLES = 3

_smooth_streaming_enabled = False


def set_smooth_streaming(enabled: bool) -> None:
    """Set whether interactive assistant text reveal is paced for smooth streaming."""
    global _smooth_streaming_enabled
    _smooth_streaming_enabled = enabled


def smooth_streaming_enabled() -> bool:
    """Return whether paced streaming is enabled (set at shell startup from config)."""
    return _smooth_streaming_enabled


MAX_SUBAGENT_TOOL_CALLS_TO_SHOW = 4
_MAX_RUNNING_ROWS = 2
_MAX_SUB_OUTPUT_CHARS = 200
_MAX_SUBAGENT_ROLLUP_TOOLS = 6
_MAX_SUBAGENT_CHANGED_FILES = 5

# Background-agent statuses that mean "still running" — the tool call result
# has arrived but the spawned agent has not yet finished.  Blocks with this
# status must stay in the Live area so their spinner keeps animating.
_AGENT_ACTIVE_STATUSES = frozenset({"created", "starting", "running", "awaiting_approval"})
_TODO_TOOL_NAMES = frozenset({"SetTodoList", "TodoWrite"})
_MUTATING_TOOL_NAMES = frozenset(
    {
        "applypatch",
        "edit",
        "replace",
        "strreplacefile",
        "write",
        "writefile",
    }
)


def _parse_tool_result_top_fields(result_text: str) -> dict[str, str]:
    """Parse top-level ``key: value`` lines before nested agent/task sections."""
    top: dict[str, str] = {}
    for line in result_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("- ") or line.startswith("  "):
            break
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key or " " in key:
            continue
        top[key] = value.strip()
    return top


def _is_active_background_agent(tool_name: str, result_text: str) -> bool:
    """Return True when a tool card should stay in the Live area after its result arrives."""
    if tool_name == "Agent":
        values = _parse_tool_result_top_fields(result_text)
        return values.get("kind") == "agent" and values.get("status") in _AGENT_ACTIVE_STATUSES
    if tool_name == "RunAgents":
        values = _parse_tool_result_top_fields(result_text)
        return values.get("mode") == "background" and values.get("tool_status") == "launched"
    return False


def _truncate_to_display_width(line: str, max_width: int) -> str:
    """Truncate *line* so its terminal display width fits within *max_width*.

    Uses ``rich.cells.cell_len`` for CJK-aware column width measurement.
    """
    from rich.cells import cell_len

    if cell_len(line) <= max_width:
        return line
    ellipsis_width = cell_len(_ELLIPSIS)
    budget = max_width - ellipsis_width
    width = 0
    for i, ch in enumerate(line):
        width += cell_len(ch)
        if width > budget:
            return line[:i] + _ELLIPSIS
    return line


def _estimate_tokens(text: str) -> float:
    """Estimate token count for mixed CJK/Latin text.

    Returns a **float** so that callers can accumulate across small chunks
    without per-chunk floor truncation (e.g. a 3-char ASCII chunk would
    yield 0 if truncated to int immediately, but 0.75 as float).

    Heuristics based on common BPE tokenizers (cl100k, o200k):
    - CJK ideographs: ~1.5 tokens per character (often split into 2-byte pieces)
    - Latin / ASCII: ~1 token per 4 characters (words average ~4 chars)
    """
    cjk = 0
    other = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF  # CJK Unified Ideographs
            or 0x3400 <= cp <= 0x4DBF  # CJK Extension A
            or 0xF900 <= cp <= 0xFAFF  # CJK Compatibility Ideographs
            or 0x3000 <= cp <= 0x303F  # CJK Symbols and Punctuation
            or 0xFF00 <= cp <= 0xFFEF  # Fullwidth Forms
        ):
            cjk += 1
        else:
            other += 1
    return cjk * 1.5 + other / 4


def _find_committed_boundary(text: str) -> int | None:
    """Return the character offset up to which *text* can be safely committed."""
    return markdown_commit_boundary(text)


def _tail_lines(text: str, n: int) -> str:
    """Extract the last *n* lines from *text* via reverse scanning (O(n))."""
    pos = len(text)
    for _ in range(n):
        pos = text.rfind("\n", 0, pos)
        if pos == -1:
            return text
    return text[pos + 1 :]


def _advance_by_display_cells(text: str, start: int, cell_budget: int) -> int:
    """Return a character offset advanced by roughly ``cell_budget`` terminal cells."""
    from rich.cells import cell_len

    if cell_budget <= 0:
        return start
    width = 0
    for index in range(start, len(text)):
        width += cell_len(text[index])
        if width >= cell_budget:
            return index + 1
    return len(text)


class _ContentBlock:
    """Streaming content block with incremental markdown commitment.

    For **composing** (``is_think=False``), confirmed markdown blocks are staged
    in the Live compose cache as they become complete. Only the unconfirmed tail
    remains as a plain-text preview in the transient Rich Live area.

    For **thinking** (``is_think=True``), the default behavior is to keep the
    raw reasoning text only for token accounting and never render it.  The
    Live area shows a compact ``Thinking`` label with an animated bullet
    sequence, elapsed time, token count, and a live tokens/second pulse;
    when the block ends, a one-liner ``✻ Cogitated for Xs`` trace is
    committed to history in grey italics.

    When ``show_thinking_stream=True``, the legacy behavior is restored: the
    Live area shows a ``Thinking...`` spinner above a 6-line scrolling preview
    of the raw reasoning text, and the full reasoning markdown is committed
    to history when the block ends.
    """

    def __init__(self, is_think: bool, *, show_thinking_stream: bool = False, paced: bool = False):
        self.is_think = is_think
        self._show_thinking_stream = show_thinking_stream
        # When paced, composing text is revealed gradually by ``reveal_tick``
        # instead of all at once on each delta, for smooth streaming.
        self._paced = paced and not is_think
        self.raw_text = ""
        # Accumulated float estimate — avoids per-chunk int truncation.
        self._token_count: float = 0.0
        self._start_time = time.monotonic()
        # Incremental commitment state (composing only).
        self._committed_len = 0
        # Characters of raw_text revealed for display/commit. Unpaced blocks keep
        # this equal to len(raw_text); paced blocks advance it via reveal_tick().
        self._revealed_len = 0
        self._has_printed_bullet = False
        self._committed_renderables: list[RenderableType] = []
        self._block_width = current_console_width()
        # Sliding window for smooth token-rate display: stores (timestamp, cumulative_tokens)
        # pairs to compute rate over the last ~1.5s. Float cumulative_tokens avoids
        # per-sample truncation.
        self._token_samples: deque[tuple[float, float]] = deque()
        self._report_update: ReportUpdateComponent | None = None

    # -- Public API ----------------------------------------------------------

    @property
    def has_expandable_card(self) -> bool:
        return self._report_update is not None and self._report_update.can_expand

    def toggle_expanded(self) -> None:
        if self._report_update is None:
            return
        self._report_update.toggle_expanded()

    def render_expanded(self) -> RenderableType:
        if self._report_update is None:
            return self.promote_to_scrollback() or Text("")
        was_expanded = self._report_update.expanded
        self._report_update.set_expanded(True)
        try:
            return self._render_report_update_body() or Text("")
        finally:
            self._report_update.set_expanded(was_expanded)

    def append(self, content: str) -> None:
        self.raw_text += content
        self._token_count += _estimate_tokens(content)
        if self._paced:
            # Reveal is paced by reveal_tick() for smooth streaming; just buffer
            # the raw text here. Commit happens as text is revealed.
            return
        # Unpaced (and all thinking blocks): reveal immediately (legacy behavior).
        self._revealed_len = len(self.raw_text)
        # Block boundaries require newlines; skip parse for mid-line chunks.
        if not self.is_think and "\n" in content:
            self._flush_committed()

    def reveal_tick(self) -> bool:
        """Advance the paced reveal cursor toward the buffered text.

        Reveals a slice sized to the backlog (keep-up pacing) so the display
        stays close to a fast model while still animating smoothly, committing
        any completed markdown blocks as they are revealed. Returns ``True`` when
        new text was revealed (the caller should refresh). No-op for unpaced or
        thinking blocks.
        """
        if not self._paced:
            return False
        from rich.cells import cell_len

        hidden = self.raw_text[self._revealed_len :]
        backlog_cells = cell_len(hidden)
        if backlog_cells <= 0:
            return False
        step_cells = max(
            _STREAM_REVEAL_MIN_CELLS,
            -(-backlog_cells // _STREAM_REVEAL_CATCHUP_TICKS),
        )
        if reduced_motion_enabled():
            step_cells = max(step_cells, -(-backlog_cells // 2))
        self._revealed_len = _advance_by_display_cells(
            self.raw_text,
            self._revealed_len,
            step_cells,
        )
        self._flush_committed()
        return True

    def reveal_all(self) -> bool:
        """Reveal all buffered text immediately (block finalize / fast-drain).

        Returns ``True`` if the reveal cursor moved. Commitment of the remaining
        text is left to the finalize path (``compose_final``), matching the
        unpaced behavior so no block is committed twice.
        """
        changed = self._revealed_len < len(self.raw_text)
        self._revealed_len = len(self.raw_text)
        return changed

    def compose(self) -> RenderableType:
        """Render the transient Live area content.

        Thinking mode shows the italic ``Thinking`` label with animated
        bullets; composing mode shows the dots spinner over the
        uncommitted markdown tail.  When ``show_thinking_stream`` is enabled,
        thinking mode falls back to the legacy ``Thinking...`` spinner stacked
        above a 6-line scrolling preview of the raw reasoning text.
        """
        if self.is_think:
            if self._show_thinking_stream:
                return self._compose_thinking_stream()
            return self._compose_thinking()
        return self._compose_composing()

    def compose_final(self) -> RenderableType:
        """Render the remaining uncommitted content when the block ends."""
        if self.is_think:
            if self._show_thinking_stream:
                remaining = self._pending_text()
                if not remaining:
                    return Text("")
                thinking_style = tui_rich_style("thinking_text")
                # Render reasoning as plain muted text — not themed Markdown — so
                # it reads as uniform grey rather than picking up bright heading /
                # purple emphasis colors.
                return BulletColumns(
                    Text(remaining, style=thinking_style + Style(italic=True)),
                    bullet=Text(TRANSCRIPT_ASSISTANT_MARKER, style=thinking_style),
                )
            elapsed_str = format_elapsed(time.monotonic() - self._start_time)
            return Text(
                f"{TRANSCRIPT_STATUS_MARKER} Cogitated for {elapsed_str}",
                style=tui_rich_style("thinking_text") + Style(italic=True),
            )
        remaining = self._pending_text()
        if not remaining:
            return Text("")
        rendered = self._render_body(remaining)
        if self._committed_renderables:
            return Group(*self._committed_renderables, BLANK_ROW, rendered)
        if self._has_printed_bullet:
            return Group(BLANK_ROW, rendered)
        return rendered

    def promote_to_scrollback(self) -> RenderableType | None:
        """Build the full block renderable for one-shot scrollback promotion."""
        report_body = self._render_report_update_body()
        if report_body is not None:
            return report_body
        parts: list[RenderableType] = list(self._committed_renderables)
        remaining = self._pending_text()
        if remaining:
            tail = self._render_body(remaining)
            if parts:
                parts.extend([BLANK_ROW, tail])
            else:
                parts = [tail]
        if not parts:
            return None
        return Group(*parts) if len(parts) > 1 else parts[0]

    def has_active_stream_preview(self) -> bool:
        """Whether live preview animation (caret / paced drain) should keep ticking."""
        if self.is_think:
            return False
        return bool(self._pending_text()) or self._revealed_len < len(self.raw_text)

    def has_pending(self) -> bool:
        """Whether there is uncommitted content to flush."""
        # Thinking blocks always commit a final trace line if any content
        # was received, so gate on raw_text rather than uncommitted length.
        if self.is_think:
            return bool(self.raw_text)
        return bool(self._pending_text())

    # -- Private -------------------------------------------------------------

    def _pending_text(self) -> str:
        return self.raw_text[self._committed_len : self._revealed_len]

    def _wrap_bullet(self, renderable: RenderableType) -> BulletColumns:
        """First call gets the ``•`` bullet; subsequent calls get a space."""
        if self._has_printed_bullet:
            return BulletColumns(renderable, bullet=Text(" "))
        self._has_printed_bullet = True
        return BulletColumns(
            renderable,
            bullet=Text(TRANSCRIPT_ASSISTANT_MARKER, style=tui_rich_style("success")),
        )

    def _wrap_preview_bullet(self, renderable: RenderableType) -> BulletColumns:
        """Wrap transient live preview without mutating scrollback bullet state.

        While the block is still streaming the marker blinks (muted); the
        committed scrollback row gets the solid green marker via
        :meth:`_wrap_bullet`, so "done" reads as a steady green ⏺.
        """
        if self._has_printed_bullet:
            return BulletColumns(renderable, bullet=Text(" "))
        return BulletColumns(
            renderable,
            bullet=Text(
                TRANSCRIPT_ASSISTANT_MARKER,
                style=tui_rich_style("muted") + Style(bold=True),
            ),
        )

    @property
    def has_emitted_to_scrollback(self) -> bool:
        """Whether any part of this block has been printed to scrollback yet."""
        return self._has_printed_bullet

    def _flush_committed(self) -> None:
        """Stage confirmed markdown blocks for the next Live compose pass."""
        if looks_like_report_update(self.raw_text):
            return
        pending = self._pending_text()
        if not pending:
            return
        boundary = _find_committed_boundary(pending)
        if boundary is None:
            return
        committed_text = pending[:boundary]
        if self._committed_renderables:
            self._committed_renderables.append(BLANK_ROW)
        self._committed_renderables.append(self._wrap_bullet(render_agent_body(committed_text)))
        self._committed_len += boundary

    def _render_report_update_body(self) -> RenderableType | None:
        update = parse_report_update(self.raw_text)
        if update is None:
            return None
        if self._report_update is None:
            self._report_update = ReportUpdateComponent(update)
        return self._report_update.render()

    def _render_body(self, text: str) -> RenderableType:
        if looks_like_report_update(text):
            update = parse_report_update(text)
            if update is not None:
                if self._report_update is None:
                    self._report_update = ReportUpdateComponent(update)
                return self._report_update.render()
        return self._wrap_bullet(render_agent_body(text))

    def _activity_snapshot(
        self, label: str, *, label_style: Style | None = None
    ) -> ActivitySnapshot:
        now = time.monotonic()
        elapsed = now - self._start_time
        tokens_int = int(self._token_count)
        token_rate = self._record_token_rate_sample(now)
        return ActivitySnapshot(
            label=label,
            elapsed_s=elapsed,
            tokens=tokens_int,
            token_rate=token_rate,
            label_style=label_style,
            # Composing / Thinking use the morphing filled shape, not braille dots.
            spinner="shape",
        )

    def _record_token_rate_sample(self, now: float) -> int | None:
        """Return a stable recent tokens/sec estimate, or None until enough data exists."""
        self._token_samples.append((now, self._token_count))
        while (
            len(self._token_samples) > 1 and now - self._token_samples[0][0] > _TOKEN_RATE_WINDOW_S
        ):
            self._token_samples.popleft()
        if len(self._token_samples) < _TOKEN_RATE_MIN_SAMPLES:
            return None
        first_t, first_tokens = self._token_samples[0]
        last_t, last_tokens = self._token_samples[-1]
        elapsed = last_t - first_t
        if elapsed <= 0:
            return None
        token_delta = last_tokens - first_tokens
        if token_delta <= 0:
            return None
        rate = int(token_delta / elapsed)
        return rate if rate > 0 else None

    def _compose_composing(self) -> RenderableType:
        spinner = self._compose_spinner()
        pending = self._pending_text()
        committed = list(self._committed_renderables)
        if not pending:
            if committed:
                return Group(*committed, BLANK_ROW, spinner)
            return spinner
        preview = self._build_preview(
            pending,
            max_lines=_COMPOSING_PREVIEW_LINES,
            reserve_caret=True,
        )
        body = self._render_preview_text(preview, caret=True)
        preview_row = self._wrap_preview_bullet(body)
        if committed:
            return Group(*committed, BLANK_ROW, spinner, BLANK_ROW, preview_row)
        return Group(spinner, BLANK_ROW, preview_row)

    def _render_preview_text(self, preview: str, *, caret: bool) -> Text:
        """Plain-text preview path shared by live compose and finalize.

        Leading newline separators (typically ``\\n`` from a markdown commit
        boundary landing at a paragraph separator) are stripped so they do not
        produce a blank row in the transient Live region. The ``BLANK_ROW``
        between the spinner and the preview row already provides the visual gap.
        """
        if not preview:
            return Text("")
        # Strip leading "\n" / "\r\n" — paragraph separator left over from the
        # commit boundary; not whitespace-only lines.
        stripped = preview.lstrip("\r\n")
        if not stripped:
            return Text("")
        body = Text()
        lines = stripped.split("\n")
        for index, line in enumerate(lines):
            if index:
                body.append("\n")
            body.append(sanitize_ansi(line))
        if caret:
            append_streaming_caret(body)
        return body

    def _compose_spinner(self) -> Text:
        return activity_status_line(
            self._activity_snapshot("Composing", label_style=tui_rich_style("thinking_text")),
            width=self._layout_width(),
        )

    def _compose_thinking_stream(self) -> RenderableType:
        """Legacy 'Thinking...' spinner stacked over a 6-line scrolling preview."""
        spinner = self._compose_thinking_spinner()
        pending = self._pending_text()
        if not pending:
            return spinner
        preview = self._build_preview(pending, max_lines=_THINKING_PREVIEW_LINES)
        preview_style = tui_rich_style("thinking_text") + Style(italic=True)
        return Group(
            spinner,
            BLANK_ROW,
            BulletColumns(
                Text(preview, style=preview_style),
                bullet=Text(TRANSCRIPT_ASSISTANT_MARKER, style=preview_style),
            ),
        )

    def _compose_thinking_spinner(self) -> Text:
        return activity_status_line(
            self._activity_snapshot("Thinking", label_style=tui_rich_style("thinking_text")),
            width=self._layout_width(),
        )

    def _layout_width(self) -> int:
        width = current_console_width()
        if width != self._block_width:
            self._block_width = width
        return self._block_width

    def _build_preview(self, text: str, *, max_lines: int, reserve_caret: bool = False) -> str:
        """Tail-trim *text* to ``max_lines`` and clamp it to terminal width."""
        max_width = self._layout_width() - 2
        if reserve_caret:
            max_width = max(1, max_width - 1)
        tail_text = _tail_lines(text, max_lines)
        lines = tail_text.split("\n")
        return "\n".join(_truncate_to_display_width(line, max_width) for line in lines)

    def _compose_thinking(self) -> Text:
        return activity_status_line(
            self._activity_snapshot("Thinking", label_style=tui_rich_style("thinking_text")),
            width=self._layout_width(),
        )


class _ToolCallBlock:
    class FinishedSubCall(NamedTuple):
        call: ToolCall
        result: ToolReturnValue

    def __init__(self, tool_call: ToolCall):
        self._tool_name = tool_call.function.name
        self._tool_call_id = tool_call.id
        self._lexer = streamingjson.Lexer()
        if tool_call.function.arguments is not None:
            self._lexer.append_string(tool_call.function.arguments)

        self._argument = self._extract_worklog_argument(
            tool_call.function.arguments, self._tool_name
        )
        self._result: ToolReturnValue | None = None
        self._subagent_id: str | None = None
        self._subagent_type: str | None = None

        self._ongoing_subagent_tool_calls: dict[str, ToolCall] = {}
        self._last_subagent_tool_call: ToolCall | None = None
        self._n_finished_subagent_tool_calls = 0
        self._finished_subagent_tool_counts: Counter[str] = Counter()
        self._subagent_changed_files: list[str] = []
        self._subagent_changed_file_set: set[str] = set()
        self._finished_subagent_tool_calls = deque[_ToolCallBlock.FinishedSubCall](
            maxlen=MAX_SUBAGENT_TOOL_CALLS_TO_SHOW
        )
        # Pythinker card: lazily built when the tui style is "card" AND a
        # renderer is registered for this tool. Stays None on the legacy
        # ``pythinker`` worklog path so that rendering is bit-for-bit
        # unchanged.
        self._tui_card: ToolExecutionComponent | None = None
        # True once the runtime reports that approval/hooks are complete and
        # the tool body is executing. Before this, streamed tool-call args render
        # as a calm "preparing" row instead of an execution spinner.
        self._execution_started: bool = False
        # Incremental tool output (currently shell stdout/stderr) that should be
        # visible before the final ToolResult arrives.
        self._streamed_output_parts: list[str] = []
        self._streamed_output_had_stderr: bool = False
        # True while the Agent tool result indicates a still-running background
        # agent.  The block stays in _tool_call_blocks (and in the Live area)
        # rather than being flushed to static scrollback, so the spinner keeps
        # animating at the Live refresh rate.
        self._is_background_pending: bool = False
        self._subagent_output_parts: dict[str, list[str]] = {}
        self._subagent_output_had_stderr: dict[str, bool] = {}
        self._subagent_execution_started: set[str] = set()

        self._renderable: RenderableType = self._compose()

    def compose(self) -> RenderableType:
        # Running tool cards and background-pending Agent cards include live
        # status markers. Recompose them on each Live/prompt refresh.
        if self._result is None or self._is_background_pending:
            return self._compose()
        return self._renderable

    @property
    def tool_call_id(self) -> str:
        return self._tool_call_id

    @property
    def is_todo_list(self) -> bool:
        return self._tool_name in _TODO_TOOL_NAMES

    @property
    def finished(self) -> bool:
        return self._result is not None

    @property
    def is_background_pending(self) -> bool:
        return self._is_background_pending

    @property
    def has_expandable_card(self) -> bool:
        return self._tui_card is not None and self._tui_card.can_expand

    def toggle_expanded(self) -> None:
        if self._tui_card is None:
            return
        self._tui_card.toggle_expanded()
        self._renderable = self._compose()

    def render_expanded(self) -> RenderableType:
        """Render the card expanded without changing its remembered collapsed state."""
        if self._tui_card is None:
            return self.compose()
        was_expanded = self._tui_card.expanded
        self._tui_card.set_expanded(True)
        try:
            return self._tui_card.render()
        finally:
            self._tui_card.set_expanded(was_expanded)

    def append_args_part(self, args_part: str):
        if self.finished:
            return
        self._lexer.append_string(args_part)
        # TODO: maybe don't extract detail if it's already stable
        argument = self._extract_worklog_argument(self._lexer.complete_json(), self._tool_name)
        if argument and argument != self._argument:
            self._argument = argument
            self._renderable = self._compose()

    def mark_execution_started(self) -> None:
        # Terminal states are monotonic: a late ToolExecutionStarted (event
        # reordering, duplicate delivery) must not restyle a finished row.
        if self._execution_started or self.finished:
            return
        self._execution_started = True
        if self._tui_card is not None:
            self._tui_card.mark_execution_started()
        self._renderable = self._compose()

    def append_output_part(self, text: str, *, stream: str = "output") -> None:
        if self.finished or not text:
            return
        self._streamed_output_parts.append(text)
        if stream == "stderr":
            self._streamed_output_had_stderr = True
        self.mark_execution_started()
        self._renderable = self._compose()

    def finish(self, result: ToolReturnValue):
        # Monotonic terminal state: the first result wins. A duplicate or
        # replayed ToolResult must not let a failed row become successful —
        # a retry is a new tool call with its own id/row. Background-pending
        # Agent rows are the one exception: their launch result is provisional
        # until the terminal update arrives.
        if self.finished and not self._is_background_pending:
            return
        self._result = result
        result_text = self._card_result_text(result)
        self._is_background_pending = _is_active_background_agent(self._tool_name, result_text)
        self._renderable = self._compose()

    def append_sub_tool_call(self, tool_call: ToolCall):
        self._ongoing_subagent_tool_calls[tool_call.id] = tool_call
        self._last_subagent_tool_call = tool_call
        self._renderable = self._compose()

    def append_sub_tool_call_part(self, tool_call_part: ToolCallPart):
        if self._last_subagent_tool_call is None:
            return
        if not tool_call_part.arguments_part:
            return
        if self._last_subagent_tool_call.function.arguments is None:
            self._last_subagent_tool_call.function.arguments = tool_call_part.arguments_part
        else:
            self._last_subagent_tool_call.function.arguments += tool_call_part.arguments_part
        self._renderable = self._compose()

    def finish_sub_tool_call(self, tool_result: ToolResult):
        self._last_subagent_tool_call = None
        sub_tool_call = self._ongoing_subagent_tool_calls.pop(tool_result.tool_call_id, None)
        if sub_tool_call is None:
            return
        self._subagent_output_parts.pop(tool_result.tool_call_id, None)
        self._subagent_output_had_stderr.pop(tool_result.tool_call_id, None)
        self._subagent_execution_started.discard(tool_result.tool_call_id)
        self._record_finished_subagent_call(sub_tool_call, tool_result.return_value)

        self._finished_subagent_tool_calls.append(
            _ToolCallBlock.FinishedSubCall(
                call=sub_tool_call,
                result=tool_result.return_value,
            )
        )
        self._n_finished_subagent_tool_calls += 1
        self._renderable = self._compose()

    def set_subagent_metadata(self, agent_id: str, subagent_type: str) -> None:
        changed = (self._subagent_id, self._subagent_type) != (agent_id, subagent_type)
        self._subagent_id = agent_id
        self._subagent_type = subagent_type
        if changed:
            self._renderable = self._compose()

    def mark_sub_execution_started(self, tool_call_id: str) -> None:
        if tool_call_id not in self._ongoing_subagent_tool_calls:
            return
        if tool_call_id in self._subagent_execution_started:
            return
        self._subagent_execution_started.add(tool_call_id)
        self._renderable = self._compose()

    def append_sub_output_part(
        self, tool_call_id: str, text: str, *, stream: str = "output"
    ) -> None:
        if tool_call_id not in self._ongoing_subagent_tool_calls:
            return
        if not text:
            return
        parts = self._subagent_output_parts.setdefault(tool_call_id, [])
        parts.append(text)
        if stream == "stderr":
            self._subagent_output_had_stderr[tool_call_id] = True
        combined = "".join(parts)
        if len(combined) > _MAX_SUB_OUTPUT_CHARS:
            self._subagent_output_parts[tool_call_id] = [combined[-_MAX_SUB_OUTPUT_CHARS:]]
        self._renderable = self._compose()

    def _record_finished_subagent_call(
        self, sub_tool_call: ToolCall, result: ToolReturnValue
    ) -> None:
        self._finished_subagent_tool_counts[sub_tool_call.function.name] += 1
        for path in self._changed_paths_from_sub_call(sub_tool_call, result):
            if path in self._subagent_changed_file_set:
                continue
            self._subagent_changed_file_set.add(path)
            self._subagent_changed_files.append(path)

    def _changed_paths_from_sub_call(
        self, sub_tool_call: ToolCall, result: ToolReturnValue
    ) -> list[str]:
        paths: list[str] = [
            block.path
            for block in getattr(result, "display", []) or []
            if isinstance(block, DiffDisplayBlock)
        ]
        if paths:
            return paths

        if sub_tool_call.function.name.lower() not in _MUTATING_TOOL_NAMES:
            return []
        try:
            args = json.loads(sub_tool_call.function.arguments or "{}", strict=False)
        except json.JSONDecodeError:
            return []
        if not isinstance(args, dict):
            return []
        parsed_args = cast(dict[str, Any], args)
        raw_path = parsed_args.get("path") or parsed_args.get("file_path")
        return [str(raw_path)] if raw_path else []

    def _subagent_rollup_children(self) -> list[RenderableType]:
        children: list[RenderableType] = []
        if self._finished_subagent_tool_counts:
            parts: list[str] = []
            for tool_name, count in self._finished_subagent_tool_counts.most_common(
                _MAX_SUBAGENT_ROLLUP_TOOLS
            ):
                label = tool_style(tool_name).label
                parts.append(f"{label} ×{count}" if count > 1 else label)
            hidden = len(self._finished_subagent_tool_counts) - len(parts)
            if hidden > 0:
                parts.append(f"+{hidden} more")
            children.append(
                BulletColumns(
                    Text("tools: " + ", ".join(parts), style=tui_rich_style("muted")),
                    bullet_style=tui_rich_style("muted"),
                )
            )
        if self._subagent_changed_files:
            shown = self._subagent_changed_files[:_MAX_SUBAGENT_CHANGED_FILES]
            suffix = ""
            hidden = len(self._subagent_changed_files) - len(shown)
            if hidden > 0:
                suffix = f", +{hidden} more"
            children.append(
                BulletColumns(
                    Text(
                        "changed: " + ", ".join(shown) + suffix,
                        style=tui_rich_style("muted"),
                    ),
                    bullet_style=tui_rich_style("muted"),
                )
            )
        return children

    def _subagent_activity_children(
        self, style_label: str, *, include_completed_subagent: bool = False
    ) -> list[RenderableType]:
        children: list[RenderableType] = []
        should_show_activity = include_completed_subagent or not (
            style_label == "Subagent" and self._result is not None
        )
        if should_show_activity:
            # Finished sub-tool call rows
            rows: list[ActivityRow] = []
            for sub_call, sub_result in self._finished_subagent_tool_calls:
                argument = extract_key_argument(
                    sub_call.function.arguments or "", sub_call.function.name
                )
                detail = tool_style(sub_call.function.name).label
                if argument:
                    detail = f"{detail} {argument}"
                rows.append(
                    ActivityRow(
                        label="agent",
                        detail=detail,
                        state="failed" if sub_result.is_error else "completed",
                    )
                )

            # Running sub-tool call rows (shown above finished rows)
            ongoing = list(self._ongoing_subagent_tool_calls.values())
            n_hidden_running = max(0, len(ongoing) - _MAX_RUNNING_ROWS)
            visible_running = ongoing[-_MAX_RUNNING_ROWS:]
            running_rows: list[ActivityRow] = []
            for call in visible_running:
                argument = extract_key_argument(call.function.arguments or "", call.function.name)
                detail = tool_style(call.function.name).label
                if argument:
                    detail = f"{detail} {argument}"
                state = "running" if call.id in self._subagent_execution_started else "waiting"
                running_rows.append(ActivityRow(label="agent", detail=detail, state=state))

            if n_hidden_running:
                children.append(
                    Text(
                        f"… {n_hidden_running} more running",
                        style=tui_rich_style("muted") + Style(italic=True),
                    )
                )

            combined_rows = running_rows + rows
            if combined_rows:
                children.append(
                    render_activity_tree(
                        combined_rows,
                        width=current_console_width(),
                        max_rows=len(combined_rows),
                    )
                )

            # Output preview for the most-recent ongoing call that has streamed output
            latest = next(
                (
                    call
                    for call in reversed(list(self._ongoing_subagent_tool_calls.values()))
                    if call.id in self._subagent_output_parts
                ),
                None,
            )
            if latest is not None:
                combined_output = "".join(self._subagent_output_parts[latest.id]).rstrip("\n")
                if combined_output:
                    is_stderr = self._subagent_output_had_stderr.get(latest.id, False)
                    output_style = "error" if is_stderr else "muted"
                    preview = _tail_lines(combined_output, 4)
                    max_line_width = max(1, current_console_width() - 6)
                    for line in preview.splitlines():
                        truncated = _truncate_to_display_width(line, max_line_width)
                        children.append(Text(f"│  {truncated}", style=tui_rich_style(output_style)))
        return children

    def _compose(self) -> RenderableType:
        if is_card_style():
            card_rendered = self._compose_card()
            if card_rendered is not None:
                return card_rendered
        children: list[RenderableType] = []
        if self._subagent_id is not None and self._subagent_type is not None:
            children.append(
                BulletColumns(
                    Text(
                        f"subagent {self._subagent_type} ({self._subagent_id})",
                        style=tui_rich_style("muted"),
                    ),
                    bullet_style=tui_rich_style("muted"),
                )
            )

        style = tool_style(self._tool_name)
        if style.label == "Subagent" and self._result is not None:
            if self._n_finished_subagent_tool_calls:
                summary = Text(
                    f"{self._n_finished_subagent_tool_calls} tool calls completed",
                    style=tui_rich_style("muted"),
                )
                if self._finished_subagent_tool_calls:
                    summary.append(
                        f" · {len(self._finished_subagent_tool_calls)} recent tracked",
                        style=tui_rich_style("muted"),
                    )
                children.append(BulletColumns(summary, bullet_style=tui_rich_style("muted")))
                children.extend(self._subagent_rollup_children())
        elif self._n_finished_subagent_tool_calls > MAX_SUBAGENT_TOOL_CALLS_TO_SHOW:
            n_hidden = self._n_finished_subagent_tool_calls - MAX_SUBAGENT_TOOL_CALLS_TO_SHOW
            children.append(
                BulletColumns(
                    Text(
                        f"{n_hidden} more tool call{'s' if n_hidden > 1 else ''} ...",
                        style=tui_rich_style("muted") + Style(italic=True),
                    ),
                    bullet_style=tui_rich_style("muted"),
                )
            )
        children.extend(
            self._subagent_activity_children(
                style.label,
                include_completed_subagent=style.label == "Subagent" and self._result is not None,
            )
        )

        if self._result is None:
            streamed_output = self._streamed_output_text()
            if streamed_output:
                preview = _tail_lines(streamed_output.rstrip("\n"), 8)
                output_style = "error" if self._streamed_output_had_stderr else "muted"
                children.append(Text(preview, style=tui_rich_style(output_style)))
            return render_worklog_entry(
                label=style.label,
                target=self._argument,
                state=WorkLogState.RUNNING,
                children=children,
            )

        error_message = self._result.message if self._result.is_error else ""
        if self._result.is_error and not error_message:
            error_message = getattr(self._result, "brief", "") or "Tool failed"
        state = (
            WorkLogState.DENIED
            if self._result.is_error and denied_error(error_message)
            else WorkLogState.FAILED
            if self._result.is_error
            else WorkLogState.COMPLETED
        )
        children.extend(
            render_display_blocks(
                getattr(self._result, "display", []) or [], is_error=self._result.is_error
            )
        )
        return render_worklog_entry(
            label=style.label,
            target=self._argument,
            state=state,
            detail=error_message if self._result.is_error else None,
            children=children,
        )

    def _compose_card(self) -> RenderableType | None:
        """Build/update the Pythinker card. Returns None to fall through.

        Renderer resolution: prefer a tool-specific renderer registered
        under ``tool_name``; fall back to the generic renderer so any
        tool gets a Pythinker card under the flag. Returns None only if
        the generic renderer itself is missing (i.e. the built-ins were
        never registered).
        """
        definition = get_tool_renderer(self._tool_name)
        if definition is None:
            definition = generic_renderer()
        if self._tui_card is None:
            self._tui_card = ToolExecutionComponent(
                self._tool_name,
                self._tool_call_id,
                definition=definition,
            )
            if self._execution_started:
                self._tui_card.mark_execution_started()
        raw_args = self._lexer.complete_json() or "{}"
        try:
            parsed = json.loads(raw_args, strict=False)
        except json.JSONDecodeError:
            parsed = {}
        # Args are complete once execution starts; before that we treat
        # complete_json output as best-effort.
        args_complete = self._execution_started or self._result is not None
        if isinstance(parsed, dict):
            args = cast(dict[str, Any], parsed)
            if not args_complete:
                # While args stream in, a key whose value hasn't arrived yet is
                # repaired to null; hide it so renderers show their pending
                # state instead of flashing an <invalid> badge for a frame.
                args = {k: v for k, v in args.items() if v is not None}
            self._tui_card.update_args(args)
        if args_complete:
            self._tui_card.set_args_complete()
        if self._result is not None:
            self._tui_card.set_result(
                ToolResultPayload(
                    text=self._card_result_text(self._result),
                    is_error=self._result.is_error,
                    details=self._card_result_details(self._result),
                ),
                is_partial=self._is_background_pending,
            )
        elif streamed_output := self._streamed_output_text():
            self._tui_card.set_result(
                ToolResultPayload(
                    text=streamed_output,
                    is_error=False,
                    details={
                        "output": streamed_output,
                        "message": "",
                        "display": [],
                        "extras": {"status": "running"},
                    },
                ),
                is_partial=True,
            )
        card_rendered = self._tui_card.render()
        style_label = tool_style(self._tool_name).label
        activity_children: list[RenderableType] = []
        if style_label == "Subagent" and self._result is not None:
            activity_children.extend(self._subagent_rollup_children())
        activity_children.extend(
            self._subagent_activity_children(
                style_label,
                include_completed_subagent=style_label == "Subagent" and self._result is not None,
            )
        )
        if activity_children:
            return Group(card_rendered, BLANK_ROW, *activity_children)
        return card_rendered

    def _streamed_output_text(self) -> str:
        return "".join(self._streamed_output_parts)

    @staticmethod
    def _card_result_details(result: ToolReturnValue) -> dict[str, Any]:
        """Preserve structured tool result data for Blackbox-style cards.

        The legacy card boundary only passed flattened text, which made exact
        file/shell renderers impossible: diffs lost their display blocks,
        shell status lost its machine-readable status, and success messages
        were mixed into stdout.  Keep the text fallback, but also expose the
        safe in-process fields that renderers can choose to consume.
        """
        output = result.output if isinstance(result.output, str) else ""
        # The <untrusted_data> wrapper is model-facing only — strip it here, at the
        # single render boundary, so no TUI renderer ever sees the tags.
        output = strip_untrusted_envelope(output)
        return {
            "output": output,
            "message": result.message,
            "display": getattr(result, "display", []) or [],
            "extras": getattr(result, "extras", None) or {},
        }

    @staticmethod
    def _card_result_text(result: ToolReturnValue) -> str:
        """Flatten a ToolReturnValue to a single text payload for cards.

        Tool renderers expect the *primary content* (file body, command
        output, grep matches) — that lives in ``output`` for Pythinker.
        Fall back to ``message`` (e.g. "Successfully wrote N bytes" from
        WriteFile, where ``output`` is empty) and finally ``brief`` for
        tools that only emit a summary block. Non-string outputs are
        skipped here; specialized renderers should pull richer detail
        from ``ctx.args``.
        """
        # Strip the model-facing <untrusted_data> wrapper for display (same single
        # boundary as _card_result_details).
        clean_output = (
            strip_untrusted_envelope(result.output) if isinstance(result.output, str) else ""
        )
        if result.is_error:
            parts: list[str] = []
            if result.message:
                parts.append(result.message)
            if clean_output:
                parts.append(clean_output)
            if not parts:
                brief = getattr(result, "brief", "") or "Tool failed"
                parts.append(brief)
            return "\n\n".join(parts)
        if clean_output:
            return clean_output
        if result.message:
            return result.message
        return getattr(result, "brief", "") or ""

    @staticmethod
    def _extract_worklog_argument(arguments: str | None, tool_name: str) -> str | None:
        argument = extract_key_argument(arguments or "", tool_name)
        try:
            args = json.loads(arguments or "{}", strict=False)
        except json.JSONDecodeError:
            return argument
        if not isinstance(args, dict):
            return argument
        args = cast(dict[str, Any], args)
        match tool_name:
            case "ReadFile":
                path = args.get("path") or args.get("file_path")
                return str(path) if path else argument
            case _:
                return argument

    @staticmethod
    def _extract_full_url(arguments: str | None, tool_name: str) -> str | None:
        """Extract the full URL from FetchURL tool arguments."""
        if tool_name != "FetchURL" or not arguments:
            return None
        try:
            args = json.loads(arguments, strict=False)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(args, dict):
            url = cast(dict[str, Any], args).get("url")
            if url:
                return str(url)
        return None


class _NotificationBlock:
    _SEVERITY_STYLE = {
        "info": "cyan",
        "success": "green",
        "warning": "yellow",
        "error": "red",
    }

    def __init__(self, notification: Notification):
        self.notification = notification

    def compose(self) -> RenderableType:
        style = self._SEVERITY_STYLE.get(self.notification.severity, "cyan")
        lines: list[RenderableType] = [
            Text(sanitize_ansi(self.notification.title), style=f"bold {style}")
        ]
        body = sanitize_ansi(self.notification.body).strip()
        if body:
            body_lines = body.splitlines()
            preview = "\n".join(body_lines[:2])
            if len(body_lines) > 2:
                preview += "\n..."
            lines.append(Text(preview, style=tui_rich_style("muted")))
        return BulletColumns(Group(*lines), bullet=Text(TRANSCRIPT_ASSISTANT_MARKER, style=style))


class _HookBlock:
    """Compact lifecycle row for configured hooks around prompts and tools."""

    def __init__(self, triggered: HookTriggered) -> None:
        self.event = triggered.event
        self.target = triggered.target
        self.hook_count = triggered.hook_count
        self.resolved: HookResolved | None = None

    def resolve(self, resolved: HookResolved) -> None:
        self.resolved = resolved
        self.event = resolved.event
        self.target = resolved.target

    @property
    def finished(self) -> bool:
        return self.resolved is not None

    def compose(self) -> RenderableType:
        target = self.event if not self.target else f"{self.event} {self.target}"
        detail_parts: list[str] = []
        if self.hook_count > 1:
            detail_parts.append(f"{self.hook_count} hooks")
        if self.resolved is None:
            state = WorkLogState.RUNNING
        elif self.resolved.action == "block":
            state = WorkLogState.FAILED
            reason = " ".join(self.resolved.reason.split())
            if reason:
                detail_parts.append(reason[:120] + ("…" if len(reason) > 120 else ""))
        else:
            state = WorkLogState.COMPLETED
        if self.resolved is not None and self.resolved.duration_ms:
            detail_parts.append(f"{self.resolved.duration_ms}ms")
        return render_worklog_entry(
            label="Hook",
            target=target,
            state=state,
            detail=" · ".join(detail_parts) if detail_parts else None,
            children=self._output_children(),
        )

    def _output_children(self) -> list[RenderableType]:
        if self.resolved is None:
            return []
        children: list[RenderableType] = []
        for output in self.resolved.outputs:
            body = Text()
            stdout = sanitize_ansi(output.stdout).rstrip("\n")
            stderr = sanitize_ansi(output.stderr).rstrip("\n")
            has_both_streams = bool(stdout and stderr)
            if stdout:
                if has_both_streams:
                    body.append("[stdout]\n", style=tui_rich_style("dim"))
                body.append(stdout, style=tui_rich_style("muted"))
            if stderr:
                if body.plain:
                    body.append("\n")
                if has_both_streams:
                    body.append("[stderr]\n", style=tui_rich_style("dim"))
                body.append(stderr, style=tui_rich_style("error"))
            if output.timed_out:
                if body.plain:
                    body.append("\n")
                body.append("hook timed out", style=tui_rich_style("warning"))
            if output.truncated:
                if body.plain:
                    body.append("\n")
                body.append("… hook output truncated", style=tui_rich_style("warning"))
            if body.plain:
                children.append(render_message_response(body))
        return children


class _QuestionAnsweredBlock:
    """Compact transcript row for answers returned from AskUserQuestion."""

    def __init__(self, event: QuestionAnswered) -> None:
        self.event = event

    def compose(self) -> RenderableType:
        title = Text()
        if self.event.dismissed or not self.event.answers:
            title.append(
                "User dismissed the question",
                style=tui_rich_style("muted") + Style(bold=True),
            )
            return BulletColumns(
                title,
                bullet=Text(TRANSCRIPT_ASSISTANT_MARKER, style=tui_rich_style("muted")),
            )

        title.append(
            "User answered Pythinker's questions:",
            style=tui_rich_style("tool_title") + Style(bold=True),
        )
        rows: list[RenderableType] = [title]
        for question, answer in self.event.answers.items():
            row = Text("· ", style=tui_rich_style("muted"))
            row.append(sanitize_ansi(question), style=tui_rich_style("muted"))
            row.append(" → ", style=tui_rich_style("dim"))
            row.append(sanitize_ansi(answer), style=tui_rich_style("info"))
            rows.append(row)
        return BulletColumns(
            Group(*rows),
            bullet=Text(TRANSCRIPT_ASSISTANT_MARKER, style=tui_rich_style("success")),
        )


class _ProgressNoteBlock:
    """Compact checkpoint/progress note for transcript UIs."""

    def __init__(self, event: ProgressNote) -> None:
        self.event = event

    def compose(self) -> RenderableType:
        title = Text(
            sanitize_ansi(self.event.title).strip() or "Progress",
            style=tui_rich_style("tool_title") + Style(bold=True),
        )
        if not self.event.body.strip():
            return BulletColumns(
                title,
                bullet=Text(TRANSCRIPT_ASSISTANT_MARKER, style=tui_rich_style("success")),
            )
        return BulletColumns(
            Group(title, Markdown(self.event.body.strip())),
            bullet=Text(TRANSCRIPT_ASSISTANT_MARKER, style=tui_rich_style("success")),
        )


class _SuggestionBlock:
    """A non-blocking next-action suggestion for transcript UIs."""

    def __init__(self, event: Suggestion) -> None:
        self.event = event

    def compose(self) -> RenderableType:
        label = Text(
            f"Suggested: {sanitize_ansi(self.event.label).strip()}",
            style=tui_rich_style("info"),
        )
        prefill = sanitize_ansi(self.event.prefill).strip()
        if not prefill:
            return BulletColumns(
                label,
                bullet=Text(TRANSCRIPT_ASSISTANT_MARKER, style=tui_rich_style("info")),
            )
        hint = Text(
            f"→ {prefill}  (Alt+S to accept)",
            style=tui_rich_style("muted"),
        )
        return BulletColumns(
            Group(label, hint),
            bullet=Text(TRANSCRIPT_ASSISTANT_MARKER, style=tui_rich_style("info")),
        )


class _StatusBlock:
    def __init__(self, initial: StatusUpdate) -> None:
        self.text = Text("", justify="right")
        self._context_usage: float = 0.0
        self._context_tokens: int = 0
        self._max_context_tokens: int = 0
        self._mcp_status: MCPStatusSnapshot | None = None
        self.update(initial)

    def render(self) -> RenderableType:
        return self.text

    def update(self, status: StatusUpdate) -> None:
        if status.context_usage is not None:
            self._context_usage = status.context_usage
        if status.context_tokens is not None:
            self._context_tokens = status.context_tokens
        if status.max_context_tokens is not None:
            self._max_context_tokens = status.max_context_tokens
        if status.mcp_status is not None:
            self._mcp_status = status.mcp_status
        if status.context_usage is not None or status.mcp_status is not None:
            parts: list[str] = []
            if self._context_usage or self._max_context_tokens:
                parts.append(
                    format_context_status(
                        self._context_usage,
                        self._context_tokens,
                        self._max_context_tokens,
                    )
                )
            if (
                self._mcp_status is not None
                and self._mcp_status.loading
                and (header := mcp_startup_header(self._mcp_status))
            ):
                parts.append(header)
            self.text.plain = "  ".join(parts)


class _CompactionBlock:
    """Animated compaction progress with a time-based estimate.

    The bar fills toward 95% over ``EXPECTED_DURATION_S`` (compaction has no
    real progress signal), then disappears once ``CompactionEnd`` arrives.
    """

    BAR_WIDTH = 40
    EXPECTED_DURATION_S = 60.0
    MAX_ESTIMATED_PROGRESS = 0.95

    TIPS: tuple[str, ...] = FEATURE_TIPS

    def __init__(
        self,
        *,
        context_tokens: int | None = None,
        todos_renderable: RenderableType | None = None,
    ) -> None:
        self._start = time.monotonic()
        self._tip = random.choice(self.TIPS)
        self._context_tokens = context_tokens
        self._todos_renderable = todos_renderable

    def update_context_tokens(self, context_tokens: int | None) -> None:
        """Refresh the token count shown in the compacting title."""
        if context_tokens is not None:
            self._context_tokens = context_tokens

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield from console.render(self._render(), options)

    def _render(self) -> RenderableType:
        elapsed = max(0.0, time.monotonic() - self._start)
        progress = min(elapsed / self.EXPECTED_DURATION_S, self.MAX_ESTIMATED_PROGRESS)
        filled = int(round(progress * self.BAR_WIDTH))
        empty = self.BAR_WIDTH - filled
        pct = int(progress * 100)
        accent = tui_rich_style("accent")
        muted = tui_rich_style("muted")
        subtle = tui_rich_style("dim")
        title_style = accent + Style(italic=True)

        title = Text()
        title.append("· ", style=muted)
        title.append("Compacting conversation…", style=title_style)
        title.append(f" ({format_elapsed(elapsed)}", style=subtle)
        if self._context_tokens is not None:
            title.append(f" · ↑ {format_token_count(self._context_tokens)} tokens", style=subtle)
        title.append(")", style=subtle)

        bar = Text("  ")
        bar.append("▰" * filled, style=tui_rich_style("activity_label"))
        bar.append("▱" * empty, style=muted)
        bar.append(f" {pct}%", style=muted)

        if self._todos_renderable is not None:
            return Group(title, bar, self._todos_renderable)

        tip = Text("  ⎿  ", style=muted)
        tip.append(f"Tip: {self._tip}", style=subtle)
        return Group(title, bar, tip)
