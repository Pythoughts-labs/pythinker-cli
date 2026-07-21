# pyright: reportPrivateUsage=false
# ``PromptController`` deliberately mirrors the session's protected surface so the
# extracted binding block stays byte-for-byte behavioral with the facade internals.
"""Prompt key-binding construction, extracted from the prompt session facade.

``build_prompt_key_bindings`` owns the whole prompt_toolkit ``KeyBindings``
assembly. ``PromptController`` is the internal interface the bindings need from
the session; ``CustomPromptSession`` implements it implicitly and passes itself.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from prompt_toolkit import PromptSession
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.key_binding import KeyBindings, KeyBindingsBase, KeyPressEvent
from prompt_toolkit.keys import Keys

from pythinker_code.ui.shell.prompting.clipboard import ClipboardAdapter
from pythinker_code.ui.shell.prompting.completion.slash import discard_slash_command
from pythinker_code.ui.shell.prompting.state import PromptMode, RunningPromptDelegate
from pythinker_code.ui.shell.prompting.toasts import toast


class PromptController(Protocol):
    """Exactly the session surface the prompt key bindings dispatch into."""

    _session: PromptSession[str]
    _suppress_auto_completion: bool
    _mode: PromptMode
    _thinking: bool
    _thinking_effort: str
    _thinking_effort_cycle_callback: Callable[[], Awaitable[str | None]] | None
    _shortcut_help_open: bool
    _clipboard_adapter: ClipboardAdapter
    _clipboard_text_available: bool
    _media_clipboard_available: bool

    def _slash_completion_active(self, document: Document) -> bool: ...

    def _active_prompt_delegate(self) -> RunningPromptDelegate | None: ...

    def _uses_native_thinking(self) -> bool: ...

    def _should_handle_running_prompt_key(self, key: str) -> bool: ...

    def _handle_running_prompt_key(self, key: str, event: KeyPressEvent) -> None: ...

    def _open_in_external_editor(self, event: KeyPressEvent) -> None: ...

    def _hard_repaint(self, event: KeyPressEvent) -> None: ...

    def _handle_bracketed_paste(self, event: KeyPressEvent) -> None: ...

    def _try_paste_media(self, event: KeyPressEvent) -> bool: ...

    def _insert_pasted_text(self, buffer: Buffer, text: str) -> None: ...

    def toggle_shortcut_help(self) -> None: ...

    def toggle_mode(self) -> None: ...

    def close_shortcut_help(self) -> None: ...

    def accept_staged_suggestion_prefill(self) -> bool: ...


def build_prompt_key_bindings(controller: PromptController) -> KeyBindingsBase:
    """Build the prompt's full key-binding set against *controller*."""
    clipboard_available = controller._clipboard_text_available
    media_clipboard_available = controller._media_clipboard_available

    _kb = KeyBindings()

    def _accept_completion(buff: Buffer) -> None:
        """Accept the current or first completion, suppressing re-completion."""
        state = buff.complete_state
        if state is None:
            return
        completion = state.current_completion
        if completion is None:
            if not state.completions:
                return
            completion = state.completions[0]
        controller._suppress_auto_completion = True
        try:
            buff.apply_completion(completion)
        finally:
            controller._suppress_auto_completion = False

    def _is_slash_completion() -> bool:
        """True when the active completion menu is for a slash command."""
        buff = controller._session.default_buffer
        return bool(
            buff.complete_state
            and buff.complete_state.completions
            and controller._slash_completion_active(buff.document)
        )

    _slash_completion_filter = has_completions & Condition(_is_slash_completion)
    _non_slash_completion_filter = has_completions & ~Condition(_is_slash_completion)

    @_kb.add("enter", filter=_slash_completion_filter)
    def _(event: KeyPressEvent) -> None:
        """Slash command completion: accept and submit in one step."""
        _accept_completion(event.current_buffer)
        event.current_buffer.validate_and_handle()

    @_kb.add("escape", eager=True, filter=_slash_completion_filter)
    def _(event: KeyPressEvent) -> None:
        """Slash completion: Escape discards a draft command, or dismisses
        the argument menu when there is no draft command to remove."""
        buffer = event.current_buffer
        if not discard_slash_command(buffer):
            # Slash-argument completion (e.g. "/model gpt"): the eager
            # binding swallowed Escape but there is no root command to
            # strip, so dismiss the completion menu explicitly instead of
            # leaving it open.
            buffer.cancel_completion()
        event.app.invalidate()

    @_kb.add("enter", filter=_non_slash_completion_filter)
    def _(event: KeyPressEvent) -> None:
        """Non-slash completion (file mentions, etc.): accept only."""
        _accept_completion(event.current_buffer)

    def _has_slash_suggestion() -> bool:
        buff = controller._session.default_buffer
        return bool(buff.suggestion and buff.suggestion.text)

    @_kb.add("tab", filter=Condition(_has_slash_suggestion))
    def _(event: KeyPressEvent) -> None:
        """Slash command ghost suggestion: Tab completes the word inline."""
        suggestion = event.current_buffer.suggestion
        if suggestion and suggestion.text:
            event.current_buffer.insert_text(suggestion.text)

    @_kb.add("?", eager=True)
    def _(event: KeyPressEvent) -> None:
        """Toggle a compact shortcuts popup when the input row is empty."""
        if controller._active_prompt_delegate() is not None:
            event.current_buffer.insert_text("?")
            return
        if event.current_buffer.text.strip():
            event.current_buffer.insert_text("?")
            return
        controller.toggle_shortcut_help()

    @_kb.add("c-x", eager=True)
    def _(event: KeyPressEvent) -> None:
        if controller._active_prompt_delegate() is not None:
            return
        controller.toggle_mode()
        from pythinker_code.telemetry import track

        track("shortcut_mode_switch", to_mode=controller._mode.value)

    @_kb.add("s-tab", eager=True)
    def _(event: KeyPressEvent) -> None:
        """Cycle thinking effort with Shift+Tab."""
        if controller._active_prompt_delegate() is not None:
            return
        if controller._thinking_effort_cycle_callback is not None:

            async def _cycle() -> None:
                assert controller._thinking_effort_cycle_callback is not None
                new_level = await controller._thinking_effort_cycle_callback()
                from pythinker_code.telemetry import track

                if new_level is None:
                    message = (
                        "Current model uses native reasoning"
                        if controller._uses_native_thinking()
                        else "Current model does not support thinking"
                    )
                    toast(
                        message,
                        topic="thinking_level",
                        duration=3.0,
                        immediate=True,
                    )
                else:
                    controller._thinking_effort = new_level
                    controller._thinking = new_level != "off"
                    track("shortcut_thinking_cycle", level=new_level)
                    toast(
                        f"Thinking level: {new_level}",
                        topic="thinking_level",
                        duration=3.0,
                        immediate=True,
                    )
                event.app.invalidate()

            event.app.create_background_task(_cycle())
        event.app.invalidate()

    @_kb.add("escape", "enter", eager=True)
    @_kb.add("c-j", eager=True)
    def _(event: KeyPressEvent) -> None:
        """Insert a newline when Alt-Enter or Ctrl-J is pressed."""
        from pythinker_code.telemetry import track

        track("shortcut_newline")
        event.current_buffer.insert_text("\n")

    @_kb.add("c-o", eager=True)
    def _(event: KeyPressEvent) -> None:
        """Expand active transcript content, or open current buffer in external editor."""
        if controller._active_prompt_delegate() is not None:
            if controller._should_handle_running_prompt_key("c-o"):
                controller._handle_running_prompt_key("c-o", event)
            return

        from pythinker_code.telemetry import track

        track("shortcut_editor")
        controller._open_in_external_editor(event)

    @_kb.add("c-l", eager=True)
    def _(event: KeyPressEvent) -> None:
        """Erase and fully repaint the screen (recovery from console damage)."""
        controller._hard_repaint(event)

    def _has_staged_suggestion_prefill() -> bool:
        return bool(getattr(controller, "_staged_suggestion_prefill", None))

    @_kb.add("escape", "s", eager=True, filter=Condition(_has_staged_suggestion_prefill))
    def _(event: KeyPressEvent) -> None:
        """Accept the latest agent suggestion into the prompt buffer."""
        if controller.accept_staged_suggestion_prefill():
            from pythinker_code.telemetry import track

            track("suggestion_accepted")
        event.app.invalidate()

    @_kb.add(
        "up",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("up")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("up", event)

    @_kb.add(
        "down",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("down")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("down", event)

    @_kb.add(
        "left",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("left")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("left", event)

    @_kb.add(
        "right",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("right")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("right", event)

    @_kb.add(
        "tab",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("tab")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("tab", event)

    @_kb.add(
        "enter",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("enter")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("enter", event)

    @_kb.add(
        "space",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("space")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("space", event)

    @_kb.add(
        "c-s",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("c-s")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("c-s", event)

    @_kb.add(
        "c-e",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("c-e")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("c-e", event)

    @_kb.add(
        "c-t",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("c-t")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("c-t", event)

    @_kb.add(
        "c-c",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("c-c")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("c-c", event)

    @_kb.add(
        "c-d",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("c-d")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("c-d", event)

    @_kb.add(
        "escape",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("escape")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("escape", event)

    @_kb.add(
        "escape",
        eager=True,
        filter=Condition(lambda: controller._shortcut_help_open),
    )
    def _(event: KeyPressEvent) -> None:
        controller.close_shortcut_help()

    @_kb.add(
        "1",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("1")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("1", event)

    @_kb.add(
        "2",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("2")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("2", event)

    @_kb.add(
        "3",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("3")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("3", event)

    @_kb.add(
        "4",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("4")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("4", event)

    @_kb.add(
        "5",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("5")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("5", event)

    @_kb.add(
        "6",
        eager=True,
        filter=Condition(lambda: controller._should_handle_running_prompt_key("6")),
    )
    def _(event: KeyPressEvent) -> None:
        controller._handle_running_prompt_key("6", event)

    @_kb.add(Keys.BracketedPaste, eager=True)
    def _(event: KeyPressEvent) -> None:
        controller._handle_bracketed_paste(event)

    if clipboard_available or media_clipboard_available:

        @_kb.add("c-v", eager=True)
        def _(event: KeyPressEvent) -> None:
            from pythinker_code.telemetry import track

            track("shortcut_paste")
            if controller._try_paste_media(event):
                return
            if clipboard_available:
                clipboard_text = controller._clipboard_adapter.paste_text(event.app.clipboard)
                if clipboard_text is None:
                    return
                controller._insert_pasted_text(event.current_buffer, clipboard_text)
                event.app.invalidate()

    return _kb
