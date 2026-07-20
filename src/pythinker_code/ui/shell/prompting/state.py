"""Pure state transitions for the interactive shell prompt."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol

from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import AnyFormattedText
from prompt_toolkit.key_binding import KeyPressEvent


class PromptMode(Enum):
    AGENT = "agent"
    SHELL = "shell"

    def toggle(self) -> PromptMode:
        return PromptMode.SHELL if self is PromptMode.AGENT else PromptMode.AGENT

    def __str__(self) -> str:
        return self.value


class RunningPromptDelegate(Protocol):
    """A component that can take over the bottom prompt area."""

    modal_priority: int

    def render_running_prompt_body(self, columns: int) -> AnyFormattedText: ...

    def running_prompt_placeholder(self) -> AnyFormattedText | None: ...

    def running_prompt_allows_text_input(self) -> bool: ...

    def running_prompt_hides_input_buffer(self) -> bool: ...

    def running_prompt_accepts_submission(self) -> bool: ...

    def should_handle_running_prompt_key(self, key: str) -> bool: ...

    def handle_running_prompt_key(self, key: str, event: KeyPressEvent) -> None: ...


class PromptPhase(Enum):
    IDLE = "idle"
    TURN_STARTING = "turn_starting"
    RUNNING = "running"


@dataclass(frozen=True, slots=True)
class PromptState:
    mode: PromptMode = PromptMode.AGENT
    phase: PromptPhase = PromptPhase.IDLE
    running_delegate: RunningPromptDelegate | None = None
    modal_stack: tuple[ModalState, ...] = ()
    suspended_document: Document | None = None
    shortcut_help_open: bool = False
    running_previous_mode: PromptMode | None = None

    def __post_init__(self) -> None:
        is_running = self.phase is PromptPhase.RUNNING
        has_delegate = self.running_delegate is not None
        if is_running != has_delegate:
            raise ValueError("a running prompt must have exactly one running delegate")


@dataclass(frozen=True, slots=True)
class ModalState:
    """Reducer-owned snapshot of modal properties used for prompt transitions."""

    delegate: RunningPromptDelegate
    priority: int
    hides_input: bool


@dataclass(frozen=True, slots=True)
class TurnStarting:
    pass


@dataclass(frozen=True, slots=True)
class TurnCleared:
    pass


@dataclass(frozen=True, slots=True)
class RunningDelegateAttached:
    delegate: RunningPromptDelegate


@dataclass(frozen=True, slots=True)
class RunningDelegateDetached:
    delegate: RunningPromptDelegate


@dataclass(frozen=True, slots=True)
class ModalAttached:
    delegate: RunningPromptDelegate
    priority: int
    hides_input: bool
    document: Document


@dataclass(frozen=True, slots=True)
class ModalDetached:
    delegate: RunningPromptDelegate
    document: Document


@dataclass(frozen=True, slots=True)
class ModeChanged:
    mode: PromptMode


@dataclass(frozen=True, slots=True)
class ShortcutHelpToggled:
    open: bool | None = None


@dataclass(frozen=True, slots=True)
class BufferObserved:
    document: Document


type PromptEvent = (
    TurnStarting
    | TurnCleared
    | RunningDelegateAttached
    | RunningDelegateDetached
    | ModalAttached
    | ModalDetached
    | ModeChanged
    | ShortcutHelpToggled
    | BufferObserved
)


@dataclass(frozen=True, slots=True)
class SelectCompleter:
    mode: PromptMode


@dataclass(frozen=True, slots=True)
class SetEraseWhenDone:
    erase_when_done: bool


@dataclass(frozen=True, slots=True)
class SuspendDocument:
    document: Document


@dataclass(frozen=True, slots=True)
class RestoreDocument:
    document: Document


@dataclass(frozen=True, slots=True)
class Invalidate:
    pass


type PromptEffect = (
    SelectCompleter | SetEraseWhenDone | SuspendDocument | RestoreDocument | Invalidate
)


@dataclass(frozen=True, slots=True)
class PromptTransition:
    state: PromptState
    effects: tuple[PromptEffect, ...] = ()


def _active_modal(stack: tuple[ModalState, ...]) -> ModalState | None:
    if not stack:
        return None
    return max(enumerate(stack), key=lambda item: (item[1].priority, item[0]))[1]


def _mode_effects(mode: PromptMode) -> tuple[PromptEffect, ...]:
    return SelectCompleter(mode), SetEraseWhenDone(mode is PromptMode.AGENT), Invalidate()


def _suspend_restore_effects(
    old_active: ModalState | None,
    new_active: ModalState | None,
    suspended: Document | None,
    document: Document,
) -> tuple[Document | None, tuple[PromptEffect, ...]]:
    """Compute the suspended-document/effects transition shared by modal
    attach and detach: suspend the live input when a hides-input modal takes
    over, restore it when the last such modal leaves."""
    old_hides_input = old_active is not None and old_active.hides_input
    new_hides_input = new_active is not None and new_active.hides_input
    if not old_hides_input and new_hides_input and document.text:
        if suspended is None:
            return document, (SuspendDocument(document),)
        return suspended, ()
    if old_hides_input and not new_hides_input and suspended is not None:
        effects: tuple[PromptEffect, ...] = (
            (RestoreDocument(suspended),) if not document.text else ()
        )
        return None, effects
    return suspended, ()


def transition(state: PromptState, event: PromptEvent) -> PromptTransition:
    """Return the next prompt state and ordered facade effects without doing I/O."""
    if isinstance(event, TurnStarting):
        if state.phase is not PromptPhase.IDLE:
            return PromptTransition(state)
        return PromptTransition(replace(state, phase=PromptPhase.TURN_STARTING), (Invalidate(),))

    if isinstance(event, TurnCleared):
        if state.phase is not PromptPhase.TURN_STARTING:
            return PromptTransition(state)
        return PromptTransition(replace(state, phase=PromptPhase.IDLE), (Invalidate(),))

    if isinstance(event, RunningDelegateAttached):
        if state.running_delegate is event.delegate:
            return PromptTransition(state)
        previous_mode = state.running_previous_mode
        if state.running_delegate is None:
            previous_mode = state.mode
        next_state = replace(
            state,
            mode=PromptMode.AGENT,
            phase=PromptPhase.RUNNING,
            running_delegate=event.delegate,
            running_previous_mode=previous_mode,
        )
        return PromptTransition(next_state, _mode_effects(PromptMode.AGENT))

    if isinstance(event, RunningDelegateDetached):
        if state.running_delegate is not event.delegate:
            return PromptTransition(state)
        mode = state.running_previous_mode or state.mode
        next_state = replace(
            state,
            mode=mode,
            phase=PromptPhase.IDLE,
            running_delegate=None,
            running_previous_mode=None,
        )
        return PromptTransition(next_state, _mode_effects(mode))

    if isinstance(event, ModalAttached):
        if any(modal.delegate is event.delegate for modal in state.modal_stack):
            return PromptTransition(state)
        old_active = _active_modal(state.modal_stack)
        stack = (
            *state.modal_stack,
            ModalState(event.delegate, event.priority, event.hides_input),
        )
        new_active = _active_modal(stack)
        suspended, effects = _suspend_restore_effects(
            old_active, new_active, state.suspended_document, event.document
        )
        next_state = replace(
            state,
            modal_stack=stack,
            suspended_document=suspended,
            shortcut_help_open=False,
        )
        return PromptTransition(next_state, (*effects, Invalidate()))

    if isinstance(event, ModalDetached):
        if not any(modal.delegate is event.delegate for modal in state.modal_stack):
            return PromptTransition(state)
        old_active = _active_modal(state.modal_stack)
        stack = tuple(modal for modal in state.modal_stack if modal.delegate is not event.delegate)
        new_active = _active_modal(stack)
        suspended, effects = _suspend_restore_effects(
            old_active, new_active, state.suspended_document, event.document
        )
        next_state = replace(state, modal_stack=stack, suspended_document=suspended)
        return PromptTransition(next_state, (*effects, Invalidate()))

    if isinstance(event, ModeChanged):
        if state.mode is event.mode:
            return PromptTransition(state)
        return PromptTransition(replace(state, mode=event.mode), _mode_effects(event.mode))

    if isinstance(event, ShortcutHelpToggled):
        opened = not state.shortcut_help_open if event.open is None else event.open
        if opened is state.shortcut_help_open:
            return PromptTransition(state)
        return PromptTransition(replace(state, shortcut_help_open=opened), (Invalidate(),))

    # BufferObserved is the only remaining event type; clearing a stale
    # suspended document once the buffer is externally repopulated.
    if state.suspended_document is not None and event.document.text:
        return PromptTransition(replace(state, suspended_document=None))
    return PromptTransition(state)


__all__ = (
    "BufferObserved",
    "Invalidate",
    "ModalAttached",
    "ModalDetached",
    "ModeChanged",
    "ModalState",
    "PromptEffect",
    "PromptEvent",
    "PromptMode",
    "PromptPhase",
    "PromptState",
    "PromptTransition",
    "RestoreDocument",
    "RunningDelegateAttached",
    "RunningDelegateDetached",
    "RunningPromptDelegate",
    "SelectCompleter",
    "SetEraseWhenDone",
    "ShortcutHelpToggled",
    "SuspendDocument",
    "TurnCleared",
    "TurnStarting",
    "transition",
)
