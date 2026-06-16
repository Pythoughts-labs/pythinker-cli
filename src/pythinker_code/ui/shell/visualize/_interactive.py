"""Interactive prompt view for the bottom dynamic area.

``_PromptLiveView`` extends ``_LiveView`` with prompt_toolkit integration:
input routing (queue/steer/btw), modal management, and key handling.
"""

# pyright: reportPrivateUsage=false, reportUnusedClass=false

from __future__ import annotations

import asyncio
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
from pythinker_code.ui.shell.motion import reduced_motion_enabled
from pythinker_code.ui.shell.prompt import (
    CustomPromptSession,
    UserInput,
)
from pythinker_code.ui.shell.visualize._blocks import smooth_streaming_enabled
from pythinker_code.ui.shell.visualize._btw_panel import _BtwModalDelegate
from pythinker_code.ui.shell.visualize._input_router import InputAction, classify_input
from pythinker_code.ui.shell.visualize._live_view import _LiveView
from pythinker_code.ui.shell.visualize._question_panel import (
    QuestionPromptDelegate,
    QuestionRequestPanel,
)
from pythinker_code.ui.theme import tui_rich_style
from pythinker_code.utils.aioqueue import QueueShutDown
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
# Fast tick while paced streamed text is actively revealing (~25 fps) so the
# reveal animates smoothly; falls back to the status cadence when idle.
_STREAM_REVEAL_INTERVAL_S = 0.04


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
        # The interactive view owns the reveal tick (_status_refresh_loop), so it
        # is the only view that paces streamed text. Disable pacing under reduced
        # motion so motion-sensitive users get immediate reveal, not a typewriter.
        self._stream_pacing = smooth_streaming_enabled() and not reduced_motion_enabled()
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

    # -- Helpers -------------------------------------------------------------

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
                # Drain buffered paced text smoothly, even past TurnEnd, so the
                # tail flows out instead of popping when the block finally
                # commits. advance_stream_reveal() is a no-op unless a paced block
                # has backlog, so reduced-motion / unpaced turns fall straight
                # through to the calm status cadence below.
                if self.advance_stream_reveal():
                    self._prompt_session.invalidate()
                    await asyncio.sleep(_STREAM_REVEAL_INTERVAL_S)
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
                try:
                    done, _ = await asyncio.wait(
                        [wire_task, external_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if wire_task in done:
                        msg = wire_task.result()
                        wire_task = asyncio.create_task(wire.receive())
                    else:
                        msg = external_task.result()
                        external_task = asyncio.create_task(self._external_messages.get())
                except QueueShutDown:
                    msg, external_task = await self._drain_external_message_after_wire_shutdown(
                        external_task
                    )
                    if msg is not None:
                        self.dispatch_wire_message(msg)
                        self._flush_prompt_refresh()
                        continue
                    self.cleanup(is_interrupt=False)
                    self._flush_prompt_refresh()
                    break

                if isinstance(msg, StepInterrupted):
                    self.cleanup(is_interrupt=True)
                    self._flush_prompt_refresh()
                    break

                if isinstance(msg, TurnEnd):
                    self._active_turn_depth = max(0, self._active_turn_depth - 1)
                    self._turn_ended = self._active_turn_depth == 0
                    if self._turn_ended:
                        self._turn_start_time = None
                        self._pending_turn_recap = True
                    self._flush_prompt_refresh()
                    continue

                self.dispatch_wire_message(msg)
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
        # Print permanently in conversation flow with UI-only text placeholders expanded.
        console.print(render_user_echo_text(user_input.resolved_command))
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
        if self._turn_ended:
            return ANSI("")
        # Exclude the trailing verb spinner — the prompt pins it separately via
        # ``render_pinned_status_tail`` so a clipped agent stream cannot hide it.
        blocks = self.compose_agent_output(include_working_indicator=False)
        if not blocks:
            return ANSI("")
        body = render_to_ansi(Group(*blocks), columns=columns).rstrip("\n")
        return ANSI(body if body else "")

    def render_pinned_status_tail(self, columns: int) -> ANSI:
        """Render the trailing verb spinner that the prompt keeps pinned below a
        (possibly clipped) agent stream, so it stays visible above the input."""
        if self._turn_ended or self._active_turn_depth <= 0:
            return ANSI("")
        body = render_to_ansi(self._working_indicator(), columns=columns).rstrip("\n")
        return ANSI(body if body else "")

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

    def running_prompt_allows_text_input(self) -> bool:
        if self._current_approval_request_panel is not None:
            return False
        if self._current_question_panel is not None:
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
                and self._completed_expandable_tool_card() is not None
            ):
                event.app.create_background_task(self._show_panel_in_pager())
            elif self._toggle_latest_tool_card():
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
        if self._need_recompose:
            self._prompt_session.invalidate()
            self._need_recompose = False

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
