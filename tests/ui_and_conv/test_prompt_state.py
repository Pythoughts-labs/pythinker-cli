from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest
from prompt_toolkit.document import Document

from pythinker_code.ui.shell.prompt import CustomPromptSession
from pythinker_code.ui.shell.prompting.state import (
    BufferObserved,
    Invalidate,
    ModalAttached,
    ModalDetached,
    ModalState,
    ModeChanged,
    PromptMode,
    PromptPhase,
    PromptState,
    RestoreDocument,
    RunningDelegateAttached,
    RunningDelegateDetached,
    SelectCompleter,
    SetEraseWhenDone,
    ShortcutHelpToggled,
    SuspendDocument,
    TurnCleared,
    TurnStarting,
    transition,
)


class _Delegate:
    def __init__(self, *, priority: int = 0, hides_input: bool = False) -> None:
        self.modal_priority = priority
        self._hides_input = hides_input
        self.visibility_queries = 0

    def running_prompt_hides_input_buffer(self) -> bool:
        self.visibility_queries += 1
        return self._hides_input

    def render_running_prompt_body(self, columns: int) -> str:
        return str(columns)

    def running_prompt_placeholder(self) -> None:
        return None

    def running_prompt_allows_text_input(self) -> bool:
        return not self._hides_input

    def running_prompt_accepts_submission(self) -> bool:
        return False

    def should_handle_running_prompt_key(self, key: str) -> bool:
        return False

    def handle_running_prompt_key(self, key: str, event: Any) -> None:
        return None


def _modal_attached(delegate: _Delegate, document: Document | None = None) -> ModalAttached:
    return ModalAttached(
        delegate=delegate,
        priority=delegate.modal_priority,
        hides_input=delegate._hides_input,
        document=document or Document(),
    )


