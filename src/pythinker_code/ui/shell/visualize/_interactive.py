"""Interactive prompt view for the bottom dynamic area.

``_PromptLiveView`` extends ``_LiveView`` with prompt_toolkit integration:
input routing (queue/steer/btw), modal management, and key handling.
"""

# pyright: reportPrivateUsage=false, reportUnusedClass=false

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress

from prompt_toolkit.application.run_in_terminal import run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyPressEvent
from pythinker_core.tooling import ToolReturnValue
from rich.console import Group, RenderableType
from rich.text import Text

from pythinker_code.ui.shell.console import (
    console,
    current_console_width,
    redirect_console_prints,
    render_to_ansi,
)
from pythinker_code.ui.shell.echo import render_user_echo_text
from pythinker_code.ui.shell.keyboard import KeyEvent
from pythinker_code.ui.shell.motion import (
    reduced_motion_enabled,
    stream_reveal_interval_s,
)
from pythinker_code.ui.shell.prompt import (
    CustomPromptSession,
    UserInput,
)
from pythinker_code.ui.shell.visualize._blocks import (
    FlushReason,
)
from pythinker_code.ui.shell.visualize._btw_panel import _BtwModalDelegate
from pythinker_code.ui.shell.visualize._input_router import InputAction, classify_input
from pythinker_code.ui.shell.visualize._live_view import _LiveView
from pythinker_code.ui.shell.visualize._question_panel import (
    QuestionPromptDelegate,
    QuestionRequestPanel,
)
from pythinker_code.ui.theme import tui_rich_style
from pythinker_code.utils.aioqueue import QueueShutDown
from pythinker_code.utils.logging import logger
from pythinker_code.utils.slashcmd import SlashCommandCall
from pythinker_code.wire import WireUISide
from pythinker_code.wire.types import (
    BtwBegin,
    BtwEnd,
    ContentPart,
    Notification,
    StatusUpdate,
    SteerInput,
    StepInterrupted,
    Suggestion,
    TurnBegin,
    TurnEnd,
    WireMessage,
)

BtwRunner = Callable[[str, Callable[[str], None] | None], Awaitable[tuple[str | None, str | None]]]
"""async (question, on_text_chunk) -> (response, error). Used for direct btw execution."""

ShellCommandRunner = Callable[[SlashCommandCall], Awaitable[None]]
"""async (call) -> None. Runs a shell-level slash command while a task is in progress."""

_TRANSIENT_COMMAND_PANEL_S = 10.0
"""How long mid-task slash-command output stays visible in the live area."""

_TRANSIENT_COMMAND_PANEL_MAX_LINES = 30
"""Cap so verbose commands (/help) cannot swallow the live area."""

_STATUS_REFRESH_INTERVAL_S = 0.22
_STATUS_REFRESH_REDUCED_INTERVAL_S = 1.0
# Redraw ticks after a terminal resize before tips return — old wrapped rows may
# not be fully erased until prompt_toolkit settles at the new geometry.
_RESIZE_RECOVERY_FRAMES = 3


def _handoff_trace(event: str) -> None:
    """Append a timeline event to the handoff-debug log when enabled.

    Diagnostic only. Set ``PYTHINKER_TUI_HANDOFF_LOG=/path/to/file`` to record
    every scrollback handoff (each is a ``run_in_terminal`` prompt-app teardown —
    the visible "pop"), every tool/think transition, and every turn end. A recorded
    session can then be replayed against the log to count per-turn pops and their
    cause (count ``HANDOFF`` lines between ``TURN_END`` markers; compare a
    text-only turn against a many-tool turn). No-op (one env lookup) when unset, so
    it is safe to leave in place. Never raises: diagnostics must not break the UI.
    """
    path = os.environ.get("PYTHINKER_TUI_HANDOFF_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.monotonic():.3f}\t{event}\n")
    except OSError:
        # Intentional: handoff diagnostics are best-effort and must never
        # disrupt the interactive UI if the log path is unwritable.
        pass


