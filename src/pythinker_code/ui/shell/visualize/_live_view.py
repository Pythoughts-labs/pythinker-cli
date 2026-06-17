# pyright: reportPrivateUsage=false, reportUnusedClass=false
"""Base event-consuming view for the streaming agent (Rich Live mode).

``_LiveView`` consumes wire events, updates internal state (content blocks,
tool calls, spinners, approval/question queues), and composes them into a
Rich renderable via ``compose()``.  The Rich ``Live`` context drives refresh.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Literal

from pythinker_core.message import Message
from pythinker_core.tooling import ToolError, ToolOk, ToolReturnValue
from rich import box
from rich.console import Group, RenderableType
from rich.live import Live
from rich.markup import escape as rich_escape
from rich.padding import Padding
from rich.panel import Panel
from rich.style import Style
from rich.text import Text

from pythinker_code.session_recap import build_turn_recap_line
from pythinker_code.soul import format_token_count
from pythinker_code.soul.live_tokens import (
    get_turn_output_tokens,
    snapshot_output_tokens_for_turn,
)
from pythinker_code.tools.display import DiffDisplayBlock, TodoDisplayBlock, TodoDisplayItem
from pythinker_code.ui.shell.components.render_utils import (
    cell_width,
    render_message_response,
    sanitize_ansi,
    truncate_to_width,
)
from pythinker_code.ui.shell.console import console, current_console_width
from pythinker_code.ui.shell.echo import render_user_echo
from pythinker_code.ui.shell.glyphs import TRANSCRIPT_ACTIVE_MARKER, TRANSCRIPT_TOOL_GUTTER
from pythinker_code.ui.shell.keyboard import KeyboardListener, KeyEvent
from pythinker_code.ui.shell.mcp_status import render_mcp_startup_text
from pythinker_code.ui.shell.motion import (
    STREAM_FPS,
    STREAM_FRAME_INTERVAL_S,
    ActivitySnapshot,
    active_marker_frame,
    activity_status_line,
    blink_visible,
    reduced_motion_enabled,
    shimmer_text,
    stream_reveal_interval_s,
)
from pythinker_code.ui.shell.spacing import BLANK_ROW, emit_scrollback_block
from pythinker_code.ui.shell.spinner_words import spinner_message
from pythinker_code.ui.shell.tips import current_tip
from pythinker_code.ui.shell.visualize._approval_panel import (
    ApprovalRequestPanel,
    show_approval_in_pager,
)
from pythinker_code.ui.shell.visualize._blocks import (
    _TOKEN_RATE_MIN_SAMPLES,
    _TOKEN_RATE_WINDOW_S,
    FlushReason,
    Markdown,
    _CompactionBlock,
    _ContentBlock,
    _HookBlock,
    _NotificationBlock,
    _ProgressNoteBlock,
    _QuestionAnsweredBlock,
    _StatusBlock,
    _SuggestionBlock,
    _ToolCallBlock,
    smooth_streaming_enabled,
)
from pythinker_code.ui.shell.visualize._diff_live import DiffLive
from pythinker_code.ui.shell.visualize._question_panel import (
    QuestionRequestPanel,
    prompt_other_input,
    show_question_body_in_pager,
)
from pythinker_code.ui.shell.visualize._worklog import render_worklog_card
from pythinker_code.ui.theme import tui_rich_style
from pythinker_code.utils.aioqueue import Queue, QueueShutDown
from pythinker_code.utils.datetime import format_elapsed
from pythinker_code.utils.logging import logger
from pythinker_code.wire import WireUISide
from pythinker_code.wire.types import (
    ApprovalRequest,
    ApprovalResponse,
    BtwBegin,
    BtwEnd,
    CompactionBegin,
    CompactionEnd,
    ContentPart,
    HookResolved,
    HookTriggered,
    MCPLoadingBegin,
    MCPLoadingEnd,
    Notification,
    PlanDisplay,
    ProgressNote,
    QuestionAnswered,
    QuestionRequest,
    StatusUpdate,
    SteerInput,
    StepBegin,
    StepInterrupted,
    StepRetry,
    SubagentEvent,
    Suggestion,
    TextPart,
    ThinkPart,
    ToolCall,
    ToolCallPart,
    ToolCallRequest,
    ToolExecutionStarted,
    ToolOutputPart,
    ToolResult,
    TurnBegin,
    TurnEnd,
    WireMessage,
)

MAX_LIVE_NOTIFICATIONS = 4
EXTERNAL_MESSAGE_GRACE_S = 0.1
_TRANSITION_DRAIN_MAX_TICKS = 12
_COMPOSE_BATCH_PERIOD_S = 0.01
_COMPOSE_BATCH_MAX_DURATION_S = 1 / 60
_SCROLLED_COMPOSE_FPS = 16
_LIVE_VERTICAL_OVERFLOW: Literal["crop", "ellipsis", "visible"] = "ellipsis"
# Canonical inter-block spacer. The live stream owns the gaps *between* action
# blocks; cards/panels must not add external top/bottom spacing (see spacing.py).
_ACTION_SPACER = BLANK_ROW
# Show the rotating feature tip under the spinner only once a turn has been
# running long enough that a quick turn won't flash it.
_WORKING_TIP_MIN_ELAPSED_S = 4.0
_MAX_PINNED_TODO_ROWS = 5


def _todo_activity_label(label: str) -> str:
    stripped = label.rstrip()
    if stripped.endswith(("…", "...")):
        return stripped
    return f"{stripped}…"


def _append_action_block(
    blocks: list[RenderableType], block: RenderableType, *, leading: bool = False
) -> None:
    """Append a live action block with a one-row gap around stream/status rows."""
    if blocks or leading:
        blocks.append(_ACTION_SPACER)
    blocks.append(block)


def _print_action_block(block: RenderableType) -> None:
    """Commit a completed action block to scrollback with one trailing blank row."""
    emit_scrollback_block(console, block)


def _format_step_retry(retry: StepRetry) -> Text:
    reason = _step_retry_reason(retry)
    wait = format_elapsed(retry.wait_s)
    return Text(
        f"Retrying after {reason} · attempt {retry.next_attempt}/{retry.max_attempts} · {wait}",
        style=tui_rich_style("muted") + Style(italic=True),
    )


def _step_retry_reason(retry: StepRetry) -> str:
    if retry.status_code == 429:
        return "rate limit"
    if retry.status_code is not None and retry.status_code >= 500:
        return "server error"
    if retry.error_type == "APITimeoutError":
        return "timeout"
    if retry.error_type == "APIConnectionError":
        return "connection issue"
    if retry.error_type == "APIEmptyResponseError":
        return "empty response"
    return retry.error_type


@asynccontextmanager
async def _keyboard_listener(
    handler: Callable[[KeyboardListener, KeyEvent], Awaitable[None]],
):
    listener = KeyboardListener()
    await listener.start()

    async def _keyboard():
        while True:
            event = await listener.get()
            await handler(listener, event)

    task = asyncio.create_task(_keyboard())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await listener.stop()


class _LiveView:
    def __init__(
        self,
        initial_status: StatusUpdate,
        cancel_event: asyncio.Event | None = None,
        *,
        show_thinking_stream: bool = False,
        show_turn_recaps: bool = False,
    ):
        self._cancel_event = cancel_event
        self._show_thinking_stream = show_thinking_stream
        self._show_turn_recaps = show_turn_recaps
        # Paced reveal of streamed composing text. Disabled under reduced motion
        # so motion-sensitive users get immediate reveal, not a typewriter.
        self._stream_pacing = smooth_streaming_enabled() and not reduced_motion_enabled()

        self._active_turn_depth = 0
        self._turn_start_time: float | None = None
        self._latest_context_tokens = initial_status.context_tokens
        # Sliding window of (monotonic time, context tokens) samples backing
        # the t/s readout on the working/todo activity lines. Mirrors the
        # per-content-block tracker in _ContentBlock._record_token_rate_sample.
        self._turn_token_samples: deque[tuple[float, int]] = deque()
        self._latest_todos: tuple[TodoDisplayItem, ...] = ()
        self._pinned_todos_visible = True
        self._compaction_block: _CompactionBlock | None = None
        self._latest_mcp_status = initial_status.mcp_status
        self._mcp_loading_spinner: RenderableType | None = None
        self._btw_spinner: RenderableType | None = None
        self._btw_question: str | None = None
        self._recap_user_input = ""
        self._recap_text_parts: list[str] = []
        self._recap_tool_counts: Counter[str] = Counter()
        self._recap_files_modified: set[str] = set()
        self._pending_turn_recap = False

        self._current_content_block: _ContentBlock | None = None
        self._tool_call_blocks: dict[str, _ToolCallBlock] = {}
        self._last_tool_call_block: _ToolCallBlock | None = None
        self._held_tool_search_block: _ToolCallBlock | None = None
        self._completed_expandable_tool_blocks = deque[_ToolCallBlock](maxlen=20)
        self._completed_expandable_content_blocks = deque[_ContentBlock](maxlen=20)
        self._current_step_retry: StepRetry | None = None
        self._approval_request_queue = deque[ApprovalRequest]()
        """
        It is possible that multiple subagents request approvals at the same time,
        in which case we will have to queue them up and show them one by one.
        """
        self._current_approval_request_panel: ApprovalRequestPanel | None = None
        self._question_request_queue = deque[QuestionRequest]()
        self._current_question_panel: QuestionRequestPanel | None = None
        self._notification_blocks = deque[_NotificationBlock]()
        self._live_notification_blocks = deque[_NotificationBlock](maxlen=MAX_LIVE_NOTIFICATIONS)
        self._hook_blocks: dict[tuple[str, str], _HookBlock] = {}
        self._status_block = _StatusBlock(initial_status)

        self._need_recompose = False
        self._dirty = False
        self._force_refresh = False
        self._external_messages: Queue[WireMessage] = Queue()
        self._live: Live | DiffLive | None = None

    def _reset_live_shape(self, live: Live | DiffLive) -> None:
        # Rich doesn't expose a public API to clear Live's cached render height.
        # After leaving the pager, stale height causes cursor restores to jump,
        # so we reset the private _shape to re-anchor the next refresh.
        live_render = getattr(live, "_live_render", None)
        if live_render is not None:
            live_render._shape = None  # type: ignore[reportPrivateUsage]

    async def _drain_external_message_after_wire_shutdown(
        self,
        external_task: asyncio.Task[WireMessage],
    ) -> tuple[WireMessage | None, asyncio.Task[WireMessage]]:
        try:
            msg = await asyncio.wait_for(
                asyncio.shield(external_task),
                timeout=EXTERNAL_MESSAGE_GRACE_S,
            )
        except (TimeoutError, QueueShutDown):
            return None, external_task
        return msg, asyncio.create_task(self._external_messages.get())

    def _stream_compose_interval_s(self) -> float:
        """Adaptive compose cadence: throttle when the live tail is long."""
        block = self._current_content_block
        if block is None or block.is_think:
            return STREAM_FRAME_INTERVAL_S
        if block._committed_renderables or len(block._pending_text()) > 1500:
            return 1 / _SCROLLED_COMPOSE_FPS
        return STREAM_FRAME_INTERVAL_S

    async def _emit_incremental_content_commits(self) -> bool:
        """Emit stable markdown slices to scrollback during an active stream."""
        block = self._current_content_block
        if block is None or block.is_think:
            return False
        committed = block.take_committed_renderables()
        if not committed:
            return False
        for renderable in committed:
            self._emit_incremental_scrollback(renderable)
        await self._after_incremental_scrollback_emitted()
        return True

    async def _after_incremental_scrollback_emitted(self) -> None:
        """Hook for subclasses (prompt_toolkit invalidate) after scrollback emit."""

    def _transition_flush_reason(self, msg: WireMessage) -> FlushReason | None:
        if isinstance(msg, (ToolCall, QuestionAnswered, ProgressNote, Suggestion, PlanDisplay)):
            return FlushReason.TOOL_START
        block = self._current_content_block
        if block is None:
            return None
        if isinstance(msg, ThinkPart) and not block.is_think:
            return FlushReason.TEXT_TO_THINK
        if isinstance(msg, TextPart) and block.is_think:
            return FlushReason.THINK_TO_TEXT
        return None

    async def _drain_content_for_transition(self, reason: FlushReason) -> None:
        if reason not in {
            FlushReason.TOOL_START,
            FlushReason.TEXT_TO_THINK,
            FlushReason.THINK_TO_TEXT,
        }:
            return
        block = self._current_content_block
        if block is None or block.is_think:
            return
        for _ in range(_TRANSITION_DRAIN_MAX_TICKS):
            if self._current_content_block is not block:
                return
            has_more = block.drain_for_transition()
            emitted = await self._emit_incremental_content_commits()
            if emitted or block.has_active_stream_preview():
                self._dirty = True
            if not has_more:
                return
            await asyncio.sleep(stream_reveal_interval_s())

    async def _extend_wire_batch(
        self,
        wire: WireUISide,
        wire_task: asyncio.Task[WireMessage],
        messages: list[WireMessage],
    ) -> asyncio.Task[WireMessage]:
        """Coalesce bursty wire delivery before dispatch (short batch window)."""
        deadline = time.monotonic() + _COMPOSE_BATCH_MAX_DURATION_S
        while time.monotonic() < deadline:
            if wire_task.done():
                messages.append(wire_task.result())
                wire_task = asyncio.create_task(wire.receive())
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _ = await asyncio.wait(
                [wire_task],
                timeout=min(_COMPOSE_BATCH_PERIOD_S, remaining),
            )
            if wire_task in done:
                messages.append(wire_task.result())
                wire_task = asyncio.create_task(wire.receive())
        return wire_task

    async def _frame_refresh_loop(self, live: Live | DiffLive) -> None:
        """Coalesce wire-driven repaints to the streaming frame budget."""
        try:
            while True:
                await asyncio.sleep(self._stream_compose_interval_s())
                advanced = self.advance_stream_reveal()
                emitted = await self._emit_incremental_content_commits()
                needs_animation = self._streaming_needs_animation_frame()
                if advanced or emitted or needs_animation:
                    self._dirty = True
                if not self._dirty and not self._force_refresh:
                    continue
                live.update(self.compose(), refresh=self._force_refresh)
                self._dirty = False
                self._force_refresh = False
                self._need_recompose = False
        except asyncio.CancelledError:
            pass

    def _streaming_needs_animation_frame(self) -> bool:
        block = self._current_content_block
        if block is None:
            return False
        return block.has_active_stream_preview()

    def _flush_live_refresh(self, live: Live | DiffLive, *, force: bool = False) -> None:
        """Paint immediately; use for user-initiated repaints only."""
        if not force and not self._dirty and not self._force_refresh:
            return
        live.update(self.compose(), refresh=True)
        self._dirty = False
        self._force_refresh = False
        self._need_recompose = False

    def _open_live_region(self) -> Live | DiffLive:
        """Return a live-region driver: diff-based on terminals, Rich Live otherwise."""
        if console.is_terminal:
            return DiffLive(
                console=console,
                transient=True,
                get_renderable=lambda: self.compose(),
            )
        return Live(
            self.compose(),
            console=console,
            refresh_per_second=STREAM_FPS,
            transient=True,
            vertical_overflow=_LIVE_VERTICAL_OVERFLOW,
        )

    async def visualize_loop(self, wire: WireUISide):
        live = self._open_live_region()
        with live:
            if isinstance(live, DiffLive):
                live.refresh()
            self._live = live

            async def keyboard_handler(listener: KeyboardListener, event: KeyEvent) -> None:
                # Handle Ctrl+O specially - pause Live only while the pager is active.
                # Ctrl+E remains accepted as a legacy alias.
                if event in (KeyEvent.CTRL_O, KeyEvent.CTRL_E):
                    if self._has_expandable_modal_panel() or (
                        self._expandable_tool_card() is None
                        and self._expandable_content_block() is None
                        and (
                            self._completed_expandable_tool_card() is not None
                            or self._completed_expandable_content_block() is not None
                        )
                    ):
                        from pythinker_code.telemetry import track

                        track("shortcut_expand")
                        await listener.pause()
                        live.stop()
                        try:
                            self._show_expandable_panel_content()
                        finally:
                            # Reset live render shape so the next refresh re-anchors cleanly.
                            # Resume the listener even if restarting Live raises, so the
                            # keyboard never deadlocks paused.
                            try:
                                self._reset_live_shape(live)
                                live.start()
                                live.update(self.compose(), refresh=True)
                            finally:
                                await listener.resume()
                    elif self._toggle_latest_tool_card():
                        live.update(self.compose(), refresh=True)
                    return

                # Handle ENTER/SPACE on question panel when "Other" is selected
                if self._should_prompt_question_other_for_key(event):
                    panel = self._current_question_panel
                    assert panel is not None
                    question_text = panel.current_question_text
                    await listener.pause()
                    live.stop()
                    try:
                        text = await prompt_other_input(question_text)
                    finally:
                        # Always resume the listener, even if restarting Live raises.
                        try:
                            self._reset_live_shape(live)
                            live.start()
                        finally:
                            await listener.resume()

                    self._submit_question_other_text(text)
                    live.update(self.compose(), refresh=True)
                    return

                self.dispatch_keyboard_event(event)
                if self._need_recompose:
                    live.update(self.compose(), refresh=True)
                    self._need_recompose = False

            async with _keyboard_listener(keyboard_handler):
                wire_task = asyncio.create_task(wire.receive())
                external_task = asyncio.create_task(self._external_messages.get())
                frame_task = asyncio.create_task(self._frame_refresh_loop(live))
                try:
                    while True:
                        from_external = False
                        try:
                            done, _ = await asyncio.wait(
                                [wire_task, external_task, frame_task],
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if frame_task in done:
                                # The frame loop is expected to run until it is
                                # cancelled at shutdown. If it finished while the
                                # main loop is still live it raised — surface that
                                # instead of silently freezing the live view.
                                frame_task.result()
                                raise RuntimeError(
                                    "live-view frame refresh loop exited unexpectedly"
                                )
                            if wire_task in done:
                                msg = wire_task.result()
                                wire_task = asyncio.create_task(wire.receive())
                                messages = [msg]
                                wire_task = await self._extend_wire_batch(wire, wire_task, messages)
                            else:
                                msg = external_task.result()
                                external_task = asyncio.create_task(self._external_messages.get())
                                from_external = True
                                messages = [msg]
                        except QueueShutDown:
                            (
                                msg,
                                external_task,
                            ) = await self._drain_external_message_after_wire_shutdown(
                                external_task
                            )
                            if msg is not None:
                                if reason := self._transition_flush_reason(msg):
                                    await self._drain_content_for_transition(reason)
                                self.dispatch_wire_message(msg)
                                self._flush_live_refresh(live, force=True)
                                continue
                            self.cleanup(is_interrupt=False)
                            self._flush_live_refresh(live, force=True)
                            break

                        interrupted = False
                        for msg in messages:
                            if isinstance(msg, StepInterrupted):
                                self.cleanup(is_interrupt=True)
                                self._flush_live_refresh(live, force=True)
                                interrupted = True
                                break

                            if reason := self._transition_flush_reason(msg):
                                await self._drain_content_for_transition(reason)
                            self.dispatch_wire_message(msg)
                            if from_external:
                                # External (out-of-band) messages — approval requests,
                                # steer input — are interactive and must paint at once
                                # rather than wait for the streaming frame budget.
                                self._flush_live_refresh(live, force=True)
                        if interrupted:
                            break
                finally:
                    frame_task.cancel()
                    wire_task.cancel()
                    external_task.cancel()
                    self._external_messages.shutdown(immediate=True)
                    with suppress(asyncio.CancelledError, QueueShutDown):
                        _ = await frame_task
                    with suppress(asyncio.CancelledError, QueueShutDown):
                        _ = await wire_task
                    with suppress(asyncio.CancelledError, QueueShutDown):
                        _ = await external_task
            self._live = None

    def refresh_soon(self, force: bool = False) -> None:
        self._dirty = True
        self._need_recompose = True
        if force:
            self._force_refresh = True

    def advance_stream_reveal(self) -> bool:
        """Advance paced reveal of the active composing block by one tick.

        Returns ``True`` when new text was revealed so the caller can refresh.
        No-op unless a paced composing block is active.
        """
        block = self._current_content_block
        if block is None:
            return False
        return block.reveal_tick()

    def _on_question_panel_state_changed(self) -> None:
        """Hook for subclasses to react when question panel visibility changes."""
        return None

    def enqueue_external_message(self, msg: WireMessage) -> None:
        try:
            self._external_messages.put_nowait(msg)
        except QueueShutDown:
            logger.debug("Ignoring external wire message after live view shutdown: {msg}", msg=msg)

    def has_expandable_panel(self) -> bool:
        return (
            self._has_expandable_modal_panel()
            or self._expandable_tool_card() is not None
            or self._expandable_content_block() is not None
            or self._completed_expandable_tool_card() is not None
            or self._completed_expandable_content_block() is not None
        )

    def _has_expandable_modal_panel(self) -> bool:
        return (
            self._expandable_approval_panel() is not None
            or self._expandable_question_panel() is not None
        )

    def _expandable_approval_panel(self) -> ApprovalRequestPanel | None:
        panel = self._current_approval_request_panel
        if panel is not None and panel.has_expandable_content:
            return panel
        return None

    def _expandable_question_panel(self) -> QuestionRequestPanel | None:
        panel = self._current_question_panel
        if panel is not None and panel.has_expandable_content:
            return panel
        return None

    def _expandable_tool_card(self) -> _ToolCallBlock | None:
        candidates = list(self._tool_call_blocks.values())
        if self._last_tool_call_block in candidates:
            candidates.remove(self._last_tool_call_block)
            candidates.append(self._last_tool_call_block)
        for block in reversed(candidates):
            if block.has_expandable_card:
                return block
        return None

    def _completed_expandable_tool_card(self) -> _ToolCallBlock | None:
        for block in reversed(self._completed_expandable_tool_blocks):
            if block.has_expandable_card:
                return block
        return None

    def _expandable_content_block(self) -> _ContentBlock | None:
        block = self._current_content_block
        if block is not None and block.has_expandable_card:
            return block
        return None

    def _completed_expandable_content_block(self) -> _ContentBlock | None:
        for block in reversed(self._completed_expandable_content_blocks):
            if block.has_expandable_card:
                return block
        return None

    def _toggle_latest_tool_card(self) -> bool:
        block = self._expandable_content_block()
        if block is not None:
            block.toggle_expanded()
            self.refresh_soon()
            return True
        block = self._expandable_tool_card()
        if block is None:
            return False
        block.toggle_expanded()
        self.refresh_soon()
        return True

    def _show_expandable_panel_content(self) -> bool:
        if approval_panel := self._expandable_approval_panel():
            show_approval_in_pager(approval_panel)
            return True
        if question_panel := self._expandable_question_panel():
            show_question_body_in_pager(question_panel)
            return True
        if block := self._completed_expandable_content_block():
            with console.screen(), console.pager(styles=True):
                console.print(block.render_expanded())
            return True
        if block := self._completed_expandable_tool_card():
            with console.screen(), console.pager(styles=True):
                console.print(block.render_expanded())
            return True
        return False

    def _should_prompt_question_other_for_key(self, key: KeyEvent) -> bool:
        panel = self._current_question_panel
        if panel is None or not panel.should_prompt_other_input():
            return False
        return key == KeyEvent.ENTER or (key == KeyEvent.SPACE and not panel.is_multi_select)

    def _submit_question_other_text(self, text: str) -> None:
        panel = self._current_question_panel
        if panel is None:
            return

        all_done = panel.submit_other(text)
        if all_done:
            panel.request.resolve(panel.get_answers())
            self.show_next_question_request()
        self.refresh_soon()

    # -- Composable rendering --------------------------------------------------

    def compose_interactive_panels(self) -> list[RenderableType]:
        """Approval and question panels — interactive overlays.

        In Non-interactive mode (Rich Live), these are rendered by ``compose()``.
        In Interactive mode (prompt_toolkit), these are rendered by modal
        delegates in Layer 2, so ``render_agent_status()`` skips them to
        avoid double-rendering.
        """
        blocks: list[RenderableType] = []
        if self._current_approval_request_panel:
            blocks.append(self._current_approval_request_panel.render())
        if self._current_question_panel:
            blocks.append(self._current_question_panel.render())
        return blocks

    def compose_agent_output(
        self,
        *,
        include_working_indicator: bool = True,
        include_content_activity: bool = True,
    ) -> list[RenderableType]:
        """Spinners, content blocks, tool calls, notifications.

        Pure agent streaming status — no interactive overlays.
        Always safe to render regardless of modal state.

        ``include_working_indicator`` controls whether the trailing verb
        spinner is emitted. ``include_content_activity`` controls the composing
        activity row inside the active content block. The interactive prompt
        sets both ``False`` so it can pin the active spinner *below* a clipped
        agent stream (see ``render_pinned_status_tail``), keeping it visible
        instead of letting the clip hint cover it or split the body from the
        mutable preview.

        Display priority (highest → lowest):
          1. MCP loading spinner (connecting to servers)
          2. Compaction spinner (context compaction in progress)
          3. Content blocks + tool call blocks (streaming output)
          4. Moon spinner fallback (turn active but nothing else visible)
        The btw spinner is always shown (side-channel, not mutually exclusive).
        """
        blocks: list[RenderableType] = []
        if self._btw_spinner is not None:
            _append_action_block(blocks, self._btw_spinner)
        if self._mcp_loading_spinner is not None:
            _append_action_block(blocks, self._mcp_loading_spinner)
        elif self._compaction_block is not None:
            # Compaction starts after prior assistant/tool text has usually just
            # committed to scrollback. Keep the same one-row seam the rest of
            # the live stream uses instead of letting it touch the previous line.
            _append_action_block(blocks, self._compaction_block, leading=True)
        else:
            current_step_retry = getattr(self, "_current_step_retry", None)
            if current_step_retry is not None:
                _append_action_block(blocks, _format_step_retry(current_step_retry), leading=True)
            if self._current_content_block is not None:
                _append_action_block(
                    blocks,
                    self._current_content_block.compose(include_activity=include_content_activity),
                    leading=True,
                )
            # When an approval panel is on-screen for a specific tool call, the
            # panel already previews the same command/diff that the pending tool
            # card would show. Suppress the matching card to avoid the duplicate.
            suppressed_tool_call_id: str | None = None
            if self._current_approval_request_panel is not None:
                suppressed_tool_call_id = self._current_approval_request_panel.request.tool_call_id
            for tool_call in list(self._tool_call_blocks.values()):
                if (
                    suppressed_tool_call_id is not None
                    and tool_call.tool_call_id == suppressed_tool_call_id
                ):
                    continue
                if tool_call.is_todo_list:
                    # Todo updates are pinned under the verb spinner; don't also
                    # render a floating todo tool card above the stream.
                    continue
                # leading=True gives the first live tool card a blank row above
                # it too, so a still-running agent is separated from a finished
                # one already committed to scrollback.
                _append_action_block(blocks, tool_call.compose(), leading=True)
            for hook_block in getattr(self, "_hook_blocks", {}).values():
                _append_action_block(blocks, hook_block.compose(), leading=True)
            if (
                include_working_indicator
                and self._active_turn_depth > 0
                and self._current_question_panel is None
                and self._current_approval_request_panel is None
            ):
                # Keep a stable activity indicator visible for the whole turn —
                # even while content or tool cards are already on-screen, and even
                # while a foreground tool runs. The agent is still working the
                # turn, so the shimmer verb spinner stays up as the liveness signal
                # (the same way it persists while thinking). When a todo is
                # in-progress, _working_indicator() swaps the verb for the todo
                # title instead.
                _append_action_block(blocks, self._working_indicator(), leading=True)
        for notification in list(self._live_notification_blocks):
            _append_action_block(blocks, notification.compose())
        return blocks

    def _track_recap_modified_files(self, result: ToolResult) -> None:
        """Record files a tool reported changing, for the turn-recap deltas."""
        for block in getattr(result.return_value, "display", []) or []:
            if isinstance(block, DiffDisplayBlock) and block.path:
                self._recap_files_modified.add(block.path)

    def _build_turn_recap_block(self) -> RenderableType | None:
        if not self._show_turn_recaps:
            return None
        # TextPart values are streaming deltas, not paragraphs. Concatenate them
        # directly; joining with spaces/newlines can split BPE-sized chunks into
        # unreadable recap text such as `. py think er /re ports ...`.
        assistant_text = "".join(self._recap_text_parts).strip()
        line = build_turn_recap_line(
            request=self._recap_user_input,
            assistant_text=assistant_text,
            step_count=sum(self._recap_tool_counts.values()) or None,
            files_changed=len(self._recap_files_modified),
        )
        if not line:
            return None
        return Padding(
            Markdown(sanitize_ansi(line), style=tui_rich_style("muted") + Style(italic=True)),
            (0, 1),
        )

    def _print_turn_recap(self) -> None:
        block = self._build_turn_recap_block()
        if block is None:
            return
        console.print()
        # Pad the recap to the same horizontal inset as message/tool cards so
        # it stays aligned with the transcript instead of spanning edge-to-edge.
        console.print(block)
        console.print()

    def _working_indicator(self) -> RenderableType:
        now = time.monotonic()
        elapsed = 0.0 if self._turn_start_time is None else now - self._turn_start_time
        width = current_console_width()
        active_todo_title = (
            self._active_todo_title() if getattr(self, "_pinned_todos_visible", True) else None
        )
        label = active_todo_title or spinner_message(now)
        todo_block = self._pinned_todo_block(
            width=width,
            elapsed_s=elapsed,
            hide_active=False,
        )
        if active_todo_title is not None or todo_block is not None:
            line = self._todo_activity_line(
                label,
                elapsed_s=elapsed,
                width=width,
                shimmer_label=active_todo_title is None,
            )
            if todo_block is not None:
                return Group(line, todo_block)
            return line

        line = activity_status_line(
            ActivitySnapshot(
                label=spinner_message(now),
                elapsed_s=elapsed,
                tokens=get_turn_output_tokens(),
                token_rate=self._turn_token_rate(now),
            ),
            width=width,
        )
        # During longer waits, surface a rotating CLI-feature tip under the verb.
        if elapsed < _WORKING_TIP_MIN_ELAPSED_S:
            return line
        tip_content = Text("Tip: ", style=tui_rich_style("dim"))
        tip_content.append(current_tip(now), style=tui_rich_style("dim"))
        return Group(line, render_message_response(tip_content))

    def _turn_token_rate(self, now: float) -> int | None:
        """Stable recent tokens/sec for the running turn, or None until known.

        Samples the session-wide output-token counter at refresh cadence and
        derives the rate over a short sliding window, so the readout tracks
        live throughput instead of a whole-turn average.
        """
        tokens = get_turn_output_tokens()
        # Lazy init: subclasses used in tests don't always run __init__.
        samples = getattr(self, "_turn_token_samples", None)
        if samples is None:
            samples = self._turn_token_samples = deque()
        samples.append((now, tokens))
        while len(samples) > 1 and now - samples[0][0] > _TOKEN_RATE_WINDOW_S:
            samples.popleft()
        if len(samples) < _TOKEN_RATE_MIN_SAMPLES:
            return None
        first_t, first_tokens = samples[0]
        last_t, last_tokens = samples[-1]
        elapsed = last_t - first_t
        token_delta = last_tokens - first_tokens
        if elapsed <= 0 or token_delta <= 0:
            return None
        rate = int(token_delta / elapsed)
        return rate if rate > 0 else None

    def _todo_activity_line(
        self, label: str, *, elapsed_s: float, width: int, shimmer_label: bool = True
    ) -> Text:
        label = _todo_activity_label(label)
        parts = [format_elapsed(elapsed_s)]
        turn_tokens = get_turn_output_tokens()
        if turn_tokens:
            parts.append(f"↓ {format_token_count(turn_tokens)} tokens")
        rate = self._turn_token_rate(time.monotonic())
        if rate:
            parts.append(f"{rate} t/s")
        metadata = f"({', '.join(parts)})"
        prefix = f"{active_marker_frame(elapsed_s)} "
        suffix = f" {metadata}"
        label_width = max(1, width - cell_width(prefix) - cell_width(suffix))

        label_text = truncate_to_width(label, label_width)
        line = Text(prefix, style=tui_rich_style("activity_spinner"))
        if shimmer_label:
            line.append_text(
                shimmer_text(
                    label_text,
                    elapsed_s,
                    reduced_motion=reduced_motion_enabled(),
                )
            )
        else:
            # The active-todo title matches the coral activity color (same as
            # the in-progress ■ boxes) so everything "working" reads as one
            # family, distinct from neutral body text.
            line.append(label_text, style=tui_rich_style("activity_verb") + Style(bold=True))
        line.append(suffix, style=tui_rich_style("muted"))
        return line

    def _active_todo_title(self) -> str | None:
        for todo in getattr(self, "_latest_todos", ()):
            if todo.status == "in_progress" and todo.title.strip():
                return todo.title.strip()
        return None

    def _pinned_todo_block(
        self, *, width: int, elapsed_s: float | None = None, hide_active: bool = True
    ) -> RenderableType | None:
        """Render the single todo source of truth under the pinned activity line."""
        if not getattr(self, "_pinned_todos_visible", True):
            return None
        latest_todos = tuple(
            todo
            for todo in getattr(self, "_latest_todos", ())
            if todo.status in ("done", "in_progress", "pending", "cancelled") and todo.title.strip()
        )
        active_todo = next((todo for todo in latest_todos if todo.status == "in_progress"), None)
        status_order = {"in_progress": 0, "pending": 1, "cancelled": 2, "done": 3}
        ordered_todos = tuple(
            sorted(
                enumerate(latest_todos),
                key=lambda item: (status_order[item[1].status], item[0]),
            )
        )
        todos = tuple(
            todo for _, todo in ordered_todos if not (hide_active and todo is active_todo)
        )
        if not todos:
            return None

        visible = todos[:_MAX_PINNED_TODO_ROWS]
        hidden = todos[_MAX_PINNED_TODO_ROWS:]

        rows: list[Text] = []
        if elapsed_s is None:
            now = time.monotonic()
            elapsed_s = 0.0 if self._turn_start_time is None else now - self._turn_start_time
        for index, todo in enumerate(visible):
            rows.append(
                self._pinned_todo_row(
                    todo,
                    is_first=index == 0,
                    width=width,
                    elapsed_s=elapsed_s,
                )
            )

        if hidden:
            hidden_pending = sum(1 for todo in hidden if todo.status == "pending")
            hidden_done = sum(1 for todo in hidden if todo.status == "done")
            label = (
                "pending"
                if hidden_pending == len(hidden)
                else "completed"
                if hidden_done == len(hidden)
                else "more"
            )
            row = Text("      … ", style=tui_rich_style("muted"))
            row.append(f"+{len(hidden)} {label}", style=tui_rich_style("muted"))
            rows.append(row)
        return Group(*rows)

    def _pinned_todo_row(
        self,
        todo: TodoDisplayItem,
        *,
        is_first: bool,
        width: int,
        elapsed_s: float | None = None,
    ) -> Text:
        if todo.status == "done":
            icon = "✓"
            icon_token = "success"
            title_style = tui_rich_style("muted") + Style(strike=True)
        elif todo.status == "cancelled":
            icon = "✕"
            icon_token = "muted"
            title_style = tui_rich_style("muted") + Style(strike=True)
        elif todo.status == "in_progress":
            icon = "■"
            # Reference design: the box carries the coral activity accent
            # while the title reads bold in the primary text color.
            icon_token = "activity_verb"
            title_style = tui_rich_style("text") + Style(bold=True)
        else:
            icon = "□"
            icon_token = "muted"
            title_style = tui_rich_style("muted")
        # The first row carries the ``⎿`` gutter; continuation rows omit it but
        # keep the same checkbox/title columns for a stable todo list alignment.
        prefix = f"  {TRANSCRIPT_TOOL_GUTTER}    " if is_first else "       "
        title_budget = max(1, width - cell_width(prefix) - 2)
        title = truncate_to_width(todo.title.strip(), title_budget)
        # No base style on the row: a muted base would bleed through spans
        # that set no color of their own (the bold running-task title must
        # fall back to the terminal default white, not muted grey).
        row = Text()
        row.append(prefix, style=tui_rich_style("muted"))
        row.append(icon, style=tui_rich_style(icon_token))
        row.append(" ")
        row.append(title, style=title_style)
        return row

    def toggle_pinned_todos(self) -> bool:
        """Toggle visibility of the pinned todo list and return the new state."""
        self._pinned_todos_visible = not getattr(self, "_pinned_todos_visible", True)
        self.refresh_soon()
        return self._pinned_todos_visible

    def compose(self, *, include_status: bool = True) -> RenderableType:
        """Compose the full live view display content.

        Combines interactive panels (approval/question) and agent output.
        Panels are rendered first so they remain visible at the top of the
        terminal even when tool-call output is long enough to push content
        beyond the visible area.

        In Interactive mode, prefer ``compose_agent_output()`` for Layer 1
        rendering to avoid double-rendering panels that modal delegates
        already handle in Layer 2.
        """
        blocks: list[RenderableType] = []
        blocks.extend(self.compose_interactive_panels())
        agent_blocks = self.compose_agent_output()
        blocks.extend(agent_blocks)
        if include_status:
            # One blank row under the spinner verb (the agent stream's tail)
            # before the status line — but only when the status line has content,
            # since an empty status row already renders as a blank line.
            if agent_blocks and self._status_block.text.plain.strip():
                blocks.append(_ACTION_SPACER)
            blocks.append(self._status_block.render())
        return Group(*blocks)

    def _begin_turn_token_window(self) -> None:
        """Start a fresh per-turn token-rate window: reset the baseline snapshot
        and drop prior-turn samples so the t/s rate can't be skewed by stale data."""
        snapshot_output_tokens_for_turn()
        self._turn_token_samples.clear()

    def dispatch_wire_message(self, msg: WireMessage) -> None:
        """Dispatch the Wire message to UI components."""
        assert not isinstance(msg, StepInterrupted)  # handled in visualize_loop

        if isinstance(msg, StepBegin):
            self.cleanup(is_interrupt=False)
            self._mcp_loading_spinner = None
            # Defensive: if StepBegin arrives without a preceding TurnBegin
            # (e.g. during replay), ensure the turn is considered active.
            if self._active_turn_depth == 0:
                self._active_turn_depth = 1
                self._turn_start_time = time.monotonic()
                self._begin_turn_token_window()
            self.refresh_soon()
            return
        if isinstance(msg, StepRetry):
            self.discard_retry_attempt(msg)
            self.refresh_soon()
            return

        match msg:
            case TurnBegin(user_input=user_input):
                if self._active_turn_depth == 0:
                    self._turn_start_time = time.monotonic()
                    self._begin_turn_token_window()
                    self._recap_user_input = (
                        user_input
                        if isinstance(user_input, str)
                        else Message(role="user", content=user_input).extract_text(" ")
                    )
                    self._recap_text_parts.clear()
                    self._recap_tool_counts.clear()
                    self._recap_files_modified.clear()
                    self._pending_turn_recap = False
                self._active_turn_depth += 1
                self.flush_content(FlushReason.TURN_END)
                self.refresh_soon()
            case SteerInput(user_input=user_input):
                self.cleanup(is_interrupt=False)
                content: list[ContentPart]
                if isinstance(user_input, list):
                    content = list(user_input)
                else:
                    content = [TextPart(text=user_input)]
                self._emit_steer_echo(render_user_echo(Message(role="user", content=content)))
            case TurnEnd():
                self._active_turn_depth = max(0, self._active_turn_depth - 1)
                if self._active_turn_depth == 0:
                    self._turn_start_time = None
                    self._pending_turn_recap = True
            case CompactionBegin():
                self._compaction_block = _CompactionBlock(
                    context_tokens=self._latest_context_tokens,
                    todos_renderable=self._pinned_todo_block(
                        width=current_console_width(), hide_active=False, elapsed_s=0.0
                    ),
                )
                self.refresh_soon()
            case CompactionEnd():
                self._compaction_block = None
                self.refresh_soon()
            case MCPLoadingBegin():
                if self._latest_mcp_status is not None:
                    self._mcp_loading_spinner = render_mcp_startup_text(self._latest_mcp_status)
                else:
                    line = Text(f"{TRANSCRIPT_ACTIVE_MARKER} ", style=tui_rich_style("muted"))
                    line.append("Starting MCP servers", style=tui_rich_style("muted"))
                    self._mcp_loading_spinner = line
                self.refresh_soon()
            case MCPLoadingEnd():
                self._mcp_loading_spinner = None
                self.refresh_soon()
            case BtwBegin(question=question):
                truncated = (question[:40] + "...") if len(question) > 40 else question
                self._btw_question = question
                glyph = TRANSCRIPT_ACTIVE_MARKER if blink_visible() else " "
                line = Text(f"{glyph} ", style=tui_rich_style("muted"))
                line.append(f"Side question... {truncated}", style=tui_rich_style("muted"))
                self._btw_spinner = line
                self.refresh_soon()
            case BtwEnd(response=response, error=error):
                self._btw_spinner = None
                q = self._btw_question or ""
                truncated_q = (q[:50] + "...") if len(q) > 50 else q
                self._btw_question = None
                if response:
                    self._emit_action_block(
                        Panel(
                            Markdown(response),
                            title=f"[dim]btw: {rich_escape(truncated_q)}[/dim]",
                            border_style=tui_rich_style("border_muted"),
                            box=box.ROUNDED,
                            padding=(0, 1),
                        )
                    )
                elif error:
                    self._emit_action_block(
                        Panel(
                            Text(error, style=tui_rich_style("error")),
                            title="[dim]btw (error)[/dim]",
                            border_style=tui_rich_style("error"),
                            box=box.ROUNDED,
                            padding=(0, 1),
                        )
                    )
                self.refresh_soon()
            case StatusUpdate():
                self._status_block.update(msg)
                if msg.mcp_status is not None:
                    self._latest_mcp_status = msg.mcp_status
                    if msg.mcp_status.loading:
                        self._mcp_loading_spinner = render_mcp_startup_text(msg.mcp_status)
                if msg.context_tokens is not None:
                    self._latest_context_tokens = msg.context_tokens
                    if self._compaction_block is not None:
                        self._compaction_block.update_context_tokens(msg.context_tokens)
            case Notification():
                self.append_notification(msg)
            case QuestionAnswered():
                self.display_question_answered(msg)
            case ProgressNote():
                self.display_progress_note(msg)
            case Suggestion():
                self.display_suggestion(msg)
            case HookTriggered():
                self.append_hook_triggered(msg)
            case HookResolved():
                self.append_hook_resolved(msg)
            case ContentPart():
                if isinstance(msg, TextPart) and msg.text:
                    self._recap_text_parts.append(msg.text)
                self.append_content(msg)
            case ToolCall():
                self._recap_tool_counts[msg.function.name] += 1
                self.append_tool_call(msg)
            case ToolCallPart():
                self.append_tool_call_part(msg)
            case ToolExecutionStarted():
                self.mark_tool_execution_started(msg.tool_call_id)
            case ToolOutputPart():
                self.append_tool_output_part(msg)
            case ToolResult():
                self._track_recap_modified_files(msg)
                self.append_tool_result(msg)
            case ApprovalResponse():
                self._reconcile_approval_requests()
            case SubagentEvent():
                self.handle_subagent_event(msg)
            case PlanDisplay():
                self.display_plan(msg)
            case ApprovalRequest():
                self.request_approval(msg)
            case QuestionRequest():
                self.request_question(msg)
            case ToolCallRequest():
                logger.warning("Unexpected ToolCallRequest in shell UI: {msg}", msg=msg)
            case _:
                pass

    def _try_submit_question(self, method: str = "enter") -> None:
        """Submit the current question answer; if all done, resolve and advance."""
        panel = self._current_question_panel
        if panel is None:
            return
        all_done = panel.submit()
        if all_done:
            from pythinker_code.telemetry import track

            track("question_answered", method=method)
            panel.request.resolve(panel.get_answers())
            self.show_next_question_request()

    def dispatch_keyboard_event(self, event: KeyEvent) -> None:
        # Handle question panel keyboard events
        if self._current_question_panel is not None:
            match event:
                case KeyEvent.UP:
                    self._current_question_panel.move_up()
                case KeyEvent.DOWN:
                    self._current_question_panel.move_down()
                case KeyEvent.LEFT:
                    self._current_question_panel.prev_tab()
                case KeyEvent.RIGHT | KeyEvent.TAB:
                    self._current_question_panel.next_tab()
                case KeyEvent.SPACE:
                    if self._current_question_panel.is_multi_select:
                        self._current_question_panel.toggle_select()
                    else:
                        self._try_submit_question(method="space")
                case KeyEvent.ENTER:
                    # "Other" is handled in keyboard_handler (async context)
                    self._try_submit_question(method="enter")
                case KeyEvent.ESCAPE:
                    from pythinker_code.telemetry import track

                    track("question_dismissed")
                    self._current_question_panel.request.resolve({})
                    self.show_next_question_request()
                case (
                    KeyEvent.NUM_1
                    | KeyEvent.NUM_2
                    | KeyEvent.NUM_3
                    | KeyEvent.NUM_4
                    | KeyEvent.NUM_5
                    | KeyEvent.NUM_6
                ):
                    # Number keys select option in question panel
                    num_map = {
                        KeyEvent.NUM_1: 0,
                        KeyEvent.NUM_2: 1,
                        KeyEvent.NUM_3: 2,
                        KeyEvent.NUM_4: 3,
                        KeyEvent.NUM_5: 4,
                        KeyEvent.NUM_6: 5,
                    }
                    idx = num_map[event]
                    panel = self._current_question_panel
                    if panel.select_index(idx):
                        if panel.is_multi_select:
                            panel.toggle_select()
                        elif not panel.is_other_selected:
                            # Auto-submit for single-select (unless "Other")
                            self._try_submit_question(method="number_key")
                case _:
                    pass
            self.refresh_soon()
            return

        # Ctrl+T toggles the pinned todo list; it is the only todo UI surface.
        if event == KeyEvent.CTRL_T and self._latest_todos:
            self.toggle_pinned_todos()
            return

        # handle ESC key to cancel the run
        if event == KeyEvent.ESCAPE and self._cancel_event is not None:
            from pythinker_code.telemetry import track

            track("cancel")
            self._cancel_event.set()
            return

        # Handle approval panel keyboard events
        if self._current_approval_request_panel is not None:
            match event:
                case KeyEvent.UP:
                    self._current_approval_request_panel.move_up()
                    self.refresh_soon()
                case KeyEvent.DOWN:
                    self._current_approval_request_panel.move_down()
                    self.refresh_soon()
                case KeyEvent.ENTER:
                    self._submit_approval()
                case KeyEvent.NUM_1 | KeyEvent.NUM_2 | KeyEvent.NUM_3 | KeyEvent.NUM_4:
                    # Number keys directly select and submit approval option
                    num_map = {
                        KeyEvent.NUM_1: 0,
                        KeyEvent.NUM_2: 1,
                        KeyEvent.NUM_3: 2,
                        KeyEvent.NUM_4: 3,
                    }
                    idx = num_map[event]
                    if idx < len(self._current_approval_request_panel.options):
                        self._current_approval_request_panel.selected_index = idx
                        self._submit_approval()
                case _:
                    pass
            return

    def _submit_approval(self) -> None:
        """Submit the currently selected approval response."""
        assert self._current_approval_request_panel is not None
        request = self._current_approval_request_panel.request
        resp = self._current_approval_request_panel.get_selected_response()
        request.resolve(resp)
        if resp == "approve_for_session":
            to_remove_from_queue: list[ApprovalRequest] = []
            for request in self._approval_request_queue:
                # approve all queued requests with the same action
                if request.action == self._current_approval_request_panel.request.action:
                    request.resolve("approve_for_session")
                    to_remove_from_queue.append(request)
            for request in to_remove_from_queue:
                self._approval_request_queue.remove(request)
        self.show_next_approval_request()

    def cleanup(self, is_interrupt: bool) -> None:
        """Cleanup the live view on step end or interruption."""
        self.flush_content(FlushReason.CANCEL if is_interrupt else FlushReason.TURN_END)

        for block in self._tool_call_blocks.values():
            if not block.finished:
                # this should not happen, but just in case
                block.finish(
                    ToolError(message="", brief="Interrupted")
                    if is_interrupt
                    else ToolOk(output="")
                )
        self._last_tool_call_block = None
        self.flush_finished_tool_calls()
        self._flush_held_tool_search()
        # Drain background-pending blocks skipped above.  They must be printed
        # to scrollback here; the transient Live area is about to be erased.
        for tool_call_id in list(self._tool_call_blocks.keys()):
            block = self._tool_call_blocks.pop(tool_call_id)
            self._archive_completed_tool_card(block)
            self._emit_action_block(block.compose())
            self.refresh_soon()
        self.flush_notifications()
        if not is_interrupt and self._active_turn_depth == 0 and self._pending_turn_recap:
            self._print_turn_recap()
            self._pending_turn_recap = False

        # Clear transient spinners to prevent visual residuals after interrupts
        self._compaction_block = None
        self._mcp_loading_spinner = None
        self._btw_spinner = None
        self._hook_blocks.clear()
        self._current_step_retry = None

        if is_interrupt:
            self._active_turn_depth = 0
            self._turn_start_time = None

        while self._approval_request_queue:
            # should not happen, but just in case
            self._approval_request_queue.popleft().resolve("reject")
        self._current_approval_request_panel = None

        while self._question_request_queue:
            self._question_request_queue.popleft().resolve({})
        self._current_question_panel = None

    def discard_retry_attempt(self, retry: StepRetry) -> None:
        """Discard partial streamed state from a failed step attempt.

        The failed attempt may have already streamed content or a tool call into
        the transient Live area. Keep scrollback untouched, but clear the live
        attempt state so the retry status and next attempt do not render beside
        stale partial output.
        """
        self._current_content_block = None
        self._tool_call_blocks.clear()
        self._last_tool_call_block = None
        self._held_tool_search_block = None
        self._current_step_retry = retry

    def flush_content(self, reason: FlushReason = FlushReason.TURN_END) -> None:
        """Flush the current content block."""
        if self._current_content_block is not None:
            block = self._current_content_block
            block.prepare_for_finalize(reason)
            self._current_content_block = None
            self._finalize_content_block_once(block)
            self.refresh_soon()

    def _emit_final_scrollback(self, renderable: RenderableType) -> None:
        live = self._live
        if live is not None:
            live.update(renderable, refresh=True)
        emit_scrollback_block(console, renderable)

    def _emit_incremental_scrollback(self, renderable: RenderableType) -> None:
        emit_scrollback_block(console, renderable)

    def _emit_action_block(self, renderable: RenderableType) -> None:
        _print_action_block(renderable)

    def _emit_steer_echo(self, renderable: RenderableType) -> None:
        console.print(renderable)

    def _finalize_content_block_once(self, block: _ContentBlock) -> None:
        """Promote one content block to scrollback exactly once."""
        self._flush_held_tool_search()
        if block.is_think:
            if block.has_pending():
                self._emit_final_scrollback(block.compose_final())
            return
        renderable = block.promote_to_scrollback()
        if renderable is not None:
            self._emit_final_scrollback(renderable)
        if block.has_expandable_card:
            self._completed_expandable_content_blocks.append(block)

    def _flush_held_tool_search(self) -> None:
        if self._held_tool_search_block is not None:
            block = self._held_tool_search_block
            self._held_tool_search_block = None
            self._emit_action_block(block.compose())
            self.refresh_soon()

    def flush_finished_tool_calls(self) -> None:
        """Flush all leading finished tool call blocks.

        Background-pending blocks (Agent results with still-running status) are
        skipped with ``continue`` instead of stopping the flush — they stay in
        the Live area so their spinner keeps animating.  Subsequent finished
        blocks can still flush past them because background agents are async.

        ToolSearch blocks are absorbed silently — only the last one in a
        consecutive run is shown (``isAbsorbedSilently`` contract).  A
        non-ToolSearch block triggers the held ToolSearch to flush
        first so ordering is preserved.
        """
        tool_call_ids = list(self._tool_call_blocks.keys())
        for tool_call_id in tool_call_ids:
            block = self._tool_call_blocks[tool_call_id]
            if block.is_background_pending:
                continue  # stays in Live area; animated each refresh tick
            if not block.finished:
                break

            self._archive_completed_tool_card(block)
            self._tool_call_blocks.pop(tool_call_id)
            if self._last_tool_call_block == block:
                self._last_tool_call_block = None
            if block.is_tool_search:
                # Discard the previously held probe and hold this one instead.
                self._held_tool_search_block = block
            else:
                self._flush_held_tool_search()
                self._emit_action_block(block.compose())
            self.refresh_soon()

    def flush_notifications(self) -> None:
        """Flush rendered notifications to terminal history."""
        self._live_notification_blocks.clear()
        while self._notification_blocks:
            self._emit_action_block(self._notification_blocks.popleft().compose())
            self.refresh_soon()

    def append_content(self, part: ContentPart) -> None:
        match part:
            case ThinkPart(think=text) | TextPart(text=text):
                is_think = isinstance(part, ThinkPart)
                # Skip empty TextPart, but still create the block for empty
                # ThinkPart so the "Thinking" indicator shows immediately
                # (e.g. Anthropic/OpenAI block-start events yield think="").
                if not text and not is_think:
                    return
                self._current_step_retry = None
                if self._current_content_block is None:
                    self._current_content_block = _ContentBlock(
                        is_think,
                        show_thinking_stream=self._show_thinking_stream,
                        paced=self._stream_pacing,
                    )
                    self.refresh_soon()
                elif self._current_content_block.is_think != is_think:
                    transition = (
                        FlushReason.TEXT_TO_THINK if is_think else FlushReason.THINK_TO_TEXT
                    )
                    self.flush_content(transition)
                    self._current_content_block = _ContentBlock(
                        is_think,
                        show_thinking_stream=self._show_thinking_stream,
                        paced=self._stream_pacing,
                    )
                    self.refresh_soon()
                if text:
                    self._current_content_block.append(text)
                    self.refresh_soon()
            case _:
                # TODO: support more content part types
                pass

    def append_tool_call(self, tool_call: ToolCall) -> None:
        self._current_step_retry = None
        self.flush_content(FlushReason.TOOL_START)
        self._tool_call_blocks[tool_call.id] = _ToolCallBlock(tool_call)
        self._last_tool_call_block = self._tool_call_blocks[tool_call.id]
        self.refresh_soon()

    def append_tool_call_part(self, part: ToolCallPart) -> None:
        if not part.arguments_part:
            return
        if self._last_tool_call_block is None:
            return
        self._last_tool_call_block.append_args_part(part.arguments_part)
        self.refresh_soon()

    def mark_tool_execution_started(self, tool_call_id: str) -> None:
        if block := self._tool_call_blocks.get(tool_call_id):
            block.mark_execution_started()
            self.refresh_soon()

    def append_tool_output_part(self, part: ToolOutputPart) -> None:
        if block := self._tool_call_blocks.get(part.tool_call_id):
            block.append_output_part(part.text, stream=part.stream)
            self.refresh_soon()

    def append_tool_result(self, result: ToolResult) -> None:
        if block := self._tool_call_blocks.get(result.tool_call_id):
            self._record_todo_display(result.return_value)
            if block.is_todo_list and not result.return_value.is_error:
                # Successful todo updates are represented only by the pinned
                # todo summary under the verb spinner.
                self._tool_call_blocks.pop(result.tool_call_id, None)
                if self._last_tool_call_block == block:
                    self._last_tool_call_block = None
                self.refresh_soon()
                return
            block.finish(result.return_value)
            self.flush_finished_tool_calls()
            self.refresh_soon()

    def _record_todo_display(self, result: ToolReturnValue) -> None:
        """Remember the latest todo display block for the pinned status tail."""
        for block in getattr(result, "display", []) or []:
            if isinstance(block, TodoDisplayBlock):
                self._latest_todos = tuple(block.items)
                return

    def _archive_completed_tool_card(self, block: _ToolCallBlock) -> None:
        if block.has_expandable_card:
            self._completed_expandable_tool_blocks.append(block)

    def append_notification(self, notification: Notification) -> None:
        block = _NotificationBlock(notification)
        self._notification_blocks.append(block)
        self._live_notification_blocks.append(block)
        self.refresh_soon()

    def append_hook_triggered(self, event: HookTriggered) -> None:
        self._hook_blocks[(event.event, event.target)] = _HookBlock(event)
        self.refresh_soon()

    def append_hook_resolved(self, event: HookResolved) -> None:
        key = (event.event, event.target)
        block = self._hook_blocks.pop(key, None)
        if block is None:
            block = _HookBlock(
                HookTriggered(
                    event=event.event,
                    target=event.target,
                    hook_count=1,
                )
            )
        block.resolve(event)
        self._emit_action_block(block.compose())
        self.refresh_soon()

    def display_question_answered(self, event: QuestionAnswered) -> None:
        self.flush_content(FlushReason.TOOL_START)
        block = _QuestionAnsweredBlock(event)
        self._emit_action_block(block.compose())
        self.refresh_soon()

    def display_progress_note(self, event: ProgressNote) -> None:
        self.flush_content(FlushReason.TOOL_START)
        self.flush_finished_tool_calls()
        block = _ProgressNoteBlock(event)
        self._emit_action_block(block.compose())
        self.refresh_soon()

    def display_suggestion(self, event: Suggestion) -> None:
        self.flush_content(FlushReason.TOOL_START)
        self.flush_finished_tool_calls()
        block = _SuggestionBlock(event)
        self._emit_action_block(block.compose())
        self.refresh_soon()

    def request_approval(self, request: ApprovalRequest) -> None:
        self._approval_request_queue.append(request)

        if self._current_approval_request_panel is None:
            console.bell()
            self.show_next_approval_request()

    def _reconcile_approval_requests(self) -> None:
        self._approval_request_queue = deque(
            request for request in self._approval_request_queue if not request.resolved
        )
        if (
            self._current_approval_request_panel is not None
            and self._current_approval_request_panel.request.resolved
        ):
            self._current_approval_request_panel = None
            self.show_next_approval_request()
        else:
            self.refresh_soon()

    def show_next_approval_request(self) -> None:
        """
        Show the next approval request from the queue.
        If there are no pending requests, clear the current approval panel.
        """
        if not self._approval_request_queue:
            if self._current_approval_request_panel is not None:
                self._current_approval_request_panel = None
                self.refresh_soon()
            return

        while self._approval_request_queue:
            request = self._approval_request_queue.popleft()
            if request.resolved:
                # skip resolved requests
                continue
            self._current_approval_request_panel = ApprovalRequestPanel(request)
            self.refresh_soon()
            break
        else:
            # All queued requests were already resolved
            if self._current_approval_request_panel is not None:
                self._current_approval_request_panel = None
                self.refresh_soon()

    def display_plan(self, msg: PlanDisplay) -> None:
        """Render plan content inline in the chat with a bordered panel."""
        self.flush_content(FlushReason.TOOL_START)
        self.flush_finished_tool_calls()
        plan_body = Markdown(msg.content)
        panel = render_worklog_card(
            "Plan",
            plan_body,
            subtitle=msg.file_path,
            border_style=tui_rich_style("border"),
        )
        self._emit_action_block(panel)

    def request_question(self, request: QuestionRequest) -> None:
        self._question_request_queue.append(request)
        if self._current_question_panel is None:
            console.bell()
            self.show_next_question_request()

    def show_next_question_request(self) -> None:
        """Show the next question request from the queue."""
        if not self._question_request_queue:
            if self._current_question_panel is not None:
                self._current_question_panel = None
                self.refresh_soon()
                self._on_question_panel_state_changed()
            return

        while self._question_request_queue:
            request = self._question_request_queue.popleft()
            if request.resolved:
                continue
            self._current_question_panel = QuestionRequestPanel(request)
            self.refresh_soon()
            self._on_question_panel_state_changed()
            break
        else:
            # All queued requests were already resolved
            if self._current_question_panel is not None:
                self._current_question_panel = None
                self.refresh_soon()
                self._on_question_panel_state_changed()

    def handle_subagent_event(self, event: SubagentEvent) -> None:
        if event.parent_tool_call_id is None:
            return
        block = self._tool_call_blocks.get(event.parent_tool_call_id)
        if block is None:
            return
        if event.agent_id is not None and event.subagent_type is not None:
            block.set_subagent_metadata(event.agent_id, event.subagent_type)

        match event.event:
            case ToolCall() as tool_call:
                block.append_sub_tool_call(tool_call)
                self.refresh_soon()
            case ToolCallPart() as tool_call_part:
                block.append_sub_tool_call_part(tool_call_part)
                self.refresh_soon()
            case ToolResult() as tool_result:
                block.finish_sub_tool_call(tool_result)
                self.refresh_soon()
            case ToolExecutionStarted() as started:
                block.mark_sub_execution_started(started.tool_call_id)
                self.refresh_soon()
            case ToolOutputPart() as output_part:
                block.append_sub_output_part(
                    output_part.tool_call_id,
                    output_part.text,
                    stream=output_part.stream,
                )
                self.refresh_soon()
            case _:
                # ignore other events for now
                # TODO: may need to handle multi-level nested subagents
                pass