def test_state_and_transition_are_frozen() -> None:
    result = transition(PromptState(), TurnStarting())

    with pytest.raises(FrozenInstanceError):
        result.state.phase = PromptPhase.IDLE  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.effects = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    ("phase", "delegate"),
    [
        (PromptPhase.IDLE, _Delegate()),
        (PromptPhase.TURN_STARTING, _Delegate()),
        (PromptPhase.RUNNING, None),
    ],
)
def test_phase_and_running_delegate_invariant(
    phase: PromptPhase, delegate: _Delegate | None
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        PromptState(phase=phase, running_delegate=delegate)


def test_turn_starting_and_clear_transition_table() -> None:
    idle = PromptState()
    starting = transition(idle, TurnStarting())
    assert starting.state.phase is PromptPhase.TURN_STARTING
    assert starting.effects == (Invalidate(),)

    repeated_start = transition(starting.state, TurnStarting())
    assert repeated_start.state is starting.state
    assert repeated_start.effects == ()

    cleared = transition(starting.state, TurnCleared())
    assert cleared.state.phase is PromptPhase.IDLE
    assert cleared.effects == (Invalidate(),)

    for state in (idle, PromptState(phase=PromptPhase.RUNNING, running_delegate=_Delegate())):
        stale_clear = transition(state, TurnCleared())
        assert stale_clear.state is state
        assert stale_clear.effects == ()

    running = PromptState(phase=PromptPhase.RUNNING, running_delegate=_Delegate())
    stale_start = transition(running, TurnStarting())
    assert stale_start.state is running
    assert stale_start.effects == ()


def test_running_delegate_transition_table_and_effect_order() -> None:
    first = _Delegate()
    replacement = _Delegate()
    stale = _Delegate()
    idle = PromptState(mode=PromptMode.SHELL)

    attached = transition(idle, RunningDelegateAttached(first))
    assert attached.state.phase is PromptPhase.RUNNING
    assert attached.state.running_delegate is first
    assert attached.state.running_previous_mode is PromptMode.SHELL
    assert attached.effects == (
        SelectCompleter(PromptMode.AGENT),
        SetEraseWhenDone(True),
        Invalidate(),
    )

    duplicate = transition(attached.state, RunningDelegateAttached(first))
    assert duplicate.state is attached.state
    assert duplicate.effects == ()

    stale_detach = transition(attached.state, RunningDelegateDetached(stale))
    assert stale_detach.state is attached.state
    assert stale_detach.effects == ()

    replaced = transition(attached.state, RunningDelegateAttached(replacement))
    assert replaced.state.running_delegate is replacement
    assert replaced.state.running_previous_mode is PromptMode.SHELL
    assert replaced.effects == (
        SelectCompleter(PromptMode.AGENT),
        SetEraseWhenDone(True),
        Invalidate(),
    )

    detached = transition(replaced.state, RunningDelegateDetached(replacement))
    assert detached.state == PromptState(mode=PromptMode.SHELL)
    assert detached.effects == (
        SelectCompleter(PromptMode.SHELL),
        SetEraseWhenDone(False),
        Invalidate(),
    )


def test_running_delegate_attaches_from_turn_starting() -> None:
    delegate = _Delegate()
    starting = PromptState(mode=PromptMode.SHELL, phase=PromptPhase.TURN_STARTING)

    attached = transition(starting, RunningDelegateAttached(delegate))

    assert attached.state.phase is PromptPhase.RUNNING
    assert attached.state.running_delegate is delegate
    assert attached.state.running_previous_mode is PromptMode.SHELL
    assert attached.effects == (
        SelectCompleter(PromptMode.AGENT),
        SetEraseWhenDone(True),
        Invalidate(),
    )


def test_mode_changed_transition_table_and_effect_order() -> None:
    agent = PromptState()
    unchanged = transition(agent, ModeChanged(PromptMode.AGENT))
    assert unchanged.state is agent
    assert unchanged.effects == ()

    changed = transition(agent, ModeChanged(PromptMode.SHELL))
    assert changed.state.mode is PromptMode.SHELL
    assert changed.effects == (
        SelectCompleter(PromptMode.SHELL),
        SetEraseWhenDone(False),
        Invalidate(),
    )


@pytest.mark.parametrize("phase", [PromptPhase.IDLE, PromptPhase.TURN_STARTING])
def test_mode_changed_preserves_non_running_phase(phase: PromptPhase) -> None:
    state = PromptState(phase=phase)

    changed = transition(state, ModeChanged(PromptMode.SHELL))

    assert changed.state.phase is phase
    assert changed.state.mode is PromptMode.SHELL
    assert changed.effects == (
        SelectCompleter(PromptMode.SHELL),
        SetEraseWhenDone(False),
        Invalidate(),
    )


def test_mode_changed_while_running_preserves_delegate_and_previous_mode() -> None:
    delegate = _Delegate()
    state = PromptState(
        phase=PromptPhase.RUNNING,
        running_delegate=delegate,
        running_previous_mode=PromptMode.SHELL,
    )

    changed = transition(state, ModeChanged(PromptMode.SHELL))

    assert changed.state.phase is PromptPhase.RUNNING
    assert changed.state.running_delegate is delegate
    assert changed.state.running_previous_mode is PromptMode.SHELL
    assert changed.effects == (
        SelectCompleter(PromptMode.SHELL),
        SetEraseWhenDone(False),
        Invalidate(),
    )

    unchanged = transition(changed.state, ModeChanged(PromptMode.SHELL))
    assert unchanged.state is changed.state
    assert unchanged.effects == ()


def test_shortcut_help_transition_table() -> None:
    closed = PromptState()
    opened = transition(closed, ShortcutHelpToggled())
    assert opened.state.shortcut_help_open is True
    assert opened.effects == (Invalidate(),)

    already_open = transition(opened.state, ShortcutHelpToggled(open=True))
    assert already_open.state is opened.state
    assert already_open.effects == ()

    closed_again = transition(opened.state, ShortcutHelpToggled(open=False))
    assert closed_again.state.shortcut_help_open is False
    assert closed_again.effects == (Invalidate(),)

    already_closed = transition(closed_again.state, ShortcutHelpToggled(open=False))
    assert already_closed.state is closed_again.state
    assert already_closed.effects == ()


def test_modal_priority_latest_tie_break_duplicate_and_purity() -> None:
    visible_high = _Delegate(priority=20)
    hidden_low = _Delegate(priority=10, hides_input=True)
    hidden_tie = _Delegate(priority=20, hides_input=True)
    document = Document("draft")

    visible = transition(PromptState(), _modal_attached(visible_high, document))
    low = transition(visible.state, _modal_attached(hidden_low, document))
    assert not any(isinstance(effect, SuspendDocument) for effect in low.effects)

    tie = transition(low.state, _modal_attached(hidden_tie, document))
    assert tie.effects == (SuspendDocument(document), Invalidate())
    assert tuple(modal.delegate for modal in tie.state.modal_stack) == (
        visible_high,
        hidden_low,
        hidden_tie,
    )
    assert visible_high.visibility_queries == 0
    assert hidden_low.visibility_queries == 0
    assert hidden_tie.visibility_queries == 0

    duplicate = transition(tie.state, _modal_attached(hidden_tie, document))
    assert duplicate.state is tie.state
    assert duplicate.effects == ()


def test_hidden_modal_suspends_and_restores_document_in_effect_order() -> None:
    modal = _Delegate(priority=1, hides_input=True)
    document = Document("draft", cursor_position=5)
    attached = transition(PromptState(), _modal_attached(modal, document))
    assert attached.state.suspended_document == document
    assert attached.effects == (SuspendDocument(document), Invalidate())

    detached = transition(attached.state, ModalDetached(modal, Document()))
    assert detached.effects == (RestoreDocument(document), Invalidate())
    assert detached.state.suspended_document is None

    stale = transition(detached.state, ModalDetached(modal, Document()))
    assert stale.state is detached.state
    assert stale.effects == ()


def test_hidden_modal_with_empty_document_does_not_suspend() -> None:
    modal = _Delegate(priority=1, hides_input=True)

    attached = transition(PromptState(), _modal_attached(modal, Document()))

    assert attached.state.suspended_document is None
    assert attached.effects == (Invalidate(),)


def test_hidden_modal_detach_drops_suspension_when_buffer_is_non_empty() -> None:
    modal = _Delegate(priority=1, hides_input=True)
    attached = transition(PromptState(), _modal_attached(modal, Document("original")))

    detached = transition(attached.state, ModalDetached(modal, Document("replacement")))

    assert detached.state.suspended_document is None
    assert detached.effects == (Invalidate(),)


def test_modal_active_visibility_transitions_in_both_directions() -> None:
    hidden = _Delegate(priority=10, hides_input=True)
    visible = _Delegate(priority=20)
    draft = Document("draft")

    hidden_active = transition(PromptState(), _modal_attached(hidden, draft))
    visible_active = transition(hidden_active.state, _modal_attached(visible, Document()))
    assert visible_active.effects == (RestoreDocument(draft), Invalidate())
    assert visible_active.state.suspended_document is None

    hidden_revealed = transition(
        visible_active.state,
        ModalDetached(visible, draft),
    )
    assert hidden_revealed.effects == (SuspendDocument(draft), Invalidate())
    assert hidden_revealed.state.suspended_document == draft


def test_detaching_inactive_modal_keeps_active_visibility() -> None:
    hidden_low = _Delegate(priority=10, hides_input=True)
    visible_high = _Delegate(priority=20)
    document = Document("draft")
    low = transition(PromptState(), _modal_attached(hidden_low, document))
    high = transition(low.state, _modal_attached(visible_high, Document()))

    detached = transition(high.state, ModalDetached(hidden_low, document))

    assert detached.state.modal_stack == (ModalState(visible_high, 20, False),)
    assert detached.state.suspended_document is None
    assert detached.effects == (Invalidate(),)


def test_modal_attach_closes_shortcut_help() -> None:
    state = transition(PromptState(), ShortcutHelpToggled(open=True)).state
    attached = transition(state, _modal_attached(_Delegate()))
    assert attached.state.shortcut_help_open is False
    assert attached.effects == (Invalidate(),)


def test_buffer_observed_transition_table_prevents_stale_restore() -> None:
    modal = _Delegate(priority=1, hides_input=True)
    state = transition(PromptState(), _modal_attached(modal, Document("original"))).state

    empty = transition(state, BufferObserved(Document()))
    assert empty.state is state
    assert empty.effects == ()

    observed = transition(state, BufferObserved(Document("replacement")))
    assert observed.state.suspended_document is None
    assert observed.effects == ()

    repeated = transition(observed.state, BufferObserved(Document("replacement")))
    assert repeated.state is observed.state
    assert repeated.effects == ()

    detached = transition(observed.state, ModalDetached(modal, Document("replacement")))
    assert detached.effects == (Invalidate(),)
    assert not any(isinstance(effect, RestoreDocument) for effect in detached.effects)


def test_session_dispatch_keeps_reducer_modal_snapshot_authoritative() -> None:
    delegate = _Delegate(priority=1, hides_input=False)
    session = object.__new__(CustomPromptSession)
    facade = cast(Any, session)
    facade._prompt_state = PromptState(modal_stack=(ModalState(delegate, 1, False),))
    facade._modal_delegates = [delegate]
    facade._mode = PromptMode.AGENT
    facade._shortcut_help_open = False
    facade._turn_starting = False
    facade._running_prompt_delegate = None
    facade._running_prompt_previous_mode = None
    facade._suspended_buffer_document = None
    invalidations = 0

    def invalidate() -> None:
        nonlocal invalidations
        invalidations += 1

    facade.invalidate = invalidate
    delegate._hides_input = True

    session.toggle_shortcut_help()

    assert session._prompt_state.modal_stack == (ModalState(delegate, 1, False),)
    assert session._prompt_state.suspended_document is None
    assert delegate.visibility_queries == 0
    assert invalidations == 1