class _PromptLiveView(_LiveView):
    """Interactive prompt view: renders agent output above the input buffer.

    Supports two modes for user input during streaming:
    - **Queue (Enter)**: message is held and sent as a new turn after the
      current turn completes.  Queued messages are shown above the input and
      can be recalled with ↑.
    - **Steer (Ctrl+S)**: message is injected immediately into the running
      turn's context.  Shown permanently in the conversation flow.
    """

    modal_priority = 0

    def __init__(
        self,
        initial_status: StatusUpdate,
        *,
        prompt_session: CustomPromptSession,
        steer: Callable[[str | list[ContentPart]], None],
        btw_runner: BtwRunner | None = None,
        shell_command_runner: ShellCommandRunner | None = None,
        cancel_event: asyncio.Event | None = None,
        show_thinking_stream: bool = False,
        show_turn_recaps: bool = False,
    ) -> None:
        super().__init__(
            initial_status,
            cancel_event,
            show_thinking_stream=show_thinking_stream,
            show_turn_recaps=show_turn_recaps,
        )
        self._prompt_session = prompt_session
        self._steer = steer
        self._btw_runner = btw_runner
        self._shell_command_runner = shell_command_runner
        self._shell_command_tasks: set[asyncio.Task[None]] = set()
        self._transient_command_output: str | None = None
        self._transient_command_expires: float = 0.0
        self._pending_local_steer_count: int = 0
        self._turn_ended = False
        self._question_modal: QuestionPromptDelegate | None = None
        # -- Queue: messages waiting to be sent after the turn ends ----------
        self._queued_messages: list[UserInput] = []
        # -- BTW modal (replaces prompt line when active) --------------------
        self._btw_modal: _BtwModalDelegate | None = None
        self._btw_dismiss_event: asyncio.Event | None = None
        self._btw_refresh_task: asyncio.Task[None] | None = None
        self._btw_run_task: asyncio.Task[None] | None = None
        self._status_refresh_task: asyncio.Task[None] | None = None
        self._pending_scrollback: list[tuple[RenderableType, bool]] = []
        self._pending_scrollback_anchors: list[bool] = []
        self._scrollback_handoff_depth: int = 0
        self._scrollback_flush_lock = asyncio.Lock()
        self._last_terminal_size: tuple[int, int] | None = None
        self._resize_recovery_remaining: int = 0
        # True once this turn has committed anything to scrollback via a
        # run_in_terminal handoff. Editable input content is hidden until then
        # (see running_prompt_hide_input_card), while the empty card chrome stays
        # visible so the prompt bar does not disappear while the agent is loading.
        self._committed_scrollback_this_turn: bool = False
        self._awaiting_input_card_restore_anchor: bool = False

    # -- Helpers -------------------------------------------------------------

    @property
    def _suppress_transient_preamble(self) -> bool:
        """True while scrollback handoff must not paint height-varying preamble."""
        return getattr(self, "_scrollback_handoff_depth", 0) > 0

    @property
    def _hide_working_tips(self) -> bool:
        """True while resize recovery is settling — tips are multi-row and ghost easily."""
        return getattr(self, "_resize_recovery_remaining", 0) > 0

    def _current_terminal_size(self) -> tuple[int, int] | None:
        from prompt_toolkit.application import get_app_or_none

        app = get_app_or_none()
        if app is None:
            return None
        size = app.output.get_size()
        return (size.columns, size.rows)

    def _tick_resize_recovery(self) -> None:
        """Detect terminal geometry changes and force a hard preamble invalidation."""
        size = self._current_terminal_size()
        if size is not None:
            columns, rows = size
            if columns >= 1 and rows >= 1:
                if self._last_terminal_size != size:
                    self._last_terminal_size = size
                    self._resize_recovery_remaining = _RESIZE_RECOVERY_FRAMES
                    self._force_refresh = True
                    _handoff_trace(f"RESIZE\t{columns}x{rows}")
                    self._reset_prompt_renderer("resize")
            else:
                _handoff_trace(f"RESIZE_IGNORE\t{columns}x{rows}")
        if self._resize_recovery_remaining > 0:
            self._resize_recovery_remaining -= 1

    def _reset_prompt_renderer(self, reason: str) -> None:
        """Drop the prompt renderer's diff state so the next redraw repaints every row.

        When the real screen diverges from prompt_toolkit's frame model —
        ConPTY rewraps lines on resize, a terminal replays/clears the
        viewport, a scrollback handoff dies mid-erase — the differential
        renderer keeps emitting empty diffs over a blank screen and the UI
        never comes back. Resetting makes the next frame absolute.
        """
        from prompt_toolkit.application import get_app_or_none

        app = get_app_or_none()
        if app is None:
            return
        try:
            app.renderer.reset()
        except Exception as exc:  # noqa: BLE001 — recovery must never take down the UI loop
            _handoff_trace(f"RENDERER_RESET_FAIL\t{reason}\t{type(exc).__name__}:{exc}")
            logger.debug("Prompt renderer reset failed ({}): {}", reason, exc)
        else:
            _handoff_trace(f"RENDERER_RESET\t{reason}")

    def _defer_scrollback_handoff(self) -> bool:
        """Backpressure: defer permanent scrollback while preamble geometry is unstable."""
        return self._resize_recovery_remaining > 0

    def _safe_prompt_invalidate(self) -> None:
        """Invalidate the prompt without letting teardown races take down the UI loop."""
        try:
            self._prompt_session.invalidate()
        except Exception as exc:  # noqa: BLE001 — invalidate must never abort handoff cleanup
            _handoff_trace(f"INVALIDATE_FAIL\t{type(exc).__name__}:{exc}")
            logger.debug("Prompt invalidation failed during scrollback handoff: {}", exc)

    def _prompt_is_finalizing(self) -> bool:
        """True while scrollback is queued or being emitted above the prompt."""
        if (
            getattr(self, "_pending_scrollback", None)
            or getattr(self, "_scrollback_handoff_depth", 0) > 0
        ):
            return True
        block = getattr(self, "_current_content_block", None)
        if block is None or block.is_think:
            return False
        return bool(block._committed_renderables or block.has_active_stream_preview())

    def _finalizing_indicator(self) -> RenderableType:
        from pythinker_code.ui.shell.motion import ActivitySnapshot, activity_status_line

        return activity_status_line(
            ActivitySnapshot(label="Finalizing", elapsed_s=0.0, spinner="shape"),
            width=current_console_width(),
        )

    async def _run_scrollback_handoff(self, emit: Callable[[], None], *, reason: str = "?") -> None:
        _handoff_trace(f"HANDOFF\t{reason}")
        self._scrollback_handoff_depth += 1
        self._safe_prompt_invalidate()
        try:
            if console.is_terminal:
                await run_in_terminal(emit)
            else:
                emit()
        except Exception as exc:
            _handoff_trace(f"HANDOFF_FAIL\t{reason}\t{type(exc).__name__}:{exc}")
            # The teardown/erase may have half-completed; force an absolute
            # repaint so the prompt recovers instead of diffing a wrong model.
            self._reset_prompt_renderer("handoff-fail")
            raise
        finally:
            self._scrollback_handoff_depth -= 1
            self._safe_prompt_invalidate()

    @property
    def _btw_active(self) -> bool:
        return self._btw_modal is not None

    def _dismiss_btw(self) -> None:
        if self._btw_modal is not None:
            self._prompt_session.detach_modal(self._btw_modal)
            self._btw_modal = None
        if self._btw_run_task is not None:
            self._btw_run_task.cancel()
            self._btw_run_task = None
        if self._btw_refresh_task is not None:
            self._btw_refresh_task.cancel()
            self._btw_refresh_task = None
        # Wake the visualize_loop if it's waiting for user dismiss
        if self._btw_dismiss_event is not None:
            self._btw_dismiss_event.set()
            self._btw_dismiss_event = None
        self._prompt_session.invalidate()

    def _start_btw(self, question: str) -> None:
        """Set up the btw modal and start the LLM task."""
        import time

        # Attach modal FIRST (hides input buffer), then clear buffer.
        # This avoids a render frame between clear and attach where the
        # user would see an empty input flash.
        modal = _BtwModalDelegate(on_dismiss=self._dismiss_btw)
        modal._question = question  # pyright: ignore[reportPrivateUsage]
        modal.set_start_time(time.monotonic())
        self._btw_modal = modal
        self._prompt_session.attach_modal(modal)
        # Now safe to clear — buffer is hidden by modal
        buf = self._prompt_session._session.default_buffer  # pyright: ignore[reportPrivateUsage]
        if buf.text:
            buf.set_document(Document(), bypass_readonly=True)
        self._btw_refresh_task = asyncio.create_task(self._btw_refresh_loop())
        self._btw_run_task = asyncio.create_task(self._run_btw(question))

    async def _run_btw(self, question: str) -> None:
        """Execute /btw directly via btw_runner (no wire)."""
        assert self._btw_runner is not None
        try:

            def _on_chunk(chunk: str) -> None:
                if self._btw_modal is not None:
                    self._btw_modal.append_text(chunk)

            response, error = await self._btw_runner(question, _on_chunk)
            if self._btw_modal is not None:
                self._btw_modal.set_result(response, error)
        except asyncio.CancelledError:
            pass  # dismiss cancelled us — expected
        except Exception as e:
            if self._btw_modal is not None:
                self._btw_modal.set_result(None, str(e))
        finally:
            self._btw_run_task = None  # self-clear so _dismiss_btw won't cancel a done task
            if self._btw_refresh_task is not None:
                self._btw_refresh_task.cancel()
                self._btw_refresh_task = None
            self._prompt_session.invalidate()

    async def _btw_refresh_loop(self) -> None:
        """Periodically invalidate prompt so the btw modal spinner animates."""
        try:
            while True:
                await asyncio.sleep(0.08)
                self._prompt_session.invalidate()
        except asyncio.CancelledError:
            pass

    async def _status_refresh_loop(self) -> None:
        """Periodically invalidate prompt so pinned status shimmer is frame-based.

        Wire events are bursty: a long-running subagent can leave the prompt
        untouched for seconds, which freezes shimmer even though the turn is
        still active. Keep this loop prompt-scoped and cheap; Rich Live mode has
        its own refresh clock.
        """
        try:
            while True:
                self._tick_resize_recovery()
                # Drain buffered paced text smoothly, even past TurnEnd, so the
                # tail flows out instead of popping when the block finally
                # commits. advance_stream_reveal() is a no-op unless a paced block
                # has backlog, so reduced-motion / unpaced turns fall straight
                # through to the calm status cadence below.
                advanced = self.advance_stream_reveal()
                # No mid-stream scrollback commit here: each commit is a
                # run_in_terminal prompt-app teardown (the visible "jump"). Completed
                # prose stays in the in-place live preview (clamped to a tail window
                # by _compose_composing) and is flushed to scrollback exactly once at
                # a tool transition or turn end (_drain_content_for_transition /
                # flush_content). _flush_pending_scrollback below drains only that
                # once-per-event queue, never per-paragraph mid-stream pops.
                await self._flush_pending_scrollback()
                needs_animation = self._streaming_needs_animation_frame()
                if advanced or needs_animation:
                    self._dirty = True
                if self._dirty or self._force_refresh:
                    self._prompt_session.invalidate()
                    self._dirty = False
                    self._force_refresh = False
                    self._need_recompose = False
                    await asyncio.sleep(stream_reveal_interval_s())
                    continue
                interval = (
                    _STATUS_REFRESH_REDUCED_INTERVAL_S
                    if reduced_motion_enabled()
                    else _STATUS_REFRESH_INTERVAL_S
                )
                await asyncio.sleep(interval)
                if self._active_turn_depth > 0 and not self._turn_ended:
                    self._prompt_session.invalidate()
        except asyncio.CancelledError:
            pass

    def advance_stream_reveal(self) -> bool:
        return super().advance_stream_reveal()

    async def _emit_incremental_content_commits(self) -> bool:
        block = self._current_content_block
        if block is None or block.is_think:
            return False
        committed = block.take_committed_renderables()
        if not committed:
            return False

        def emit_committed() -> None:
            for renderable in committed:
                self._emit_incremental_scrollback(renderable)

        await self._run_scrollback_handoff(emit_committed, reason=f"prose_commit({len(committed)})")
        await self._after_incremental_scrollback_emitted()
        return True

    async def _after_incremental_scrollback_emitted(self) -> None:
        self._prompt_session.invalidate()

    async def _flush_pending_scrollback(self, *, force: bool = False) -> None:
        """Drain queued scrollback to scrollback.

        In a real terminal, route through run_in_terminal so the prompt preamble
        is not fossilized into permanent transcript output.  In piped/non-terminal
        mode run_in_terminal does not write to the captured stdout, so fall back to
        direct console.print() which matches the pre-preamble base-class behavior.

        Scrollback is removed from the queue only after a successful handoff emit.
        Failed emits leave the queue intact for a later retry; handoffs are deferred
        while terminal geometry is settling after a resize unless ``force`` is set
        (e.g. outermost turn end must not leave completed prose stuck finalizing).
        """
        async with self._scrollback_flush_lock:
            if not self._pending_scrollback:
                return
            if not force and self._defer_scrollback_handoff():
                _handoff_trace(
                    f"HANDOFF_DEFER\tpending_scrollback({len(self._pending_scrollback)})"
                )
                return
            batch = self._pending_scrollback[:]
            anchor_batch = self._pending_scrollback_anchors[: len(batch)]
            if self.focus_model is not None and self._active_turn_depth > 0:
                del self._pending_scrollback[: len(batch)]
                del self._pending_scrollback_anchors[: len(batch)]
                self._safe_prompt_invalidate()
                return

            def emit() -> None:
                for renderable, blank_row in batch:
                    console.print(renderable)
                    if blank_row:
                        console.print()

            try:
                await self._run_scrollback_handoff(emit, reason=f"pending_scrollback({len(batch)})")
            except Exception:
                logger.exception(
                    "Failed to flush pending scrollback; retaining {} queued blocks",
                    len(batch),
                )
                return

            del self._pending_scrollback[: len(batch)]
            del self._pending_scrollback_anchors[: len(batch)]
            if any(anchor_batch):
                first_commit = not self._committed_scrollback_this_turn
                self._committed_scrollback_this_turn = True
                if first_commit:
                    self._awaiting_input_card_restore_anchor = True
                elif self._awaiting_input_card_restore_anchor:
                    self._awaiting_input_card_restore_anchor = False
            self._safe_prompt_invalidate()

    def _emit_final_scrollback(self, renderable: RenderableType) -> None:
        self._pending_scrollback.append((renderable, True))
        self._pending_scrollback_anchors.append(False)

    def _emit_action_block(self, renderable: RenderableType) -> None:
        self._pending_scrollback.append((renderable, True))
        self._pending_scrollback_anchors.append(True)

    def _emit_steer_echo(self, renderable: RenderableType) -> None:
        self._pending_scrollback.append((renderable, False))
        if not hasattr(self, "_pending_scrollback_anchors"):
            self._pending_scrollback_anchors = []
        self._pending_scrollback_anchors.append(False)

    def _print_turn_recap(self) -> None:
        block = self._build_turn_recap_block()
        if block is None:
            return
        self._pending_scrollback.append((Text(""), False))
        self._pending_scrollback_anchors.append(False)
        self._pending_scrollback.append((block, False))
        self._pending_scrollback_anchors.append(False)
        self._pending_scrollback.append((Text(""), False))
        self._pending_scrollback_anchors.append(False)

    async def _drain_content_for_transition(self, reason: FlushReason) -> None:
        _handoff_trace(f"TRANSITION\t{reason.name}")
        await super()._drain_content_for_transition(reason)
        if self._dirty:
            self._flush_prompt_refresh()

    # -- Public API: queued messages for the shell to drain ------------------

    def drain_queued_messages(self) -> list[UserInput]:
        """Return and clear all queued messages (called by shell after turn)."""
        msgs = list(self._queued_messages)
        self._queued_messages.clear()
        return msgs

    async def wait_for_btw_dismiss(self) -> None:
        """Wait for btw LLM completion + user dismiss, then clean up.

        Called by the shell AFTER visualize_loop returns (which must exit
        within run_soul's 0.5s ui_task timeout).  The modal is still
        attached to prompt_session, so prompt_toolkit continues to render
        and handle key events.
        """
        if self._btw_modal is None:
            return
        # If LLM is still running, wait for it (user can Escape to cancel)
        if self._btw_run_task is not None and not self._btw_run_task.done():
            with suppress(asyncio.CancelledError):
                await self._btw_run_task
        # Wait for user dismiss (Escape/Enter/Space)
        if self._btw_modal is not None:  # pyright: ignore[reportUnnecessaryComparison]
            self._btw_dismiss_event = asyncio.Event()
            await self._btw_dismiss_event.wait()
        # Clean up: detach modal, cancel remaining tasks
        self._dismiss_btw()

    # -- Visualize loop ------------------------------------------------------

    async def visualize_loop(self, wire: WireUISide):
        # Declare outside try so finally can always cancel them.
        wire_task: asyncio.Task[WireMessage] | None = None
        external_task: asyncio.Task[WireMessage] | None = None
        status_refresh_task: asyncio.Task[None] | None = None
        try:
            wire_task = asyncio.create_task(wire.receive())
            external_task = asyncio.create_task(self._external_messages.get())
            status_refresh_task = asyncio.create_task(self._status_refresh_loop())
            self._status_refresh_task = status_refresh_task
            while True:
                from_external = False
                try:
                    done, _ = await asyncio.wait(
                        [wire_task, external_task, status_refresh_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if status_refresh_task in done:
                        # The status loop is expected to run until cancelled at
                        # shutdown. If it finished while the main loop is live it
                        # raised — surface that instead of silently freezing the
                        # prompt repaint clock.
                        status_refresh_task.result()
                        raise RuntimeError("prompt status refresh loop exited unexpectedly")
                    if wire_task in done:
                        msg = wire_task.result()
                        wire_task = asyncio.create_task(wire.receive())
                    else:
                        msg = external_task.result()
                        external_task = asyncio.create_task(self._external_messages.get())
                        from_external = True
                except QueueShutDown:
                    msg, external_task = await self._drain_external_message_after_wire_shutdown(
                        external_task
                    )
                    if msg is not None:
                        if reason := self._transition_flush_reason(msg):
                            await self._drain_content_for_transition(reason)
                        self.dispatch_wire_message(msg)
                        await self._flush_pending_scrollback()
                        self._flush_prompt_refresh()
                        continue
                    self.cleanup(is_interrupt=False)
                    await self._flush_pending_scrollback(force=True)
                    self._force_refresh = True
                    self._flush_prompt_refresh()
                    break

                if isinstance(msg, StepInterrupted):
                    self.cleanup(is_interrupt=True)
                    await self._flush_pending_scrollback(force=True)
                    self._force_refresh = True
                    self._flush_prompt_refresh()
                    break

                if isinstance(msg, TurnEnd):
                    self._active_turn_depth = max(0, self._active_turn_depth - 1)
                    turn_ended = self._active_turn_depth == 0
                    if turn_ended:
                        _handoff_trace("TURN_END")
                        self.flush_content(FlushReason.TURN_END)
                        self._turn_ended = True
                        self._turn_start_time = None
                        self._pending_turn_recap = True
                    else:
                        self._turn_ended = False
                    self._force_refresh = True
                    await self._flush_pending_scrollback(force=turn_ended)
                    self._flush_prompt_refresh()
                    continue

                if reason := self._transition_flush_reason(msg):
                    await self._drain_content_for_transition(reason)
                self.dispatch_wire_message(msg)
                if from_external:
                    # External (out-of-band) messages — approval requests, steer
                    # input — are interactive and must repaint at once rather than
                    # wait for the status refresh cadence.
                    self._force_refresh = True
                await self._flush_pending_scrollback()
                self._flush_prompt_refresh()

            # NOTE: btw dismiss waiting is handled by the shell layer
            # (run_soul_command → wait_for_btw_dismiss) AFTER visualize_loop
            # returns, because run_soul gives ui_task only a 0.5s timeout.
        finally:
            self._external_messages.shutdown(immediate=True)
            for task in (wire_task, external_task, status_refresh_task):
                if task is None:
                    continue
                task.cancel()
                with suppress(asyncio.CancelledError, QueueShutDown):
                    await task
            # Mid-turn slash-command tasks must not outlive the live view that
            # displays their transient output. The runner already contains
            # command exceptions; gather() retrieves the CancelledError.
            if self._shell_command_tasks:
                command_tasks = [*self._shell_command_tasks]
                for task in command_tasks:
                    task.cancel()
                await asyncio.gather(*command_tasks, return_exceptions=True)
            self._status_refresh_task = None
            self._pending_local_steer_count = 0
            # Do NOT dismiss btw here — the shell will call
            # wait_for_btw_dismiss() after visualize_loop returns.
            # Only cancel the refresh task (spinner animation stops,
            # but modal stays attached for rendering + key handling).
            if self._btw_refresh_task is not None:
                self._btw_refresh_task.cancel()
                self._btw_refresh_task = None
            self._turn_ended = False
            if self._question_modal is not None:
                self._prompt_session.detach_modal(self._question_modal)
                self._question_modal = None
            self._prompt_session.invalidate()

    # -- Input handling ------------------------------------------------------

    def _intercept_shell_command(self, user_input: UserInput) -> bool:
        """Intercept shell-level slash commands typed during a running task.

        Returns True when the input was consumed: commands flagged
        ``available_during_task`` run immediately (output shows transiently
        in the live area); the rest are rejected with a toast. Returns False
        for non-shell input so callers can queue/steer it normally.
        """
        from pythinker_code.utils.slashcmd import parse_slash_command_call

        cmd = parse_slash_command_call(user_input.resolved_command.strip())
        if cmd is None:
            return False
        from pythinker_code.ui.shell.slash import registry as shell_registry

        command = shell_registry.find_command(cmd.name)
        if command is None:
            return False
        from pythinker_code.ui.shell.prompt import toast

        if not command.available_during_task or self._shell_command_runner is None:
            toast(
                f"/{cmd.name} is disabled while a task is in progress",
                topic="input-ignored",
                duration=3.0,
            )
            return True
        from pythinker_code.telemetry import track

        track("input_command", command=command.name)
        runner = self._shell_command_runner
        echo = render_user_echo_text(user_input.resolved_command)

        async def _run() -> None:
            # Capture the command's output (this task's prints only) and show
            # it transiently in the live area instead of polluting scrollback
            # above the streaming agent output.
            try:
                with redirect_console_prints(columns=current_console_width()) as buf:
                    console.print(echo)
                    try:
                        await runner(cmd)
                    finally:
                        self._show_transient_command_output(buf.getvalue().rstrip("\n"))
            except Exception:
                # The task object is discarded on completion; without this the
                # crash would vanish with it.
                from pythinker_code.utils.logging import logger

                logger.exception("Mid-task slash command /{} failed", cmd.name)

        task = asyncio.create_task(_run())
        self._shell_command_tasks.add(task)
        task.add_done_callback(self._shell_command_tasks.discard)
        return True

    def _show_transient_command_output(self, ansi_text: str) -> None:
        if not ansi_text:
            return
        lines = ansi_text.splitlines()
        if len(lines) > _TRANSIENT_COMMAND_PANEL_MAX_LINES:
            hidden = len(lines) - _TRANSIENT_COMMAND_PANEL_MAX_LINES
            lines = [*lines[:_TRANSIENT_COMMAND_PANEL_MAX_LINES], f"… +{hidden} more lines"]
            ansi_text = "\n".join(lines)
        self._transient_command_output = ansi_text
        self._transient_command_expires = time.monotonic() + _TRANSIENT_COMMAND_PANEL_S
        self._prompt_session.invalidate()

    def _dismiss_transient_command_output(self) -> None:
        self._transient_command_output = None

    def _current_transient_command_output(self) -> str | None:
        if self._transient_command_output is None:
            return None
        if time.monotonic() >= self._transient_command_expires:
            self._transient_command_output = None
            return None
        return self._transient_command_output

    def handle_local_input(self, user_input: UserInput) -> None:
        """Route user input through the unified classifier."""
        if not user_input or self._turn_ended:
            return
        # New input dismisses any lingering slash-command panel.
        self._dismiss_transient_command_output()
        action = classify_input(user_input.resolved_command, is_streaming=True)
        match action.kind:
            case InputAction.BTW:
                if self._btw_runner is not None and not self._btw_active:
                    self._start_btw(action.args)
            case InputAction.QUEUE:
                # Shell-only commands must not be queued — they would be
                # misrouted through run_soul() instead of the shell dispatcher.
                # Safe ones run immediately; the rest are rejected.
                if self._intercept_shell_command(user_input):
                    return
                self._queued_messages.append(user_input)
                from pythinker_code.telemetry import track

                track("input_queue")
                # Invalidate directly — _flush_prompt_refresh() is gated by
                # _need_recompose which may be False between wire events.
                self._prompt_session.invalidate()
            case InputAction.IGNORED:
                from pythinker_code.ui.shell.prompt import toast

                toast(action.args, topic="input-ignored", duration=3.0)
            case _:
                pass  # SEND and unknown actions are no-ops during streaming

    def handle_immediate_steer(self, user_input: UserInput) -> None:
        """Ctrl+S: inject immediately into the running turn's context."""
        if not user_input or self._turn_ended:
            return
        # Intercept /btw and IGNORED (e.g. /btw without args) on Ctrl+S
        action = classify_input(user_input.resolved_command, is_streaming=True)
        if action.kind == InputAction.BTW:
            if self._btw_runner is not None and not self._btw_active:
                self._start_btw(action.args)
            return
        if action.kind == InputAction.IGNORED:
            from pythinker_code.ui.shell.prompt import toast

            toast(action.args, topic="input-ignored", duration=3.0)
            return
        # Intercept shell-only commands — same handling as the Enter/queue path
        if self._intercept_shell_command(user_input):
            return
        # Queue permanently in conversation flow with UI-only text placeholders expanded.
        self._emit_steer_echo(render_user_echo_text(user_input.resolved_command))
        from pythinker_code.telemetry import track

        track("input_steer")
        # Track that we originated this steer locally (FIFO counter for dedup)
        self._pending_local_steer_count += 1
        self._steer(user_input.content)
        self._flush_prompt_refresh()

    # -- Wire event dispatch -------------------------------------------------

    def dispatch_wire_message(self, msg: WireMessage) -> None:
        # Dedup locally-originated steers: we know how many we sent,
        # so consume the matching SteerInput events without content comparison.
        # This avoids text vs media mismatch issues.
        if isinstance(msg, SteerInput) and self._pending_local_steer_count > 0:
            self._pending_local_steer_count -= 1
            return
        # Suppress parent's BtwBegin/BtwEnd spinner — btw is handled via modal
        if isinstance(msg, (BtwBegin, BtwEnd)):
            self._btw_spinner = None
            return
        if isinstance(msg, Notification) and msg.source_kind == "background_task":
            # Interactive shell users already see background task completions as
            # bottom-toolbar toasts from the shell notification watcher. Do not
            # also inject them into the chat transcript/status area above the
            # prompt, where they can look like part of the previous assistant
            # answer.
            return
        # A fresh turn starts hidden-carded until its first commit — reset the
        # flag on the 0->1 transition (super() increments the depth below).
        if isinstance(msg, TurnBegin) and self._active_turn_depth == 0:
            self._committed_scrollback_this_turn = False
            self._awaiting_input_card_restore_anchor = False
        super().dispatch_wire_message(msg)

    def display_suggestion(self, event: Suggestion) -> None:
        super().display_suggestion(event)
        # Stage unconditionally: an empty prefill clears any prior staged value
        # (stage_suggestion_prefill stores ``None`` for blank input), so a later
        # suggestion without a prefill cannot leave stale Esc+s text behind.
        self._prompt_session.stage_suggestion_prefill(event.prefill)

    # -- Running prompt rendering --------------------------------------------

    def _record_todo_display(self, result: ToolReturnValue) -> None:
        super()._record_todo_display(result)
        self._prompt_session.update_pinned_todos(getattr(self, "_latest_todos", ()))

    def render_agent_status(self, columns: int) -> ANSI:
        """Render agent streaming output — always visible regardless of modal.

        Uses ``compose_agent_output()`` (not ``compose()``) to avoid rendering
        approval/question panels here.  Those panels are rendered by their
        respective modal delegates in Layer 2.
        """
        if self._turn_ended and not self._prompt_is_finalizing():
            return ANSI("")
        # During a scrollback handoff the prompt app is torn down and redrawn
        # around run_in_terminal (every tool transition / turn end). Re-rendering
        # the multi-row live stream in that window is what gets left behind as
        # fossilized scrollback when the teardown erase height drifts. The
        # committed prose is emitted by the handoff itself, and the remaining
        # tail is re-rendered once the handoff completes — so suppress the
        # transient body for the duration of the handoff.
        if self._suppress_transient_preamble:
            return ANSI("")
        from prompt_toolkit.application import get_app_or_none

        from pythinker_code.ui.shell.prompt import _prompt_preamble_max_rows

        app = get_app_or_none()
        terminal_rows = app.output.get_size().rows if app is not None else None
        # Reserve one row for the pinned verb spinner rendered below the clip hint.
        body_budget = max(1, _prompt_preamble_max_rows(terminal_rows) - 1)
        content_block = getattr(self, "_current_content_block", None)
        if content_block is not None:
            content_block.set_preview_row_budget(body_budget)
        # Exclude activity rows here — the prompt pins the active spinner
        # separately via ``render_pinned_status_tail`` so a clipped agent stream
        # cannot hide it or place it between committed prose and the live tail.
        blocks = self.compose_agent_output(
            include_working_indicator=False,
            include_content_activity=False,
        )
        if not blocks:
            return ANSI("")
        body = render_to_ansi(Group(*blocks), columns=columns).rstrip("\n")
        return ANSI(body if body else "")

    def render_pinned_status_tail(self, columns: int) -> ANSI:
        """Render the trailing verb spinner that the prompt keeps pinned below a
        (possibly clipped) agent stream, so it stays visible above the input."""
        if (
            self._current_question_panel is not None
            or self._current_approval_request_panel is not None
        ):
            return ANSI("")

        finalizing = self._prompt_is_finalizing()
        turn_active = self._active_turn_depth > 0 and not self._turn_ended
        if not turn_active and not finalizing:
            return ANSI("")

        # During scrollback handoff the prompt app is torn down around
        # run_in_terminal. Any pinned spinner/tip row rendered in that window can
        # be fossilized into permanent scrollback — suppress all transient tail
        # content for the handoff duration.
        if self._suppress_transient_preamble:
            return ANSI("")

        if finalizing and not turn_active:
            body = render_to_ansi(self._finalizing_indicator(), columns=columns).rstrip("\n")
            return ANSI(body if body else "")

        content_block = getattr(self, "_current_content_block", None)
        if self._active_subagent_activity_label() is not None:
            body = render_to_ansi(
                self._working_indicator(hide_tips=self._hide_working_tips),
                columns=columns,
            ).rstrip("\n")
        elif content_block is not None and not content_block.is_think:
            body = render_to_ansi(content_block._compose_spinner(), columns=columns).rstrip("\n")
        else:
            body = render_to_ansi(
                self._working_indicator(hide_tips=self._hide_working_tips),
                columns=columns,
            ).rstrip("\n")
        return ANSI(body if body else "")

    def _working_indicator(self, *, hide_tips: bool = False) -> RenderableType:
        return super()._working_indicator(hide_tips=hide_tips)

    def render_running_prompt_body(self, columns: int) -> ANSI:
        """Render the interactive part — transient command output + queued messages."""
        parts: list[str] = []
        if (panel := self._current_transient_command_output()) is not None:
            parts.append(panel)
        if self._queued_messages:
            blocks: list[RenderableType] = []
            from rich.style import Style as _RStyle

            for qi in self._queued_messages:
                blocks.append(
                    Text(f"❯ {qi.command}", style=tui_rich_style("info") + _RStyle(dim=True))
                )
            blocks.append(Text("↑ to edit · ctrl-s to send immediately", style="dim"))

            body = render_to_ansi(Group(*blocks), columns=columns).rstrip("\n")
            if body:
                parts.append(body)
        return ANSI("\n".join(parts))

    def running_prompt_placeholder(self) -> str | None:
        if self._current_approval_request_panel is not None:
            return "Use ↑/↓ or 1/2/3, then press Enter to respond to the approval request."
        return None

    def running_prompt_hides_input_buffer(self) -> bool:
        return False

    def running_prompt_hide_input_card(self) -> bool:
        """True while the input card must stay hidden to avoid fossilizing it.

        The card is hidden from turn-start until this turn's first scrollback
        commit. That first commit's ``run_in_terminal`` teardown fossilizes
        whatever chrome sits in the pre-handoff frame (the erase-height drifts on
        the first transition into streaming); keeping the card out of that frame
        is the only reliable prevention — suppressing it merely *during* the
        handoff is too late, because the erase runs before the repaint. Once the
        turn has committed, the layout is established and the card repaints for
        the rest of the turn so the user can see where to steer.

        No ``_active_turn_depth`` guard: the flag must hide the card from the
        moment the delegate attaches — which can precede the ``TurnBegin`` that
        raises the depth — through the first commit. ``_committed_scrollback_this_turn``
        is explicitly reset to False on each ``TurnBegin`` (see
        ``dispatch_wire_message``), not merely assumed from construction — this
        method must stay correct even if a future change reuses one delegate
        instance across turns instead of building a fresh one per turn."""
        if getattr(self, "_scrollback_handoff_depth", 0) > 0:
            return True
        if self._turn_ended:
            return False
        return not self._committed_scrollback_this_turn or getattr(
            self, "_awaiting_input_card_restore_anchor", False
        )

    def running_prompt_hide_input_card_chrome(self) -> bool:
        return getattr(self, "_scrollback_handoff_depth", 0) > 0 or bool(
            getattr(self, "_pending_scrollback", None)
        )

    def running_prompt_allows_text_input(self) -> bool:
        if self._current_approval_request_panel is not None:
            return False
        if self._current_question_panel is not None:
            return False
        if self._turn_ended:
            return False
        return not self._turn_ended

    def running_prompt_accepts_submission(self) -> bool:
        if self._current_approval_request_panel is not None:
            return True
        if self._current_question_panel is not None:
            return True
        return not self._turn_ended

    # -- Key handling --------------------------------------------------------

    def should_handle_running_prompt_key(self, key: str) -> bool:
        if key in {"c-o", "c-e"}:
            return self.has_expandable_panel()
        if key == "escape":
            return self._cancel_event is not None
        if self._current_approval_request_panel is not None:
            return key in {"up", "down", "enter", "1", "2", "3", "4"}
        if self._turn_ended:
            return False
        if key == "c-t":
            return bool(getattr(self, "_latest_todos", ()))
        # ↑ on empty buffer: recall last queued message.
        # Only intercept when buffer is empty — otherwise let prompt_toolkit
        # handle ↑ for cursor movement / history navigation.
        if key == "up" and self._queued_messages:
            buf = self._prompt_session._session.default_buffer  # pyright: ignore[reportPrivateUsage]
            return not buf.text.strip()
        # Ctrl+S: immediate steer
        return key == "c-s"

    def handle_running_prompt_key(self, key: str, event: KeyPressEvent) -> None:
        if key in {"c-o", "c-e"}:
            if self._has_expandable_modal_panel() or (
                self._expandable_tool_card() is None
                and self._expandable_content_block() is None
                and (
                    self._completed_expandable_tool_card() is not None
                    or self._completed_expandable_content_block() is not None
                )
            ):
                event.app.create_background_task(self._show_panel_in_pager())
            elif self._toggle_latest_tool_card():
                self._force_refresh = True
                self._flush_prompt_refresh()
            return

        # ESC during a running turn cancels the run — same contract as the
        # raw-key path in _live_view.handle_key_event. should_handle already
        # verified _cancel_event is not None.
        if key == "escape":
            if self._cancel_event is not None:
                from pythinker_code.telemetry import track

                track("cancel")
                self._cancel_event.set()
            return

        if key == "c-t":
            self.toggle_pinned_todos()
            self._force_refresh = True
            self._flush_prompt_refresh()
            return

        # ↑ on empty buffer: pop last queued message back to input for editing.
        # should_handle already verified buffer is empty.
        if key == "up" and self._queued_messages:
            buf = event.current_buffer
            recalled = self._queued_messages.pop()
            buf.document = Document(recalled.command, len(recalled.command))
            self._prompt_session.invalidate()
            return

        # Ctrl+S: immediate steer
        #   1) If input has text → steer it
        #   2) Else if queue has messages → pop first (oldest) and steer it
        if key == "c-s":
            buf = event.current_buffer
            text = buf.text.strip()
            if text:
                steer_input = self._prompt_session._build_user_input(text)  # pyright: ignore[reportPrivateUsage]
                self._clear_buffer(buf)
                self.handle_immediate_steer(steer_input)
            elif self._queued_messages:
                queued = self._queued_messages.pop(0)  # FIFO: oldest first
                self.handle_immediate_steer(queued)
                self._flush_prompt_refresh()
            return

        mapped = {
            "up": KeyEvent.UP,
            "down": KeyEvent.DOWN,
            "enter": KeyEvent.ENTER,
            "escape": KeyEvent.ESCAPE,
            "1": KeyEvent.NUM_1,
            "2": KeyEvent.NUM_2,
            "3": KeyEvent.NUM_3,
            "4": KeyEvent.NUM_4,
        }.get(key)
        if mapped is None:
            return
        if self._current_approval_request_panel is not None:
            self._clear_buffer(event.current_buffer)
        self.dispatch_keyboard_event(mapped)
        self._flush_prompt_refresh()

    async def _show_panel_in_pager(self) -> None:
        await run_in_terminal(self._show_expandable_panel_content)
        self._prompt_session.invalidate()

    @staticmethod
    def _clear_buffer(buffer: Buffer) -> None:
        if buffer.text:
            buffer.document = Document(text="", cursor_position=0)

    def _flush_prompt_refresh(self) -> None:
        if self._force_refresh:
            # Always invalidate when the caller explicitly asked for a
            # forced refresh (e.g. TurnEnd on a contentless turn where
            # neither _dirty nor _need_recompose has been set by the
            # composition pipeline). Skipping the invalidate here left
            # the prompt stale until the next composition tick.
            self._prompt_session.invalidate()
            self._dirty = False
            self._force_refresh = False
            self._need_recompose = False
            return
        if self._need_recompose:
            self._dirty = True

    def cleanup(self, is_interrupt: bool) -> None:
        super().cleanup(is_interrupt)

    def _on_question_panel_state_changed(self) -> None:
        panel = self._current_question_panel
        if panel is None:
            if self._question_modal is not None:
                self._prompt_session.detach_modal(self._question_modal)
                self._question_modal = None
            return
        if self._question_modal is None:
            self._question_modal = QuestionPromptDelegate(
                panel,
                on_advance=self._advance_question,
                on_invalidate=self._flush_prompt_refresh,
                buffer_text_provider=lambda: self._prompt_session._session.default_buffer.text,  # pyright: ignore[reportPrivateUsage]
                text_expander=self._prompt_session._get_placeholder_manager().serialize_for_history,  # pyright: ignore[reportPrivateUsage]
            )
            self._prompt_session.attach_modal(self._question_modal)
        else:
            self._question_modal.set_panel(panel)
        self._prompt_session.invalidate()

    def _advance_question(self) -> QuestionRequestPanel | None:
        """Advance to the next question in the queue, returning the new panel or None."""
        self.show_next_question_request()
        return self._current_question_panel
