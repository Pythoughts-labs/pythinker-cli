import asyncio
import importlib
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

import pytest
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from rich.text import Text

from pythinker_code.tools.display import TodoDisplayItem
from pythinker_code.ui.shell.motion import _SHIMMER_BASE, _SHIMMER_HIGHLIGHT, _SHIMMER_MID
from pythinker_code.ui.shell.prompt import BgTaskCounts, CustomPromptSession, PromptMode, UserInput
from pythinker_code.ui.shell.spacing import PREAMBLE_EARLIER_OUTPUT_HIDDEN_HINT
from pythinker_code.wire.types import (
    ApprovalRequest,
    StatusUpdate,
    SteerInput,
    TextPart,
    TurnBegin,
    TurnEnd,
)

shell_visualize = importlib.import_module("pythinker_code.ui.shell.visualize")
# Sub-modules for monkeypatching internal names (Live, _keyboard_listener, console)
_live_view_mod = importlib.import_module("pythinker_code.ui.shell.visualize._live_view")
_interactive_mod = importlib.import_module("pythinker_code.ui.shell.visualize._interactive")
_LiveView = shell_visualize._LiveView
_PromptLiveView = shell_visualize._PromptLiveView


@pytest.mark.asyncio
async def test_visualize_uses_prompt_live_view_when_prompt_session_and_steer_are_provided(
    monkeypatch,
) -> None:
    called: list[tuple[str, object, object]] = []
    bound: list[tuple[object, object]] = []
    unbound: list[object] = []

    class _PromptSession:
        def attach_running_prompt(self, delegate) -> None:
            called.append(("attach", delegate, None))

        def detach_running_prompt(self, delegate) -> None:
            called.append(("detach", delegate, None))

    class _DummyPromptLiveView:
        def __init__(
            self,
            initial_status,
            *,
            prompt_session,
            steer,
            btw_runner=None,
            shell_command_runner=None,
            cancel_event,
            show_thinking_stream=False,
            show_turn_recaps=False,
        ):
            called.append(("init", initial_status, cancel_event))
            assert prompt_session is not None
            assert steer is not None
            self.handle_local_input = lambda user_input: None

        async def visualize_loop(self, wire) -> None:
            called.append(("loop", wire, None))

    def _unexpected_live_view(*args, **kwargs):
        raise AssertionError("_LiveView should not be used")

    monkeypatch.setattr(shell_visualize, "_PromptLiveView", _DummyPromptLiveView)
    monkeypatch.setattr(shell_visualize, "_LiveView", _unexpected_live_view)

    status = StatusUpdate(context_usage=0.1)
    wire = cast(Any, object())

    await shell_visualize.visualize(
        wire,
        initial_status=status,
        cancel_event=asyncio.Event(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _: None,
        bind_running_input=lambda on_input, on_interrupt: bound.append((on_input, on_interrupt)),
        unbind_running_input=lambda: unbound.append(True),
    )

    assert [entry[0] for entry in called] == ["init", "attach", "loop", "detach"]
    assert called[2] == ("loop", wire, None)
    assert len(bound) == 1
    assert unbound == [True]


def test_render_agent_status_uses_compose_agent_output_not_compose() -> None:
    """render_agent_status() must call compose_agent_output(), NOT compose().

    This ensures approval/question panels are not double-rendered when a modal
    delegate is active (they are rendered in Layer 2 by the modal, not Layer 1).
    """
    view = object.__new__(_PromptLiveView)
    view._turn_ended = False

    agent_calls: list[bool] = []
    compose_calls: list[bool] = []

    def fake_compose_agent_output(
        *,
        include_working_indicator: bool = True,
        include_content_activity: bool = True,
    ):
        agent_calls.append(True)
        return [Text("agent-status")]

    def fake_compose(*, include_status: bool = True):
        compose_calls.append(True)
        return Text("full-compose")

    view.compose_agent_output = fake_compose_agent_output
    view.compose = fake_compose

    rendered = view.render_agent_status(80)

    assert agent_calls == [True], "compose_agent_output() should be called"
    assert compose_calls == [], "compose() should NOT be called"
    assert "agent-status" in rendered.value


@pytest.mark.asyncio
async def test_prompt_final_scrollback_invalidates_after_transient_block_detached(
    monkeypatch,
) -> None:
    """Prompt mode must detach the content block and flush it via run_in_terminal."""
    from pythinker_code.ui.shell.visualize._blocks import _ContentBlock

    printed: list[object] = []
    invalidation_saw_detached_block: list[bool] = []
    view_holder: dict[str, _PromptLiveView] = {}

    class _PromptSession:
        def invalidate(self) -> None:
            invalidation_saw_detached_block.append(
                view_holder["view"]._current_content_block is None
            )

    async def _run_in_terminal(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        func()

    monkeypatch.setattr(_interactive_mod, "run_in_terminal", _run_in_terminal)
    monkeypatch.setattr(_live_view_mod.console, "_force_terminal", True)
    monkeypatch.setattr(
        _live_view_mod.console,
        "print",
        lambda *args, **kwargs: printed.extend(args) if args else None,
    )

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    view_holder["view"] = view
    view._current_content_block = _ContentBlock(is_think=False)
    view._current_content_block.append("final prompt text")

    view.flush_content()
    await view._flush_pending_scrollback()

    # The handoff invalidates around the run_in_terminal teardown; every
    # invalidation must observe the block already detached (no torn live frame).
    assert invalidation_saw_detached_block
    assert all(invalidation_saw_detached_block)
    assert len(printed) == 1


@pytest.mark.asyncio
async def test_prompt_incremental_scrollback_uses_terminal_handoff(monkeypatch) -> None:
    """Committed prompt-path blocks must print while prompt_toolkit is suspended."""
    from pythinker_code.ui.shell.visualize._blocks import _ContentBlock

    invalidations: list[str] = []
    printed: list[object] = []
    terminal_handoffs: list[str] = []

    class _PromptSession:
        def invalidate(self) -> None:
            invalidations.append("invalidate")

    async def _run_in_terminal(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        terminal_handoffs.append("run")
        func()

    monkeypatch.setattr(_interactive_mod, "run_in_terminal", _run_in_terminal)
    monkeypatch.setattr(_live_view_mod.console, "_force_terminal", True)
    monkeypatch.setattr(
        _live_view_mod,
        "emit_scrollback_block",
        lambda _console, renderable: printed.append(renderable),
    )

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    block = _ContentBlock(is_think=False)
    block.append("First paragraph.\n\nMutable tail")
    assert block._committed_renderables
    view._current_content_block = block

    emitted = await view._emit_incremental_content_commits()

    assert emitted is True
    assert terminal_handoffs == ["run"]
    assert printed
    assert invalidations  # prompt is invalidated around the terminal handoff


def test_status_loop_has_no_midstream_commit_throttle() -> None:
    """Regression guard: the per-tick mid-stream commit (the source of the
    run_in_terminal "jump") must not come back. Completed prose stays in the
    in-place live preview and flushes once at a transition / turn end."""
    assert not hasattr(_PromptLiveView, "_maybe_emit_incremental_commits")


@pytest.mark.asyncio
async def test_streaming_does_not_commit_to_scrollback_until_turn_end(monkeypatch) -> None:
    """During an active stream, completed paragraphs are NOT handed to scrollback
    (no run_in_terminal teardown / jump); the whole block flushes exactly once at
    turn end."""
    from pythinker_code.ui.shell.visualize._blocks import _ContentBlock

    printed: list[object] = []

    class _PromptSession:
        def invalidate(self) -> None:
            pass

    async def _run_in_terminal(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        func()

    monkeypatch.setattr(_interactive_mod, "run_in_terminal", _run_in_terminal)
    monkeypatch.setattr(
        _live_view_mod,
        "emit_scrollback_block",
        lambda _console, renderable: printed.append(renderable),
    )

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    block = _ContentBlock(is_think=False)
    block.append("Paragraph one.\n\nParagraph two.\n\ntail")
    assert block._committed_renderables  # paragraph boundaries are committable
    view._current_content_block = block

    # The status-loop body (reveal + pending-scrollback drain) commits nothing
    # mid-stream: the incremental scrollback path never fires, prose stays in the
    # live preview, and nothing is queued for handoff.
    view.advance_stream_reveal()
    await view._flush_pending_scrollback()
    assert printed == []  # _emit_incremental_scrollback (mid-stream path) never ran
    assert view._pending_scrollback == []
    assert block._committed_renderables  # retained in the in-place preview

    # Turn end detaches the block and queues the whole thing for a single handoff.
    view.flush_content()
    assert view._current_content_block is None
    assert view._pending_scrollback  # full block queued exactly once
    await view._flush_pending_scrollback()
    assert view._pending_scrollback == []  # drained


def test_render_pinned_status_tail_returns_spinner_when_turn_active() -> None:
    import time as _time

    view = object.__new__(_PromptLiveView)
    view._turn_ended = False
    view._active_turn_depth = 1
    view._turn_start_time = _time.monotonic()
    view._current_question_panel = None
    view._current_approval_request_panel = None

    out = view.render_pinned_status_tail(80)
    assert out.value.strip() != ""


def _card_session(
    *, text: str = "", turn_starting: bool = False, delegate: object | None = None
) -> CustomPromptSession:
    """A CustomPromptSession stub with exactly the attrs the input-card gate reads
    via direct access (so an init regression would fail loudly, not silently)."""
    from types import SimpleNamespace

    session = object.__new__(CustomPromptSession)
    session._modal_delegates = []
    session._turn_starting = turn_starting
    session._running_prompt_delegate = cast(Any, delegate)
    session._session = cast(Any, SimpleNamespace(default_buffer=SimpleNamespace(text=text)))
    return session


def _hiding_delegate(hide: bool) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(running_prompt_hide_input_card=lambda: hide)


def test_input_card_pre_attach_hides_until_delegate_can_pin_spinner() -> None:
    """The pre-attach race frame hides the card so it cannot fossilize above
    the spinner before the running-prompt delegate exists."""
    session = _card_session(turn_starting=True, delegate=None)
    assert session._input_card_hidden_pre_stream() is True
    assert session._should_render_input_buffer() is False


def test_input_card_pre_first_commit_keeps_prompt_row_via_delegate() -> None:
    """Post-attach: the delegate gates editable content until the first commit,
    but the prompt marker row still renders."""
    session = _card_session(delegate=_hiding_delegate(True))
    assert session._input_card_hidden_pre_stream() is True
    assert session._should_render_input_buffer() is True


def test_sticky_input_still_hides_pre_attach_buffer_window() -> None:
    session = _card_session(turn_starting=True, delegate=None)
    session._sticky_input = True

    assert session._input_card_hidden_pre_stream() is True
    assert session._should_render_input_buffer() is False


def test_sticky_input_keeps_delegate_prompt_marker_after_attach() -> None:
    session = _card_session(delegate=_hiding_delegate(True))
    session._sticky_input = True

    assert session._input_card_hidden_pre_stream() is True
    assert session._should_render_input_buffer() is True


def test_input_card_shown_after_first_commit() -> None:
    """Once the turn has committed, the delegate stops hiding and the card
    repaints so the user can see where to steer."""
    session = _card_session(delegate=_hiding_delegate(False))
    assert session._input_card_hidden_pre_stream() is False
    assert session._should_render_input_buffer() is True


def test_input_card_shown_once_user_types_to_steer() -> None:
    """A non-empty buffer (the user typed to steer) always shows the card, even
    while the delegate would otherwise hide it."""
    session = _card_session(text="steer this", delegate=_hiding_delegate(True))
    assert session._input_card_hidden_pre_stream() is False
    assert session._should_render_input_buffer() is True


def test_input_card_shown_when_idle_between_turns() -> None:
    session = _card_session(turn_starting=False, delegate=None)
    assert session._input_card_hidden_pre_stream() is False
    assert session._should_render_input_buffer() is True


def test_running_prompt_hide_input_card_flips_on_first_commit() -> None:
    """The delegate hides the card until the turn's first commit, then shows it;
    a finalizing/ended turn always shows it."""
    view = object.__new__(_PromptLiveView)
    view._turn_ended = False
    view._committed_scrollback_this_turn = False
    assert view.running_prompt_hide_input_card() is True

    view._committed_scrollback_this_turn = True
    assert view.running_prompt_hide_input_card() is False

    view._committed_scrollback_this_turn = False
    view._turn_ended = True
    assert view.running_prompt_hide_input_card() is False


def test_mark_turn_starting_is_idempotent_and_cleared_on_attach_detach() -> None:
    """The shell sets the hint on dispatch (once — idempotent); attach (delegate
    takes over) and detach (turn ended / error-before-attach) both clear it so
    the idle prompt is never left collapsed."""
    session = object.__new__(CustomPromptSession)
    session._turn_starting = False
    invalidations: list[int] = []
    session.invalidate = lambda: invalidations.append(1)  # type: ignore[method-assign]

    session.mark_turn_starting()
    assert session._turn_starting is True
    assert len(invalidations) == 1  # repaint requested once
    session.mark_turn_starting()  # idempotent: no extra repaint
    assert len(invalidations) == 1

    # attach clears the hint (delegate becomes source of truth)
    session._running_prompt_delegate = None
    session._running_prompt_previous_mode = None
    session._mode = PromptMode.AGENT
    session._apply_mode = lambda: None  # type: ignore[method-assign]
    delegate = object()
    session.attach_running_prompt(cast(Any, delegate))
    assert session._turn_starting is False

    # detach also clears it (belt-and-suspenders for the error-before-attach path)
    session._turn_starting = True
    session.detach_running_prompt(cast(Any, delegate))
    assert session._turn_starting is False


def test_sticky_input_turn_start_enables_fullscreen_once() -> None:
    from types import SimpleNamespace

    session = object.__new__(CustomPromptSession)
    app = SimpleNamespace(full_screen=False, erase_when_done=True)
    session._session = cast(Any, SimpleNamespace(app=app, default_buffer=SimpleNamespace(text="")))
    session._sticky_input = True
    session._previous_full_screen = None
    session._turn_starting = False
    invalidations: list[int] = []
    session.invalidate = lambda: invalidations.append(1)  # type: ignore[method-assign]

    session.mark_turn_starting()
    session.mark_turn_starting()

    assert app.full_screen is True
    assert app.erase_when_done is False
    assert session._previous_full_screen is False
    assert len(invalidations) == 1


def test_sticky_input_clear_turn_starting_restores_fullscreen_on_pre_attach_error() -> None:
    from types import SimpleNamespace

    session = object.__new__(CustomPromptSession)
    app = SimpleNamespace(full_screen=True, erase_when_done=False)
    session._session = cast(Any, SimpleNamespace(app=app, default_buffer=SimpleNamespace(text="")))
    session._sticky_input = True
    session._previous_full_screen = False
    session._turn_starting = True

    session.clear_turn_starting()

    assert session._turn_starting is False
    assert app.full_screen is False


def test_clear_turn_starting_is_the_public_api_for_belt_and_suspenders_cleanup() -> None:
    """The shell's run_soul_command finally block must clear a stale hint on an
    error-before-attach path without reaching into the private ``_turn_starting``
    attribute — this is the public method it calls instead."""
    session = object.__new__(CustomPromptSession)
    session._turn_starting = True

    session.clear_turn_starting()
    assert session._turn_starting is False

    # Idempotent by construction (plain assignment): a repeat call is harmless.
    session.clear_turn_starting()
    assert session._turn_starting is False


def test_render_agent_prompt_message_keeps_prompt_marker_when_card_gate_hides_buffer(
    monkeypatch,
) -> None:
    """The chrome renderer keeps the prompt marker while hiding the editable buffer."""
    from types import SimpleNamespace

    from prompt_toolkit.formatted_text import FormattedText

    import pythinker_code.ui.shell.prompt as prompt_module
    from pythinker_code.ui.shell.prompt import PROMPT_SYMBOL_AGENT_INPUT

    border = "──────── ● off"
    session = object.__new__(CustomPromptSession)
    session._modal_delegates = []
    session._shortcut_help_open = False
    session._turn_starting = False
    monkeypatch.setattr(session, "_render_agent_status", lambda _c: FormattedText())
    monkeypatch.setattr(session, "_render_interactive_body", lambda _c: FormattedText())
    monkeypatch.setattr(session, "_render_pinned_status_tail", lambda _c: FormattedText())
    monkeypatch.setattr(session, "_render_input_top_border", lambda _c, _f: [("", border)])
    monkeypatch.setattr(prompt_module, "is_card_style", lambda: True)
    monkeypatch.setattr(prompt_module, "get_toolbar_colors", lambda: SimpleNamespace(separator=""))

    def _rendered(hidden: bool) -> str:
        monkeypatch.setattr(session, "_input_card_hidden_pre_stream", lambda: hidden)
        return "".join(text for _style, text, *_ in session._render_agent_prompt_message())

    hidden_frame = _rendered(True)
    assert hidden_frame == f"{border}\n  {PROMPT_SYMBOL_AGENT_INPUT} "

    shown_frame = _rendered(False)
    assert border in shown_frame
    assert PROMPT_SYMBOL_AGENT_INPUT in shown_frame


def test_render_agent_prompt_message_keeps_prompt_marker_when_delegate_hides_buffer(
    monkeypatch,
) -> None:
    """The post-attach running frame keeps the prompt marker."""
    from types import SimpleNamespace

    from prompt_toolkit.formatted_text import FormattedText

    import pythinker_code.ui.shell.prompt as prompt_module
    from pythinker_code.ui.shell.prompt import PROMPT_SYMBOL_AGENT_INPUT

    border = "──────── ● off"
    session = _card_session(delegate=_hiding_delegate(True))
    session._shortcut_help_open = False
    monkeypatch.setattr(session, "_render_agent_status", lambda _c: FormattedText())
    monkeypatch.setattr(session, "_render_interactive_body", lambda _c: FormattedText())
    monkeypatch.setattr(session, "_render_pinned_status_tail", lambda _c: FormattedText())
    monkeypatch.setattr(session, "_render_input_top_border", lambda _c, _f: [("", border)])
    monkeypatch.setattr(prompt_module, "is_card_style", lambda: True)
    monkeypatch.setattr(prompt_module, "get_toolbar_colors", lambda: SimpleNamespace(separator=""))

    frame = "".join(text for _style, text, *_ in session._render_agent_prompt_message())

    assert frame == f"{border}\n  {PROMPT_SYMBOL_AGENT_INPUT} "


def test_render_agent_prompt_message_keeps_prompt_marker_when_live_view_hides_buffer(
    monkeypatch,
) -> None:
    """Starting a turn keeps the input-card chrome even before first commit."""
    from types import SimpleNamespace

    from prompt_toolkit.formatted_text import FormattedText

    import pythinker_code.ui.shell.prompt as prompt_module
    from pythinker_code.ui.shell.prompt import PROMPT_SYMBOL_AGENT_INPUT

    border = "──────── ● off"
    view = object.__new__(_PromptLiveView)
    view._scrollback_handoff_depth = 0
    view._turn_ended = False
    view._committed_scrollback_this_turn = False

    session = _card_session(delegate=view)
    session._shortcut_help_open = False
    monkeypatch.setattr(session, "_render_agent_status", lambda _c: FormattedText())
    monkeypatch.setattr(session, "_render_interactive_body", lambda _c: FormattedText())
    monkeypatch.setattr(session, "_render_pinned_status_tail", lambda _c: FormattedText())
    monkeypatch.setattr(session, "_render_input_top_border", lambda _c, _f: [("", border)])
    monkeypatch.setattr(prompt_module, "is_card_style", lambda: True)
    monkeypatch.setattr(prompt_module, "get_toolbar_colors", lambda: SimpleNamespace(separator=""))

    frame = "".join(text for _style, text, *_ in session._render_agent_prompt_message())

    assert frame == f"{border}\n  {PROMPT_SYMBOL_AGENT_INPUT} "


def test_prompt_composing_activity_is_pinned_below_stream_body() -> None:
    import time as _time

    from pythinker_code.ui.shell.visualize._blocks import _ContentBlock

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, object()),
        steer=lambda _content: None,
    )
    view._turn_ended = False
    view._active_turn_depth = 1
    view._turn_start_time = _time.monotonic()
    block = _ContentBlock(is_think=False)
    block.append("Evidence:\n\nThe live preview stays with the body")
    view._current_content_block = block

    body = view.render_agent_status(80).value
    pinned_tail = view.render_pinned_status_tail(80).value

    assert "Evidence:" in body
    assert "The live preview stays with the body" in body
    assert "Composing" not in body
    assert "Composing" in pinned_tail


def test_render_pinned_status_tail_empty_when_turn_inactive() -> None:
    view = object.__new__(_PromptLiveView)
    view._turn_ended = True
    view._active_turn_depth = 0
    view._current_question_panel = None
    view._current_approval_request_panel = None
    view._pending_scrollback = []
    view._scrollback_handoff_depth = 0
    view._current_content_block = None
    assert view.render_pinned_status_tail(80).value == ""

    view2 = object.__new__(_PromptLiveView)
    view2._turn_ended = False
    view2._active_turn_depth = 0
    view2._current_question_panel = None
    view2._current_approval_request_panel = None
    view2._pending_scrollback = []
    view2._scrollback_handoff_depth = 0
    view2._current_content_block = None
    assert view2.render_pinned_status_tail(80).value == ""


def test_render_pinned_status_tail_finalizing_when_pending_scrollback() -> None:
    view = object.__new__(_PromptLiveView)
    view._turn_ended = True
    view._active_turn_depth = 0
    view._current_question_panel = None
    view._current_approval_request_panel = None
    view._pending_scrollback = [(Text("queued"), True)]
    view._scrollback_handoff_depth = 0
    view._current_content_block = None

    pinned = view.render_pinned_status_tail(80).value
    assert pinned.strip() != ""
    assert "Finalizing" in pinned


def test_render_pinned_status_tail_finalizing_during_scrollback_handoff() -> None:
    view = object.__new__(_PromptLiveView)
    view._turn_ended = True
    view._active_turn_depth = 0
    view._current_question_panel = None
    view._current_approval_request_panel = None
    view._pending_scrollback = []
    view._scrollback_handoff_depth = 1
    view._current_content_block = None

    assert view.render_pinned_status_tail(80).value == ""


def test_scrollback_handoff_suppresses_transient_prompt_layers() -> None:
    view = object.__new__(_PromptLiveView)
    view._turn_ended = False
    view._active_turn_depth = 1
    view._scrollback_handoff_depth = 1
    view._current_question_panel = None
    view._current_approval_request_panel = None
    view._committed_scrollback_this_turn = True

    assert view.render_agent_status(80).value == ""
    assert view.render_pinned_status_tail(80).value == ""
    assert view.running_prompt_hide_input_card() is True
    assert view.running_prompt_hide_input_card_chrome() is False


def test_render_pinned_status_tail_no_elapsed_spinner_during_midturn_handoff() -> None:
    """Mid-turn tool-transition handoffs (turn still active) must not render the
    elapsed-time verb spinner or tips — they stack as fossilized scrollback when
    the teardown erase drifts.
    """
    import time as _time

    from pythinker_code.ui.shell.visualize._blocks import _ContentBlock

    view = object.__new__(_PromptLiveView)
    view._turn_ended = False
    view._active_turn_depth = 1  # turn IS active (mid-turn transition)
    view._turn_start_time = _time.monotonic()
    view._current_question_panel = None
    view._current_approval_request_panel = None
    view._pending_scrollback = []
    view._scrollback_handoff_depth = 1  # ...inside a scrollback handoff
    block = _ContentBlock(is_think=False)
    block.append("Streaming body.\n\ntail")
    view._current_content_block = block

    out = view.render_pinned_status_tail(80).value
    assert out == ""
    assert "Composing" not in out


@pytest.mark.asyncio
async def test_transient_preamble_suppressed_inside_handoff_window(monkeypatch) -> None:
    """The real proof: sample the preamble renderers from inside the emit window
    of an actual ``_run_scrollback_handoff``. With the turn active and a content
    block present, the agent-status body and the elapsed verb spinner must both
    be suppressed while the handoff is in flight, so nothing height-varying is
    rendered during the prompt-app teardown.
    """
    import time as _time

    from pythinker_code.ui.shell.visualize._blocks import _ContentBlock

    class _PromptSession:
        def invalidate(self) -> None:
            pass

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    view._turn_ended = False
    view._active_turn_depth = 1
    view._turn_start_time = _time.monotonic()
    block = _ContentBlock(is_think=False)
    block.append("Live stream body.\n\ntail")
    view._current_content_block = block

    # Sanity: outside any handoff, the body and a live spinner are rendered.
    assert "Live stream body" in view.render_agent_status(80).value
    assert "Composing" in view.render_pinned_status_tail(80).value

    samples: dict[str, str] = {}

    def _emit() -> None:
        # In tests console.is_terminal is False, so emit() runs with the handoff
        # depth already incremented — exactly the teardown window.
        samples["body"] = view.render_agent_status(80).value
        samples["tail"] = view.render_pinned_status_tail(80).value

    await view._run_scrollback_handoff(_emit, reason="test")

    assert samples["body"] == ""  # multi-row stream suppressed during handoff
    assert samples["tail"] == ""  # no spinner/tips during handoff emit window

    # After the handoff completes the transient preamble comes back.
    assert "Live stream body" in view.render_agent_status(80).value
    assert "Composing" in view.render_pinned_status_tail(80).value


@pytest.mark.asyncio
async def test_multiple_handoffs_leave_no_transient_tail_snapshots(monkeypatch) -> None:
    """Each handoff emit window must see an empty pinned tail — no stacked verbs."""
    import time as _time

    from pythinker_code.ui.shell.visualize._blocks import _ContentBlock

    class _PromptSession:
        def invalidate(self) -> None:
            pass

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    view._turn_ended = False
    view._active_turn_depth = 1
    view._turn_start_time = _time.monotonic() - 30.0
    view._current_content_block = _ContentBlock(is_think=False)
    view._current_content_block.append("body\n\ntail")

    tails: list[str] = []

    async def _emit(_label: str) -> None:
        def _inner() -> None:
            tails.append(view.render_pinned_status_tail(80).value)

        await view._run_scrollback_handoff(_inner, reason=_label)

    await _emit("first")
    await _emit("second")
    await _emit("third")

    assert tails == ["", "", ""]


def test_handoff_suppresses_working_tips(monkeypatch) -> None:
    """Long-running turns show tips normally, but not during scrollback handoff."""
    import time as _time

    from pythinker_code.ui.shell.visualize._live_view import _WORKING_TIP_MIN_ELAPSED_S

    view = object.__new__(_PromptLiveView)
    view._turn_ended = False
    view._active_turn_depth = 1
    view._turn_start_time = _time.monotonic() - _WORKING_TIP_MIN_ELAPSED_S - 5.0
    view._current_question_panel = None
    view._current_approval_request_panel = None
    view._pending_scrollback = []
    view._scrollback_handoff_depth = 0
    view._current_content_block = None
    view._pinned_todos_visible = True
    view._latest_todos = ()
    view._resize_recovery_remaining = 0

    monkeypatch.setattr(_live_view_mod, "current_tip", lambda _now: "do the thing")

    normal = view.render_pinned_status_tail(80).value
    assert "Tip:" in normal

    view._scrollback_handoff_depth = 1
    during_handoff = view.render_pinned_status_tail(80).value
    assert during_handoff == ""


def test_resize_triggers_recovery_and_hides_tips(monkeypatch) -> None:
    import time as _time

    from pythinker_code.ui.shell.visualize._live_view import _WORKING_TIP_MIN_ELAPSED_S

    view = object.__new__(_PromptLiveView)
    view._turn_ended = False
    view._active_turn_depth = 1
    view._turn_start_time = _time.monotonic() - _WORKING_TIP_MIN_ELAPSED_S - 5.0
    view._current_question_panel = None
    view._current_approval_request_panel = None
    view._pending_scrollback = []
    view._scrollback_handoff_depth = 0
    view._current_content_block = None
    view._pinned_todos_visible = True
    view._latest_todos = ()
    view._last_terminal_size = (80, 24)
    view._resize_recovery_remaining = 0
    view._force_refresh = False

    monkeypatch.setattr(_live_view_mod, "current_tip", lambda _now: "resize tip")

    current_size = [80, 24]

    def _size() -> tuple[int, int]:
        return (current_size[0], current_size[1])

    monkeypatch.setattr(view, "_current_terminal_size", _size)

    current_size[:] = [100, 30]
    view._tick_resize_recovery()
    assert view._force_refresh is True
    assert view._resize_recovery_remaining == _interactive_mod._RESIZE_RECOVERY_FRAMES - 1
    assert "Tip:" not in view.render_pinned_status_tail(80).value

    view._force_refresh = False
    for expected in (
        _interactive_mod._RESIZE_RECOVERY_FRAMES - 2,
        0,
    ):
        view._tick_resize_recovery()
        assert view._resize_recovery_remaining == expected

    assert "Tip:" in view.render_pinned_status_tail(80).value


@pytest.mark.asyncio
async def test_flush_pending_scrollback_deferred_during_resize_recovery(monkeypatch) -> None:
    printed: list[object] = []

    class _PromptSession:
        def invalidate(self) -> None:
            pass

    async def _run_in_terminal(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        func()

    monkeypatch.setattr(_interactive_mod, "run_in_terminal", _run_in_terminal)
    monkeypatch.setattr(
        _live_view_mod.console,
        "print",
        lambda *args, **kwargs: printed.extend(args) if args else None,
    )

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    view._pending_scrollback.append((Text("queued block"), True))
    view._resize_recovery_remaining = 2

    await view._flush_pending_scrollback()

    assert printed == []
    assert len(view._pending_scrollback) == 1


@pytest.mark.asyncio
async def test_flush_pending_scrollback_forced_on_turn_end_during_resize_recovery(
    monkeypatch,
) -> None:
    printed: list[object] = []

    class _PromptSession:
        def invalidate(self) -> None:
            pass

    async def _run_in_terminal(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        func()

    monkeypatch.setattr(_interactive_mod, "run_in_terminal", _run_in_terminal)
    monkeypatch.setattr(
        _live_view_mod.console,
        "print",
        lambda *args, **kwargs: printed.extend(args) if args else None,
    )

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    view._pending_scrollback.append((Text("Smoke turn one completed."), True))
    view._resize_recovery_remaining = 2

    await view._flush_pending_scrollback(force=True)

    assert len(printed) == 1
    assert isinstance(printed[0], Text)
    assert printed[0].plain == "Smoke turn one completed."
    assert view._pending_scrollback == []


@pytest.mark.asyncio
async def test_flush_pending_scrollback_retains_queue_on_handoff_failure(monkeypatch) -> None:
    printed: list[object] = []

    class _PromptSession:
        def invalidate(self) -> None:
            pass

    async def _run_in_terminal(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("terminal suspended")

    monkeypatch.setattr(_interactive_mod, "run_in_terminal", _run_in_terminal)
    monkeypatch.setattr(_live_view_mod.console, "_force_terminal", True)
    monkeypatch.setattr(
        _live_view_mod.console,
        "print",
        lambda *args, **kwargs: printed.extend(args) if args else None,
    )

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    view._pending_scrollback.append((Text("must survive"), True))

    await view._flush_pending_scrollback()

    assert printed == []
    assert len(view._pending_scrollback) == 1
    assert view._pending_scrollback[0][0].plain == "must survive"


def test_tick_resize_recovery_ignores_invalid_geometry() -> None:
    view = object.__new__(_PromptLiveView)
    view._last_terminal_size = (80, 24)
    view._resize_recovery_remaining = 0
    view._force_refresh = False

    view._current_terminal_size = lambda: (0, 24)  # type: ignore[method-assign]
    view._tick_resize_recovery()
    assert view._last_terminal_size == (80, 24)
    assert view._resize_recovery_remaining == 0
    assert view._force_refresh is False


def test_render_pinned_status_tail_finalizing_when_committed_blocks_pending() -> None:
    from pythinker_code.ui.shell.visualize._blocks import _ContentBlock

    view = object.__new__(_PromptLiveView)
    view._turn_ended = True
    view._active_turn_depth = 0
    view._current_question_panel = None
    view._current_approval_request_panel = None
    view._pending_scrollback = []
    view._scrollback_handoff_depth = 0
    block = _ContentBlock(is_think=False)
    block.append("Done paragraph.\n\ntail")
    view._current_content_block = block

    assert "Finalizing" in view.render_pinned_status_tail(80).value


def test_render_pinned_status_tail_empty_while_question_panel_open() -> None:
    import time as _time

    from pythinker_code.ui.shell.visualize import QuestionRequestPanel
    from pythinker_code.wire.types import QuestionItem, QuestionOption, QuestionRequest

    view = object.__new__(_PromptLiveView)
    view._turn_ended = False
    view._active_turn_depth = 1
    view._turn_start_time = _time.monotonic()
    view._current_approval_request_panel = None
    view._current_question_panel = QuestionRequestPanel(
        QuestionRequest(
            id="qr",
            tool_call_id="tc",
            questions=[
                QuestionItem(
                    question="Approve this plan?",
                    options=[QuestionOption(label="Approve", description="")],
                )
            ],
        )
    )

    assert view.render_pinned_status_tail(80).value == ""


def test_file_activity_shelf_renders_compact_rows() -> None:
    from rich.console import Console

    from pythinker_code.ui.shell.visualize._blocks import FileActivityShelf

    shelf = FileActivityShelf(max_rows=2)
    shelf.mark("src/one.py", "created")
    shelf.mark("src/two.py", "updated")
    shelf.mark("src/three.py", "writing")

    console = Console(width=80, record=True, color_system=None)
    rendered = shelf.render(80)
    assert rendered is not None
    console.print(rendered)
    plain = console.export_text()

    assert "Files" in plain
    assert "updated" in plain
    assert "src/two.py" in plain
    assert "writing" in plain
    assert "src/three.py" in plain
    assert "+1 more" in plain
    assert "src/one.py" not in plain


def test_file_activity_tracks_write_tool_until_result() -> None:
    import json

    from pythinker_core.message import ToolCall
    from pythinker_core.tooling import ToolResult, ToolReturnValue
    from rich.console import Console

    from pythinker_code.tools.display import DiffDisplayBlock

    class _PromptSession:
        def update_pinned_todos(self, _items) -> None:  # noqa: ANN001
            pass

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    call = ToolCall(
        id="write-1",
        function=ToolCall.FunctionBody(
            name="WriteFile",
            arguments=json.dumps({"path": "src/new_file.py", "content": "print(1)"}),
        ),
    )

    view.append_tool_call(call)
    live = view._file_activity_shelf.render(80)
    assert live is not None
    console = Console(width=80, record=True, color_system=None)
    console.print(live)
    assert "writing" in console.export_text()

    view.append_tool_result(
        ToolResult(
            tool_call_id="write-1",
            return_value=ToolReturnValue(
                is_error=False,
                output="ok",
                message="ok",
                display=[
                    DiffDisplayBlock(path="src/new_file.py", old_text="", new_text="print(1)")
                ],
            ),
        )
    )
    console = Console(width=80, record=True, color_system=None)
    updated = view._file_activity_shelf.render(80)
    assert updated is not None
    console.print(updated)
    plain = console.export_text()
    assert "updated" in plain
    assert "src/new_file.py" in plain


def test_pinned_tail_stays_visible_while_foreground_tool_executes() -> None:
    """The shimmer verb spinner stays pinned for the whole active turn — including
    while a foreground tool (e.g. a server started via the shell tool, or a
    subagent) runs. The agent is still working the turn, so the spinner is the
    liveness signal throughout, the same way it persists while thinking. It clears
    only when the turn ends."""
    import time as _time

    from pythinker_core.message import ToolCall
    from pythinker_core.tooling import ToolReturnValue

    from pythinker_code.ui.shell.visualize._blocks import _ToolCallBlock

    view = object.__new__(_PromptLiveView)
    view._turn_ended = False
    view._active_turn_depth = 1
    view._turn_start_time = _time.monotonic()
    view._current_question_panel = None
    view._current_approval_request_panel = None

    block = _ToolCallBlock(
        ToolCall(id="tc-1", function=ToolCall.FunctionBody(name="Shell", arguments="{}"))
    )
    block.mark_execution_started()
    view._tool_call_blocks = {block.tool_call_id: block}

    # While the foreground command runs, the spinner stays visible.
    assert view.render_pinned_status_tail(80).value.strip() != ""

    # It is still visible after the tool finishes and the agent processes results.
    block.finish(ToolReturnValue(is_error=False, output="ok", message="ok", display=[]))
    assert view.render_pinned_status_tail(80).value.strip() != ""


@pytest.mark.asyncio
async def test_prompt_live_view_status_refresh_invalidates_active_turn(monkeypatch) -> None:
    invalidations: list[str] = []

    class _PromptSession:
        def invalidate(self) -> None:
            invalidations.append("invalidate")

    monkeypatch.setattr(_interactive_mod, "_STATUS_REFRESH_INTERVAL_S", 0.001)
    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    view._active_turn_depth = 1
    view._turn_ended = False

    task = asyncio.create_task(view._status_refresh_loop())
    try:
        for _ in range(20):
            if invalidations:
                break
            await asyncio.sleep(0.002)
        assert invalidations
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_prompt_live_view_status_refresh_skips_inactive_turn(monkeypatch) -> None:
    invalidations: list[str] = []

    class _PromptSession:
        def invalidate(self) -> None:
            invalidations.append("invalidate")

    monkeypatch.setattr(_interactive_mod, "_STATUS_REFRESH_INTERVAL_S", 0.001)
    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    view._active_turn_depth = 0
    view._turn_ended = False

    task = asyncio.create_task(view._status_refresh_loop())
    try:
        await asyncio.sleep(0.006)
        assert invalidations == []
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def test_pinned_tail_survives_preamble_clip() -> None:
    """A clipped agent stream must not hide the pinned verb spinner: the spinner
    text stays visible *after* the clip hint instead of being covered by it."""
    from prompt_toolkit.formatted_text import FormattedText

    preamble = FormattedText([("", "\n".join(f"line {i}" for i in range(40)))])
    pinned = FormattedText([("", "Pondering…\n"), ("", "  ⎿ Tip: do the thing")])

    out = CustomPromptSession._fit_preamble_with_pinned_tail(
        preamble, pinned, columns=80, max_rows=6
    )
    text = "".join(fragment for _, fragment, *_ in out)

    assert PREAMBLE_EARLIER_OUTPUT_HIDDEN_HINT in text
    assert "Pondering…" in text
    # The spinner is pinned below the clip hint, never clipped away above it.
    assert text.index("earlier output hidden") < text.index("Pondering…")


def test_pinned_tail_absent_falls_back_to_plain_clip() -> None:
    from prompt_toolkit.formatted_text import FormattedText

    preamble = FormattedText([("", "\n".join(f"line {i}" for i in range(40)))])
    out = CustomPromptSession._fit_preamble_with_pinned_tail(
        preamble, FormattedText(), columns=80, max_rows=6
    )
    text = "".join(fragment for _, fragment, *_ in out)
    assert PREAMBLE_EARLIER_OUTPUT_HIDDEN_HINT in text


def test_pinned_tail_has_blank_row_after_preamble() -> None:
    from prompt_toolkit.formatted_text import FormattedText

    out = CustomPromptSession._fit_preamble_with_pinned_tail(
        FormattedText([("", "tool output")]),
        FormattedText([("", "Actioning…")]),
        columns=80,
        max_rows=6,
    )
    text = "".join(fragment for _, fragment, *_ in out)

    assert "tool output\n\nActioning…" in text


def test_pinned_tail_has_blank_row_below_before_prompt() -> None:
    from prompt_toolkit.formatted_text import FormattedText

    out = CustomPromptSession._fit_preamble_with_pinned_tail(
        FormattedText([("", "tool output")]),
        FormattedText([("", "Actioning…")]),
        columns=80,
        max_rows=6,
    )
    text = "".join(fragment for _, fragment, *_ in out)

    # Breathing room below the pinned tail so the todo list / spinner is never
    # flush against the prompt separator beneath it.
    assert text.endswith("Actioning…\n\n")


def test_pinned_tail_has_initial_blank_row_when_first_visible_status() -> None:
    from prompt_toolkit.formatted_text import FormattedText

    out = CustomPromptSession._fit_preamble_with_pinned_tail(
        FormattedText(),
        FormattedText([("", "Actioning…")]),
        columns=80,
        max_rows=6,
    )
    text = "".join(fragment for _, fragment, *_ in out)

    assert text.startswith("\nActioning…")


def test_prompt_status_shows_working_spinner_for_background_tasks() -> None:
    session = object.__new__(CustomPromptSession)
    session._running_prompt_delegate = None
    session._background_task_count_provider = lambda: BgTaskCounts(agent=2)
    session._status_block_provider = None

    rendered = CustomPromptSession._render_agent_status(session, 80)
    text = "".join(item[1] for item in rendered)

    assert "…" in text
    assert "background agent" not in text  # footer owns the count


def test_prompt_status_block_renders_above_agent_input_preamble() -> None:
    from prompt_toolkit.formatted_text import FormattedText

    def _status_block(_columns: int) -> FormattedText:
        return FormattedText([("", "• Booting MCP server: context7")])

    session = object.__new__(CustomPromptSession)
    session._running_prompt_delegate = None
    session._background_task_count_provider = None
    session._status_block_provider = _status_block

    rendered = CustomPromptSession._render_agent_status(session, 80)
    text = "".join(item[1] for item in rendered)

    assert text.startswith("• Booting MCP server: context7")


def test_background_status_splits_verb_and_count_styles(monkeypatch) -> None:
    import pythinker_code.ui.shell.prompt as prompt_module
    from pythinker_code.ui.theme import get_active_theme, set_active_theme

    monkeypatch.setattr(prompt_module.time, "monotonic", lambda: 0.88)
    # Pin the discrete three-step sheen (256-color tier) for determinism.
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    saved_theme = get_active_theme()
    try:
        set_active_theme("dark")
        session = object.__new__(CustomPromptSession)
        session._background_task_count_provider = lambda: BgTaskCounts(agent=2)

        rendered = CustomPromptSession._render_background_working_status(session, 80)
    finally:
        set_active_theme(saved_theme)

    fragments = [(style, text) for style, text, *_ in rendered]
    shimmer_styles = {style.lower() for style, text in fragments if text.strip(" …")}

    assert {
        f"fg:{_SHIMMER_BASE.lower()}",
        f"fg:{_SHIMMER_MID.lower()}",
        f"fg:{_SHIMMER_HIGHLIGHT.lower()}",
    } <= shimmer_styles
    # The footer owns the background-task count; this line carries only the verb.
    assert not any("background agent" in text for _style, text in fragments)
    assert all(style != "ansicyan" for style, _ in fragments)


def test_prompt_status_falls_back_to_background_spinner_after_turn_end() -> None:
    session = object.__new__(CustomPromptSession)
    session._background_task_count_provider = lambda: BgTaskCounts(agent=1)
    session._status_block_provider = None
    session._latest_todos = ()

    class _EndedDelegate:
        def render_agent_status(self, columns: int):  # noqa: ARG002
            return ""

    session._running_prompt_delegate = cast(Any, _EndedDelegate())

    rendered = CustomPromptSession._render_agent_status(session, 80)
    text = "".join(item[1] for item in rendered)

    assert "…" in text
    assert "1 background agent" not in text


def test_prompt_status_keeps_background_spinner_during_blocking_task_output() -> None:
    session = object.__new__(CustomPromptSession)
    session._background_task_count_provider = lambda: BgTaskCounts(agent=2)
    session._status_block_provider = None
    session._latest_todos = ()

    class _BlockingTaskOutputDelegate:
        def render_agent_status(self, columns: int):  # noqa: ARG002
            return "TaskOutput(agent-reviewer · block, timeout 600s)"

        def render_pinned_status_tail(self, columns: int):  # noqa: ARG002
            return ""

    session._running_prompt_delegate = cast(Any, _BlockingTaskOutputDelegate())

    rendered = CustomPromptSession._render_agent_status(session, 80)
    text = "".join(item[1] for item in rendered)

    assert "TaskOutput(agent-reviewer" in text
    assert "…" in text
    assert "2 background agents" not in text


def test_prompt_status_keeps_todos_visible_during_background_tasks() -> None:
    session = object.__new__(CustomPromptSession)
    session._running_prompt_delegate = None
    session._background_task_count_provider = lambda: BgTaskCounts(agent=3)
    session._status_block_provider = None
    session._latest_todos = (
        TodoDisplayItem(title="Security vulnerability scan", status="in_progress"),
        TodoDisplayItem(title="Code quality review", status="pending"),
    )

    rendered = CustomPromptSession._render_agent_status(session, 100)
    text = "".join(item[1] for item in rendered)

    assert "3 background agents" not in text
    assert "⎿  ■ Security vulnerability scan" in text
    assert "□ Code quality review" in text


def test_prompt_background_todo_rows_align_icons_and_titles() -> None:
    session = object.__new__(CustomPromptSession)
    session._latest_todos = (
        TodoDisplayItem(
            title="Launch parallel deep scan agents (overengineering, simplicity, architecture, bug hunt)",
            status="in_progress",
        ),
        TodoDisplayItem(title="Cross-validate findings against live code", status="pending"),
        TodoDisplayItem(
            title="Synthesize consolidated findings report with fixes", status="pending"
        ),
    )

    rendered = CustomPromptSession._render_background_todo_rows(session, 120)
    lines = "".join(item[1] for item in rendered).splitlines()

    assert lines[0].startswith("  ⎿  ■ ")
    assert lines[1].startswith("     □ ")
    assert lines[2].startswith("     □ ")
    assert lines[1].index("□") == lines[0].index("■")
    assert lines[2].index("Synthesize") == lines[0].index("Launch")


def test_background_status_drops_verb_when_working_indicator_pinned() -> None:
    """During an active turn the pinned working indicator owns the verb, so the
    background-task line must show the count *without* repeating it."""
    session = object.__new__(CustomPromptSession)
    session._background_task_count_provider = lambda: BgTaskCounts(agent=3)
    session._status_block_provider = None

    class _ActiveDelegate:
        def render_agent_status(self, columns: int):  # noqa: ARG002
            return ""  # no body content yet — only the spinner is active

        def render_pinned_status_tail(self, columns: int):  # noqa: ARG002
            return "Reticulating… · 42s"

    session._running_prompt_delegate = cast(Any, _ActiveDelegate())

    rendered = CustomPromptSession._render_agent_status(session, 80)
    text = "".join(item[1] for item in rendered)

    # The pinned tail owns the verb and the footer owns the count — nothing
    # is duplicated under the executing step.
    assert "background agent" not in text
    assert "…" not in text


def test_background_status_omits_todos_when_verb_pinned() -> None:
    """When the pinned status tail is active (show_verb=False) it already renders
    the todo list under the verb spinner; the background-task line must NOT repeat
    it, or the same todo list renders twice while the agent works."""
    session = object.__new__(CustomPromptSession)
    session._background_task_count_provider = lambda: BgTaskCounts(agent=3)
    session._status_block_provider = None
    session._latest_todos = (
        TodoDisplayItem(title="Security vulnerability scan", status="in_progress"),
        TodoDisplayItem(title="Code quality review", status="pending"),
    )

    # Between turns (no pinned tail) the background line is the only surface,
    # so it must carry the todos — but never a count (the footer owns that).
    standalone = CustomPromptSession._render_background_working_status(session, 100)
    standalone_text = "".join(item[1] for item in standalone)
    assert "Security vulnerability scan" in standalone_text
    assert "Code quality review" in standalone_text
    assert "background agent" not in standalone_text


def test_running_prompt_hides_placeholder() -> None:
    view = object.__new__(_PromptLiveView)
    view._turn_ended = False
    view._current_approval_request_panel = None
    view._current_question_panel = None
    view._btw_modal = None

    assert view.running_prompt_placeholder() is None
    assert view.running_prompt_allows_text_input() is True


def test_running_prompt_shows_approval_placeholder_and_locks_text_input() -> None:
    view = object.__new__(_PromptLiveView)
    view._turn_ended = False
    view._current_question_panel = None
    view._current_approval_request_panel = object()

    placeholder = view.running_prompt_placeholder()

    assert isinstance(placeholder, str)
    assert "1/2/3" in placeholder
    assert view.running_prompt_allows_text_input() is False


def test_running_prompt_allows_text_input_for_question_other_answer() -> None:
    QuestionPromptDelegate = shell_visualize.QuestionPromptDelegate
    panel = type(
        "_Panel",
        (),
        {
            "has_expandable_content": False,
            "is_multi_select": False,
            "should_prompt_other_input": staticmethod(lambda: False),
        },
    )()
    delegate = QuestionPromptDelegate(
        panel,
        on_advance=lambda: None,
        on_invalidate=lambda: None,
    )
    delegate._awaiting_other_input = True

    assert delegate.running_prompt_allows_text_input() is True
    assert delegate.running_prompt_accepts_submission() is True


def test_running_prompt_does_not_accept_submission_after_turn_end_without_panels() -> None:
    view = object.__new__(_PromptLiveView)
    view._turn_ended = True
    view._current_question_panel = None
    view._current_approval_request_panel = None

    assert view.running_prompt_accepts_submission() is False


def test_running_prompt_keeps_accepting_submission_for_active_approval_after_turn_end() -> None:
    view = object.__new__(_PromptLiveView)
    view._turn_ended = True
    view._current_question_panel = None
    view._current_approval_request_panel = object()

    assert view.running_prompt_accepts_submission() is True


def test_live_view_renders_steer_input_as_user_echo(monkeypatch) -> None:
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "pythinker")
    view = _LiveView(StatusUpdate())
    cleaned: list[bool] = []
    printed: list[str] = []

    monkeypatch.setattr(view, "cleanup", lambda *, is_interrupt: cleaned.append(is_interrupt))
    monkeypatch.setattr(
        shell_visualize.console,
        "print",
        lambda text: printed.append(getattr(text, "plain", str(text))),
    )

    view.dispatch_wire_message(SteerInput(user_input=[TextPart(text="A steer follow-up")]))

    assert cleaned == [False]
    assert printed == ["❯ A steer follow-up"]


def test_live_view_flushes_current_output_before_printing_steer_input(monkeypatch) -> None:
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "pythinker")
    view = _LiveView(StatusUpdate())
    order: list[object] = []

    monkeypatch.setattr(
        view,
        "flush_content",
        lambda reason=None: order.append("flush_content"),
    )
    monkeypatch.setattr(view, "flush_finished_tool_calls", lambda: order.append("flush_tools"))
    monkeypatch.setattr(
        shell_visualize.console,
        "print",
        lambda text: order.append(("print", getattr(text, "plain", str(text)))),
    )

    view.dispatch_wire_message(SteerInput(user_input=[TextPart(text="A steer follow-up")]))

    assert order[:2] == ["flush_content", "flush_tools"]
    assert order[-1] == ("print", "❯ A steer follow-up")


@pytest.mark.asyncio
async def test_live_view_processes_external_approval_messages(monkeypatch) -> None:
    updates: list[object] = []

    class _FakeLive:
        def __init__(self, *args, **kwargs) -> None:
            self._live_render = type("_Render", (), {"_shape": None})()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def update(self, renderable, refresh: bool = True) -> None:
            updates.append(renderable)

        def refresh(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def start(self) -> None:
            return None

    class _Wire:
        async def receive(self):
            await asyncio.Event().wait()

    @asynccontextmanager
    async def _no_keyboard_listener(*args, **kwargs):
        yield

    monkeypatch.setattr(_live_view_mod, "DiffLive", _FakeLive)
    monkeypatch.setattr(_live_view_mod, "_keyboard_listener", _no_keyboard_listener)

    view = _LiveView(StatusUpdate())
    task = asyncio.create_task(view.visualize_loop(cast(Any, _Wire())))
    try:
        await asyncio.sleep(0)
        view.enqueue_external_message(
            ApprovalRequest(
                id="req-ext-1",
                tool_call_id="call-ext-1",
                sender="Shell",
                action="run command",
                description="pwd",
            )
        )
        for _ in range(10):
            if view._current_approval_request_panel is not None:
                break
            await asyncio.sleep(0)
        assert view._current_approval_request_panel is not None
        assert updates
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_prompt_live_view_processes_external_approval_messages() -> None:
    invalidations: list[str] = []

    class _PromptSession:
        def invalidate(self) -> None:
            invalidations.append("invalidate")

    class _Wire:
        async def receive(self):
            await asyncio.Event().wait()

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    task = asyncio.create_task(view.visualize_loop(cast(Any, _Wire())))
    try:
        await asyncio.sleep(0)
        view.enqueue_external_message(
            ApprovalRequest(
                id="req-prompt-ext-1",
                tool_call_id="call-prompt-ext-1",
                sender="WriteFile",
                action="edit file",
                description="Write file `/tmp/bg.txt`",
            )
        )
        for _ in range(10):
            if view._current_approval_request_panel is not None:
                break
            await asyncio.sleep(0)
        assert view._current_approval_request_panel is not None
        assert invalidations
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_prompt_live_view_keeps_processing_external_approvals_after_turn_end() -> None:
    invalidations: list[str] = []
    gate = asyncio.Event()

    class _PromptSession:
        def invalidate(self) -> None:
            invalidations.append("invalidate")

    class _Wire:
        def __init__(self) -> None:
            self._seen_turn_end = False

        async def receive(self):
            if not self._seen_turn_end:
                self._seen_turn_end = True
                return shell_visualize.TurnEnd()
            await gate.wait()
            raise shell_visualize.QueueShutDown

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    task = asyncio.create_task(view.visualize_loop(cast(Any, _Wire())))
    try:
        for _ in range(10):
            if view._turn_ended:
                break
            await asyncio.sleep(0)
        assert view._turn_ended is True

        view.enqueue_external_message(
            ApprovalRequest(
                id="req-prompt-turn-end",
                tool_call_id="call-prompt-turn-end",
                sender="WriteFile",
                action="edit file",
                description="Write file `/tmp/bg.txt`",
            )
        )
        for _ in range(10):
            if view._current_approval_request_panel is not None:
                break
            await asyncio.sleep(0)
        assert view._current_approval_request_panel is not None
        assert invalidations
    finally:
        gate.set()
        await task


@pytest.mark.asyncio
async def test_prompt_live_view_flushes_content_before_marking_turn_ended(monkeypatch) -> None:
    invalidations: list[str] = []
    printed: list[object] = []
    gate = asyncio.Event()

    class _PromptSession:
        def invalidate(self) -> None:
            invalidations.append("invalidate")

    class _Wire:
        def __init__(self) -> None:
            self._messages = [
                TurnBegin(user_input="summarize"),
                TextPart(text="Final streamed answer."),
                TurnEnd(),
            ]

        async def receive(self):
            if self._messages:
                return self._messages.pop(0)
            await gate.wait()
            raise shell_visualize.QueueShutDown

    async def _run_in_terminal(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        func()

    monkeypatch.setattr(_interactive_mod, "run_in_terminal", _run_in_terminal)

    def _record_print(*args: object, **_kwargs: object) -> None:
        if args:
            printed.extend(args)

    monkeypatch.setattr(_live_view_mod.console, "print", _record_print)

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )

    # Prove the ordering contract, not just the end state: the turn-end
    # flush_content() must run while _turn_ended is still False, before
    # visualize_loop marks the turn ended (_interactive.py flushes, then sets
    # the flag). Recording only end-state cannot distinguish flush-before-set
    # from set-before-flush.
    flush_turn_ended_states: list[bool] = []
    original_flush_content = view.flush_content

    def _tracking_flush_content(reason=None):  # noqa: ANN001, ANN202
        flush_turn_ended_states.append(view._turn_ended)
        if reason is None:
            return original_flush_content()
        return original_flush_content(reason)

    monkeypatch.setattr(view, "flush_content", _tracking_flush_content)

    task = asyncio.create_task(view.visualize_loop(cast(Any, _Wire())))
    # The task is consumed in the finally block; this reference keeps
    # the assignment from being flagged as a no-op by static analysis.
    assert task is not None
    try:
        for _ in range(20):
            if view._turn_ended:
                break
            await asyncio.sleep(0)

        assert view._turn_ended is True
        assert view._current_content_block is None
        assert printed
        assert invalidations
        # The final flush is the turn-end flush; it must have observed
        # _turn_ended still False, proving flush precedes the flag flip.
        assert flush_turn_ended_states, "flush_content was never called"
        assert flush_turn_ended_states[-1] is False
    finally:
        gate.set()
        await task


@pytest.mark.asyncio
async def test_prompt_live_view_prints_turn_recap_after_turn_end(monkeypatch) -> None:
    invalidations: list[str] = []
    printed: list[object] = []

    class _PromptSession:
        def invalidate(self) -> None:
            invalidations.append("invalidate")

    class _Wire:
        def __init__(self) -> None:
            self._messages = [
                TurnBegin(user_input="implement recaps"),
                TextPart(text="Implemented a /recap command."),
                TurnEnd(),
            ]

        async def receive(self):
            if self._messages:
                return self._messages.pop(0)
            raise shell_visualize.QueueShutDown

    monkeypatch.setattr(
        _live_view_mod.console, "print", lambda *args, **kwargs: printed.extend(args)
    )

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
        show_turn_recaps=True,
    )

    await view.visualize_loop(cast(Any, _Wire()))

    from rich.console import Console

    output = Console(width=100, record=True, color_system=None)
    for item in printed:
        output.print(item)
    plain = output.export_text()
    assert "※ recap: Implemented a /recap command." in plain
    assert "disable recaps in /config" in plain
    assert invalidations


@pytest.mark.asyncio
async def test_live_view_reject_does_not_reject_background_requests_from_other_sources() -> None:
    view = _LiveView(StatusUpdate())
    request_one = ApprovalRequest(
        id="req-bg-source-1",
        tool_call_id="call-bg-source-1",
        sender="Shell",
        action="run command",
        description="echo first",
        source_kind="background_agent",
        source_id="task-1",
    )
    request_two = ApprovalRequest(
        id="req-bg-source-2",
        tool_call_id="call-bg-source-2",
        sender="Shell",
        action="run command",
        description="echo second",
        source_kind="background_agent",
        source_id="task-2",
    )

    view.request_approval(request_one)
    view.request_approval(request_two)
    assert view._current_approval_request_panel is not None
    view._current_approval_request_panel.selected_index = 2

    view._submit_approval()

    assert request_one.resolved is True
    assert await request_one.wait() == "reject"
    assert request_two.resolved is False
    assert view._current_approval_request_panel is not None
    assert view._current_approval_request_panel.request is request_two


@pytest.mark.asyncio
async def test_live_view_reject_does_not_auto_reject_later_requests_from_same_source() -> None:
    view = _LiveView(StatusUpdate())
    request_one = ApprovalRequest(
        id="req-bg-same-source-1",
        tool_call_id="call-bg-same-source-1",
        sender="Shell",
        action="run command",
        description="echo first",
        source_kind="background_agent",
        source_id="task-shared",
    )
    request_two = ApprovalRequest(
        id="req-bg-same-source-2",
        tool_call_id="call-bg-same-source-2",
        sender="Shell",
        action="run command",
        description="echo second",
        source_kind="background_agent",
        source_id="task-shared",
    )

    view.request_approval(request_one)
    assert view._current_approval_request_panel is not None
    view._current_approval_request_panel.selected_index = 2

    view._submit_approval()
    view.request_approval(request_two)

    assert request_two.resolved is False
    assert view._current_approval_request_panel is not None
    assert view._current_approval_request_panel.request is request_two


@pytest.mark.asyncio
async def test_live_view_approval_num4_selects_feedback_option() -> None:
    """Pressing NUM_4 in _LiveView should select the feedback (4th) approval option."""
    view = _LiveView(StatusUpdate())
    request = ApprovalRequest(
        id="req-num4",
        tool_call_id="call-num4",
        sender="Shell",
        action="run command",
        description="echo hello",
    )
    view.request_approval(request)
    assert view._current_approval_request_panel is not None

    # NUM_4 selects the feedback option (index 3) and submits as "reject"
    view.dispatch_keyboard_event(shell_visualize.KeyEvent.NUM_4)

    assert request.resolved is True
    assert await request.wait() == "reject"


@pytest.mark.asyncio
async def test_approval_prompt_delegate_ctrl_c_rejects_current_request() -> None:
    resolved: list[tuple[str, str]] = []
    request = ApprovalRequest(
        id="req-ctrl-c",
        tool_call_id="call-ctrl-c",
        sender="Shell",
        action="run command",
        description="pwd",
    )
    delegate = shell_visualize.ApprovalPromptDelegate(
        request,
        on_response=lambda req, resp, feedback="": resolved.append((req.id, resp)),
    )

    assert delegate.should_handle_running_prompt_key("c-c") is True
    delegate.handle_running_prompt_key(
        "c-c", type("_Event", (), {"app": None, "current_buffer": Buffer()})()
    )

    assert request.resolved is True
    assert resolved == [("req-ctrl-c", "reject")]


def test_running_prompt_suppresses_local_steer_echo_from_wire(monkeypatch) -> None:
    view = object.__new__(_PromptLiveView)
    view._pending_local_steer_count = 1

    forwarded: list[object] = []
    monkeypatch.setattr(
        _LiveView,
        "dispatch_wire_message",
        lambda self, msg: forwarded.append(msg),
    )
    view.dispatch_wire_message(SteerInput(user_input=[TextPart(text="A steer follow-up")]))

    assert view._pending_local_steer_count == 0
    assert forwarded == []


def test_running_prompt_forwards_non_local_steer_from_wire(monkeypatch) -> None:
    view = object.__new__(_PromptLiveView)
    view._pending_local_steer_count = 0

    forwarded: list[object] = []
    monkeypatch.setattr(
        _LiveView,
        "dispatch_wire_message",
        lambda self, msg: forwarded.append(msg),
    )
    wire_msg = SteerInput(user_input=[TextPart(text="remote steer")])
    view.dispatch_wire_message(wire_msg)

    assert view._pending_local_steer_count == 0
    assert forwarded == [wire_msg]


def test_handle_local_input_queues_message_by_default() -> None:
    from unittest.mock import MagicMock

    view = object.__new__(_PromptLiveView)
    view._turn_ended = False
    view._queued_messages = []
    view._prompt_session = MagicMock()

    user_in = UserInput(
        mode=PromptMode.AGENT,
        command="[Pasted text #1 +3 lines]",
        resolved_command="line1\nline2\nline3",
        content=[TextPart(text="line1\nline2\nline3")],
    )
    view.handle_local_input(user_in)

    # Default Enter queues instead of steering
    assert len(view._queued_messages) == 1
    assert view._queued_messages[0].command == "[Pasted text #1 +3 lines]"


def test_handle_local_input_ignores_finished_turn(monkeypatch) -> None:
    view = object.__new__(_PromptLiveView)
    view._turn_ended = True
    view._queued_messages = []
    view._flush_prompt_refresh = lambda: None

    view.handle_local_input(
        UserInput(
            mode=PromptMode.AGENT,
            command="ignored",
            resolved_command="ignored",
            content=[TextPart(text="ignored")],
        )
    )

    # Turn ended — input should be silently ignored, nothing queued
    assert view._queued_messages == []


def test_should_prompt_question_other_for_key_shared_helper() -> None:
    view = object.__new__(_PromptLiveView)
    view._current_question_panel = type(
        "_Panel",
        (),
        {
            "is_multi_select": False,
            "should_prompt_other_input": staticmethod(lambda: True),
        },
    )()

    assert view._should_prompt_question_other_for_key(shell_visualize.KeyEvent.ENTER) is True
    assert view._should_prompt_question_other_for_key(shell_visualize.KeyEvent.SPACE) is True

    view._current_question_panel = type(
        "_Panel",
        (),
        {
            "is_multi_select": True,
            "should_prompt_other_input": staticmethod(lambda: True),
        },
    )()

    assert view._should_prompt_question_other_for_key(shell_visualize.KeyEvent.SPACE) is False


def test_submit_question_other_text_resolves_request_when_done() -> None:
    resolved: list[object] = []
    calls: list[str] = []

    class _Request:
        def resolve(self, answers) -> None:
            resolved.append(answers)

    class _Panel:
        request = _Request()

        @staticmethod
        def submit_other(text: str) -> bool:
            calls.append(text)
            return True

        @staticmethod
        def get_answers() -> dict[str, str]:
            return {"q": "custom"}

    view = object.__new__(_PromptLiveView)
    view._current_question_panel = _Panel()
    view.show_next_question_request = lambda: calls.append("next")
    view.refresh_soon = lambda: calls.append("refresh")

    view._submit_question_other_text("custom")

    assert calls == ["custom", "next", "refresh"]
    assert resolved == [{"q": "custom"}]


def test_question_delegate_clears_buffer_for_key_actions() -> None:
    QuestionPromptDelegate = shell_visualize.QuestionPromptDelegate

    submitted: list[bool] = []

    class _Panel:
        has_expandable_content = False
        is_multi_select = False
        is_other_selected = False
        request = type("_Req", (), {"resolve": lambda self, x: None})()

        @staticmethod
        def should_prompt_other_input() -> bool:
            return False

        def submit(self) -> bool:
            submitted.append(True)
            return True

        def get_answers(self) -> dict[str, str]:
            return {}

        def move_up(self) -> None:
            pass

        def move_down(self) -> None:
            pass

        def save_other_draft(self, text: str) -> None:
            pass

        def get_other_draft(self) -> str:
            return ""

    delegate = QuestionPromptDelegate(
        _Panel(),
        on_advance=lambda: None,
        on_invalidate=lambda: None,
    )

    buffer = Buffer(document=Document(text="draft", cursor_position=5))
    event = type("_Event", (), {"current_buffer": buffer})()

    delegate.handle_running_prompt_key("enter", event)

    assert buffer.text == ""
    assert submitted == [True]


def test_running_prompt_handles_approval_panel_keys_and_clears_buffer() -> None:
    view = object.__new__(_PromptLiveView)
    view._turn_ended = False
    view._current_question_panel = None
    view._current_approval_request_panel = object()
    view._btw_modal = None

    dispatched: list[object] = []
    view.dispatch_keyboard_event = lambda event: dispatched.append(event)
    view._flush_prompt_refresh = lambda: None

    buffer = Buffer(document=Document(text="draft", cursor_position=5))
    event = type("_Event", (), {"current_buffer": buffer})()

    assert view.should_handle_running_prompt_key("1") is True

    view.handle_running_prompt_key("down", event)

    assert buffer.text == ""
    assert dispatched == [shell_visualize.KeyEvent.DOWN]


def test_running_prompt_escape_sets_cancel_event() -> None:
    view = object.__new__(_PromptLiveView)
    view._current_approval_request_panel = None
    view._turn_ended = False
    view._cancel_event = asyncio.Event()

    assert view.should_handle_running_prompt_key("escape") is True

    event = type("_Event", (), {"app": None, "current_buffer": Buffer()})()
    view.handle_running_prompt_key("escape", event)

    assert view._cancel_event.is_set()


def test_question_delegate_clears_buffer_when_exiting_other_input_mode() -> None:
    QuestionPromptDelegate = shell_visualize.QuestionPromptDelegate

    resolved: list[object] = []

    class _Panel:
        has_expandable_content = False
        is_multi_select = False

        class request:
            @staticmethod
            def resolve(x):
                resolved.append(x)

        @staticmethod
        def should_prompt_other_input() -> bool:
            return False

    advanced: list[bool] = []
    delegate = QuestionPromptDelegate(
        _Panel(),
        on_advance=lambda: (advanced.append(True), None)[-1],
        on_invalidate=lambda: None,
    )
    delegate._awaiting_other_input = True

    buffer = Buffer(document=Document(text="draft", cursor_position=5))
    event = type("_Event", (), {"current_buffer": buffer})()

    delegate.handle_running_prompt_key("escape", event)

    assert delegate._awaiting_other_input is False
    assert buffer.text == ""
    assert resolved == [{}]
    assert advanced == [True]


# ---------------------------------------------------------------------------
# Inline Other input: draft save/restore across navigation
# ---------------------------------------------------------------------------

QuestionRequestPanel = shell_visualize.QuestionRequestPanel
QuestionPromptDelegate = shell_visualize.QuestionPromptDelegate


def _make_two_question_request():
    """Create a QuestionRequest with two single-select questions."""
    from pythinker_code.wire.types import QuestionItem, QuestionOption
    from pythinker_code.wire.types import QuestionRequest as QR

    return QR(
        id="qr-test",
        tool_call_id="tc-test",
        questions=[
            QuestionItem(
                question="Pick a framework",
                header="Q1",
                options=[
                    QuestionOption(label="React"),
                    QuestionOption(label="Vue"),
                ],
            ),
            QuestionItem(
                question="Pick a language",
                header="Q2",
                options=[
                    QuestionOption(label="TypeScript"),
                    QuestionOption(label="JavaScript"),
                ],
            ),
        ],
    )


def _make_delegate_with_panel(panel):
    """Create a QuestionPromptDelegate with a buffer text provider backed by a real Buffer."""
    buf = Buffer()
    delegate = QuestionPromptDelegate(
        panel,
        on_advance=lambda: None,
        on_invalidate=lambda: None,
        buffer_text_provider=lambda: buf.text,
    )
    return delegate, buf


def test_inline_other_draft_survives_up_down_navigation():
    """Type in Other, press UP to leave, press DOWN to return — draft is restored."""
    panel = QuestionRequestPanel(_make_two_question_request())
    delegate, buf = _make_delegate_with_panel(panel)

    # Navigate to Other (last option, index 2)
    panel._selected_index = len(panel._options) - 1
    assert panel.is_other_selected

    # Simulate typing
    buf.set_document(Document(text="my custom answer", cursor_position=16), bypass_readonly=True)

    # Press UP — should save draft and move away
    event = type("_Event", (), {"current_buffer": buf})()
    delegate.handle_running_prompt_key("up", event)

    assert not panel.is_other_selected
    assert buf.text == ""  # buffer cleared

    # Press DOWN — should return to Other and restore draft
    delegate.handle_running_prompt_key("down", event)

    assert panel.is_other_selected
    assert buf.text == "my custom answer"


def test_inline_other_draft_survives_tab_switch():
    """Type in Other on Q1, switch to Q2, switch back — draft is restored."""
    panel = QuestionRequestPanel(_make_two_question_request())
    delegate, buf = _make_delegate_with_panel(panel)

    # Navigate to Other on Q1
    panel._selected_index = len(panel._options) - 1
    assert panel.is_other_selected

    # Simulate typing
    buf.set_document(Document(text="custom framework", cursor_position=16), bypass_readonly=True)

    event = type("_Event", (), {"current_buffer": buf})()

    # Press RIGHT — switch to Q2
    delegate.handle_running_prompt_key("right", event)

    assert panel._current_question_index == 1
    assert buf.text == ""  # buffer cleared on Q2

    # Press LEFT — switch back to Q1
    delegate.handle_running_prompt_key("left", event)

    assert panel._current_question_index == 0
    assert panel.is_other_selected
    assert buf.text == "custom framework"


def test_inline_other_draft_cleared_after_submit():
    """After submitting Other text, the draft should not reappear."""
    panel = QuestionRequestPanel(_make_two_question_request())

    advanced: list[bool] = []
    delegate, buf = _make_delegate_with_panel(panel)
    delegate._on_advance = lambda: (advanced.append(True), None)[-1]

    # Navigate to Other on Q1
    panel._selected_index = len(panel._options) - 1

    # Type and submit
    buf.set_document(Document(text="Svelte", cursor_position=6), bypass_readonly=True)
    event = type("_Event", (), {"current_buffer": buf})()
    delegate.handle_running_prompt_key("enter", event)

    # Q1 should be answered
    assert panel._answers.get("Pick a framework") == "Svelte"
    assert buf.text == ""

    # Verify draft is cleared (check on the panel directly since advance was called)
    assert panel._other_drafts.get(0) is None


def test_question_panel_hides_input_buffer():
    """Question modal should always hide the input buffer, not just when Other is selected."""
    panel = QuestionRequestPanel(_make_two_question_request())
    delegate, _buf = _make_delegate_with_panel(panel)

    # Non-Other selected
    panel._selected_index = 0
    assert not panel.is_other_selected
    assert delegate.running_prompt_hides_input_buffer() is True

    # Other selected
    panel._selected_index = len(panel._options) - 1
    assert panel.is_other_selected
    assert delegate.running_prompt_hides_input_buffer() is True


def test_inline_other_renders_typed_text_in_panel():
    """When Other is selected, the panel renders the buffer text inline."""
    panel = QuestionRequestPanel(_make_two_question_request())
    delegate, buf = _make_delegate_with_panel(panel)

    # Navigate to Other
    panel._selected_index = len(panel._options) - 1

    # Type something
    buf.set_document(Document(text="Solid.js", cursor_position=8), bypass_readonly=True)

    # Render — should contain the typed text
    rendered = delegate.render_running_prompt_body(120)
    import re

    plain = re.sub(r"\x1b\[[^m]*m", "", rendered.value)
    assert "Solid.js" in plain


def test_inline_other_allows_text_input_only_when_other_selected():
    """Text input is only allowed when Other is the selected option."""
    panel = QuestionRequestPanel(_make_two_question_request())
    delegate, _buf = _make_delegate_with_panel(panel)

    # Non-Other: no text input
    panel._selected_index = 0
    assert delegate.running_prompt_allows_text_input() is False

    # Other: text input allowed
    panel._selected_index = len(panel._options) - 1
    assert delegate.running_prompt_allows_text_input() is True


# ---------------------------------------------------------------------------
# Approval inline feedback tests
# ---------------------------------------------------------------------------

ApprovalPromptDelegate = shell_visualize.ApprovalPromptDelegate
ApprovalRequestPanel = shell_visualize.ApprovalRequestPanel


def _make_approval_request(request_id: str = "req-1") -> ApprovalRequest:
    return ApprovalRequest(
        id=request_id,
        tool_call_id=f"call-{request_id}",
        sender="Shell",
        action="run command",
        description="echo hello",
    )


def _make_approval_delegate(request=None):
    """Create an ApprovalPromptDelegate with a real buffer for feedback testing."""
    if request is None:
        request = _make_approval_request()
    buf = Buffer()
    responses: list[tuple[str, str, str]] = []
    delegate = ApprovalPromptDelegate(
        request,
        on_response=lambda req, resp, feedback="": responses.append((req.id, resp, feedback)),
        buffer_state_provider=lambda: (buf.text, buf.cursor_position),
    )
    return delegate, buf, responses


def test_approval_panel_has_four_options():
    """Approval panel should have 4 options: approve, approve_session, reject, reject+feedback."""
    panel = ApprovalRequestPanel(_make_approval_request())
    assert len(panel.options) == 4
    assert panel.options[0][1] == "approve"
    assert panel.options[1][1] == "approve_for_session"
    assert panel.options[2][1] == "reject"
    assert panel.options[3][1] == "reject"


def test_approval_feedback_option_enables_text_input():
    """Selecting option 4 should enable inline text input."""
    delegate, _buf, _ = _make_approval_delegate()

    # Options 0-2: no text input
    for i in range(3):
        delegate._panel.selected_index = i
        assert delegate.running_prompt_allows_text_input() is False

    # Option 3 (feedback): text input enabled
    delegate._panel.selected_index = 3
    assert delegate.running_prompt_allows_text_input() is True
    assert delegate.running_prompt_hides_input_buffer() is True


def test_approval_feedback_renders_inline_input():
    """When feedback option is selected, panel renders typed text inline."""
    delegate, buf, _ = _make_approval_delegate()
    delegate._panel.selected_index = 3

    buf.set_document(Document(text="use a safer command", cursor_position=19), bypass_readonly=True)

    rendered = delegate.render_running_prompt_body(120)
    import re

    plain = re.sub(r"\x1b\[[^m]*m", "", rendered.value)
    assert "use a safer command" in plain
    assert "Type your feedback" in plain


def test_approval_feedback_cursor_markup_in_middle():
    """When the cursor is in the middle, the helper wraps the character under
    it with a reverse-video span — mimicking a terminal's native block cursor."""
    from rich.text import Span

    from pythinker_code.ui.shell.visualize._approval_panel import _render_feedback_with_cursor

    # Cursor at position 2 ("he|llo world" — on the first 'l').
    out = _render_feedback_with_cursor("hello world", 2)
    assert out.plain == "hello world"
    assert Span(2, 3, "reverse") in out.spans

    # Cursor at start.
    out = _render_feedback_with_cursor("hello world", 0)
    assert out.plain == "hello world"
    assert Span(0, 1, "reverse") in out.spans


def test_approval_feedback_cursor_markup_at_end():
    """When the cursor sits past the last character, a trailing block glyph
    is emitted (the reverse-video trick requires a character to invert)."""
    from pythinker_code.ui.shell.visualize._approval_panel import _render_feedback_with_cursor

    assert _render_feedback_with_cursor("abc", 3).plain == "abc\u2588"
    # Past-end cursor (defensive) also falls through to the trailing-block branch.
    assert _render_feedback_with_cursor("abc", 10).plain == "abc\u2588"
    # Empty text.
    assert _render_feedback_with_cursor("", 0).plain == "\u2588"
    # None means "unknown" — same fallback as end-of-text.
    assert _render_feedback_with_cursor("abc", None).plain == "abc\u2588"


def test_approval_feedback_cursor_markup_escapes_rich_metachars():
    """Rich markup tags typed by the user (e.g. ``[bold]``) must render as
    literal text, not be interpreted as styles — ``Text()`` takes plain strings."""
    from rich.text import Span

    from pythinker_code.ui.shell.visualize._approval_panel import _render_feedback_with_cursor

    # The "[bold]" prefix stays verbatim; the reverse-cursor span is still
    # applied around the character under the cursor ('h').
    out = _render_feedback_with_cursor("[bold]hello", 6)
    assert out.plain == "[bold]hello"
    assert Span(6, 7, "reverse") in out.spans


@pytest.mark.asyncio
async def test_approval_feedback_submit_sends_reject_with_text():
    """Enter with text in feedback mode should reject with feedback."""
    delegate, buf, responses = _make_approval_delegate()
    delegate._panel.selected_index = 3

    buf.set_document(Document(text="use rm -i instead", cursor_position=17), bypass_readonly=True)
    event = type("_Event", (), {"current_buffer": buf})()
    delegate.handle_running_prompt_key("enter", event)

    assert len(responses) == 1
    assert responses[0][1] == "reject"
    assert responses[0][2] == "use rm -i instead"
    assert buf.text == ""


def test_approval_feedback_empty_enter_does_not_submit():
    """Enter with empty buffer in feedback mode should not submit."""
    delegate, buf, responses = _make_approval_delegate()
    delegate._panel.selected_index = 3

    event = type("_Event", (), {"current_buffer": buf})()
    delegate.handle_running_prompt_key("enter", event)

    assert len(responses) == 0
    assert not delegate._panel.request.resolved


@pytest.mark.asyncio
async def test_approval_feedback_escape_rejects_without_feedback():
    """Escape in feedback mode should reject without feedback text."""
    delegate, buf, responses = _make_approval_delegate()
    delegate._panel.selected_index = 3
    buf.set_document(Document(text="draft", cursor_position=5), bypass_readonly=True)

    event = type("_Event", (), {"current_buffer": buf})()
    delegate.handle_running_prompt_key("escape", event)

    assert len(responses) == 1
    assert responses[0][1] == "reject"
    assert responses[0][2] == ""
    assert buf.text == ""


def test_approval_feedback_up_navigates_away():
    """UP in feedback mode should navigate to option 3 and clear buffer."""
    delegate, buf, _ = _make_approval_delegate()
    delegate._panel.selected_index = 3
    buf.set_document(Document(text="draft", cursor_position=5), bypass_readonly=True)

    event = type("_Event", (), {"current_buffer": buf})()
    delegate.handle_running_prompt_key("up", event)

    assert delegate._panel.selected_index == 2  # moved to "Reject"
    assert buf.text == ""


def test_approval_number_4_selects_feedback_without_submitting():
    """Pressing 4 should select the feedback option but NOT auto-submit."""
    delegate, buf, responses = _make_approval_delegate()
    event = type("_Event", (), {"current_buffer": buf})()

    delegate.handle_running_prompt_key("4", event)

    assert delegate._panel.selected_index == 3
    assert delegate._is_inline_feedback_active()
    assert len(responses) == 0  # should NOT submit yet


def test_approval_feedback_draft_survives_navigation():
    """Type in feedback, navigate away, navigate back — draft is restored."""
    delegate, buf, _ = _make_approval_delegate()
    delegate._panel.selected_index = 3  # feedback option

    buf.set_document(Document(text="use safer cmd", cursor_position=13), bypass_readonly=True)
    event = type("_Event", (), {"current_buffer": buf})()

    # UP — leave feedback option, draft saved
    delegate.handle_running_prompt_key("up", event)
    assert delegate._panel.selected_index == 2
    assert buf.text == ""
    assert delegate._feedback_draft == "use safer cmd"

    # DOWN — back to feedback option, draft restored
    delegate.handle_running_prompt_key("down", event)
    assert delegate._panel.selected_index == 3
    assert buf.text == "use safer cmd"


@pytest.mark.asyncio
async def test_approval_feedback_draft_cleared_after_submit():
    """After submitting feedback, draft should be cleared."""
    delegate, buf, responses = _make_approval_delegate()
    delegate._panel.selected_index = 3

    buf.set_document(Document(text="do X instead", cursor_position=12), bypass_readonly=True)
    event = type("_Event", (), {"current_buffer": buf})()

    delegate.handle_running_prompt_key("enter", event)

    assert responses[0][2] == "do X instead"
    assert delegate._feedback_draft == ""


def test_approval_feedback_draft_cleared_on_new_request():
    """set_request should clear feedback draft."""
    delegate, buf, _ = _make_approval_delegate()
    delegate._feedback_draft = "old draft"

    delegate.set_request(_make_approval_request("req-new"))
    assert delegate._feedback_draft == ""


# ---------------------------------------------------------------------------
# ApprovalRequest wire-level feedback propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_request_resolve_carries_feedback():
    """ApprovalRequest.resolve() should store feedback accessible via .feedback property."""
    request = _make_approval_request("req-fb-wire")

    request.resolve("reject", feedback="use a different command")

    assert request.resolved is True
    assert await request.wait() == "reject"
    assert request.feedback == "use a different command"


@pytest.mark.asyncio
async def test_approval_request_resolve_without_feedback_defaults_empty():
    """ApprovalRequest.resolve() without feedback should default to empty string."""
    request = _make_approval_request("req-fb-default")

    request.resolve("approve")

    assert request.resolved is True
    assert request.feedback == ""


@pytest.mark.asyncio
async def test_approval_request_feedback_available_before_wait():
    """Feedback should be readable immediately after resolve, without awaiting wait()."""
    request = _make_approval_request("req-fb-sync")

    request.resolve("reject", feedback="try rm -i instead")

    # feedback is available synchronously, no need to await
    assert request.feedback == "try rm -i instead"


def test_background_status_shows_elapsed_tokens_and_rate(monkeypatch) -> None:
    """The line above the input carries (elapsed, ↓ tokens, t/s) — the same
    metadata design as the live view's working indicator."""
    import pythinker_code.ui.shell.prompt as prompt_module
    from pythinker_code.soul import live_tokens

    live_tokens.reset_for_tests()
    session = object.__new__(CustomPromptSession)
    session._background_task_count_provider = lambda: BgTaskCounts(agent=2)
    session._latest_todos = ()
    state = {"now": 100.0, "output_tokens": 0}
    monkeypatch.setattr(prompt_module.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(
        live_tokens,
        "get_total_output_tokens",
        lambda: state["output_tokens"],
    )

    def render() -> str:
        rendered = CustomPromptSession._render_background_working_status(session, 120)
        return "".join(item[1] for item in rendered)

    first = render()
    assert "(<1s" in first  # stretch just started — no output delta yet

    state["now"], state["output_tokens"] = 100.0, 40_000
    second = render()
    assert "(<1s, ↓ 40k tokens)" in second  # no rate until the window fills
    assert session._bg_last_active_at == 100.0
    state["now"] = 101.0
    assert session._bg_refresh_active() is True
    state["now"] = 103.0
    assert session._bg_refresh_active() is False

    state["now"], state["output_tokens"] = 100.4, 40_400
    render()
    state["now"], state["output_tokens"] = 100.8, 40_800
    third = render()
    assert "(<1s, ↓ 40.8k tokens, 1000 t/s)" in third

    # Draining background work resets the trackers.
    session._background_task_count_provider = lambda: BgTaskCounts()
    assert render() == ""
    assert session._bg_status_started_at is None
    assert session._bg_status_start_tokens is None
    assert session._bg_last_active_at is None


def test_background_pure_bash_uses_fixed_label_not_verb_spinner() -> None:
    """Pure-bash background work (npm dev, docker run) shows a fixed label,
    not the agent verb spinner ('Composing…' / 'Brewing…')."""
    session = object.__new__(CustomPromptSession)
    session._background_task_count_provider = lambda: BgTaskCounts(bash=1)

    rendered = CustomPromptSession._render_background_working_status(session, 80)
    text = "".join(item[1] for item in rendered)

    assert "Running in background…" in text
    # The whimsical agent verbs must not leak into the pure-bash path.
    assert "Composing" not in text
    assert "Brewing" not in text
    # The braille active marker is still present.
    assert text.strip()


def test_background_mixed_bash_agent_keeps_verb_spinner(monkeypatch) -> None:
    """When agent work is also running, the verb spinner stays — the agent
    is actively working."""
    import pythinker_code.ui.shell.prompt as prompt_module

    monkeypatch.setattr(prompt_module.time, "monotonic", lambda: 0.5)
    session = object.__new__(CustomPromptSession)
    session._background_task_count_provider = lambda: BgTaskCounts(bash=1, agent=1)

    rendered = CustomPromptSession._render_background_working_status(session, 80)
    text = "".join(item[1] for item in rendered)

    assert "Running in background…" not in text
    assert "…" in text  # verb spinner with ellipsis


def test_background_status_truncates_after_dropping_metadata(monkeypatch) -> None:
    import pythinker_code.ui.shell.prompt as prompt_module

    monkeypatch.setattr(prompt_module.time, "monotonic", lambda: 0.0)
    session = object.__new__(CustomPromptSession)
    session._background_task_count_provider = lambda: BgTaskCounts(bash=1)
    session._background_status_metadata = lambda now: "metadata"
    session._latest_todos = ()

    rendered = CustomPromptSession._render_background_working_status(session, 8)
    text = "".join(item[1] for item in rendered)

    assert "metadata" not in text
    assert prompt_module._display_width(text) <= 8


def test_bg_refresh_active_drops_to_idle_when_quiet(monkeypatch) -> None:
    """A quiet background task (no token flow past the threshold) signals the
    refresh loop to drop from 0.1s to 1.0s."""
    import pythinker_code.ui.shell.prompt as prompt_module

    session = object.__new__(CustomPromptSession)
    base = prompt_module.time.monotonic()
    session._bg_last_active_at = base

    # Freshly-spawned: within the quiet window → still active.
    monkeypatch.setattr(prompt_module.time, "monotonic", lambda: base + 0.5)
    assert session._bg_refresh_active() is True

    # Past the quiet threshold → idle refresh.
    monkeypatch.setattr(prompt_module.time, "monotonic", lambda: base + 5.0)
    assert session._bg_refresh_active() is False


# ---------------------------------------------------------------------------
# Transient slash-command output panel (mid-task menus appear and disappear)
# ---------------------------------------------------------------------------


class _InvalidatingPromptSession:
    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate(self) -> None:
        self.invalidations += 1


def _make_prompt_live_view(**kwargs) -> Any:
    return _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _InvalidatingPromptSession()),
        steer=lambda _content: None,
        **kwargs,
    )


def test_transient_command_output_appears_then_expires(monkeypatch) -> None:
    view = _make_prompt_live_view()
    clock = {"now": 100.0}
    monkeypatch.setattr(_interactive_mod.time, "monotonic", lambda: clock["now"])

    view._show_transient_command_output("Enabled  on")
    body = view.render_running_prompt_body(80).value
    assert "Enabled  on" in body

    clock["now"] += _interactive_mod._TRANSIENT_COMMAND_PANEL_S + 0.1
    assert view.render_running_prompt_body(80).value == ""


def test_transient_command_output_dismissed_by_new_input() -> None:
    view = _make_prompt_live_view()
    view._show_transient_command_output("menu line")
    assert "menu line" in view.render_running_prompt_body(80).value

    view.handle_local_input(
        UserInput(
            mode=PromptMode.AGENT,
            command="continue please",
            resolved_command="continue please",
            content=[TextPart(text="continue please")],
        )
    )
    assert "menu line" not in view.render_running_prompt_body(80).value
    # The new input itself still queued normally.
    assert len(view._queued_messages) == 1


def test_transient_command_output_truncates_verbose_commands() -> None:
    view = _make_prompt_live_view()
    view._show_transient_command_output(
        "\n".join(f"line-{i}" for i in range(100)),
    )
    panel = view._current_transient_command_output()
    assert panel is not None
    lines = panel.splitlines()
    assert len(lines) == _interactive_mod._TRANSIENT_COMMAND_PANEL_MAX_LINES + 1
    assert lines[-1].startswith("… +")


def test_transient_panel_renders_alongside_queued_messages() -> None:
    view = _make_prompt_live_view()
    view._show_transient_command_output("panel content")
    view._queued_messages.append(
        UserInput(
            mode=PromptMode.AGENT,
            command="queued msg",
            resolved_command="queued msg",
            content=[TextPart(text="queued msg")],
        )
    )
    body = view.render_running_prompt_body(80).value
    assert "panel content" in body
    assert "queued msg" in body


@pytest.mark.asyncio
async def test_intercepted_shell_command_output_is_captured_not_printed(capsys) -> None:
    """Mid-task slash output must land in the transient panel, not scrollback."""
    import pythinker_code.ui.shell.slash  # noqa: F401  — registers /version
    from pythinker_code.ui.shell.console import console as real_console

    async def runner(call) -> None:
        real_console.print(f"menu for /{call.name} [with|brackets]")

    view = _make_prompt_live_view(shell_command_runner=runner)
    view._turn_ended = False
    consumed = view._intercept_shell_command(
        UserInput(
            mode=PromptMode.AGENT,
            command="/version",
            resolved_command="/version",
            content=[TextPart(text="/version")],
        )
    )
    assert consumed is True
    for _ in range(100):
        if not view._shell_command_tasks:
            break
        await asyncio.sleep(0.01)
    import re

    visible = re.sub(r"\x1b\[[0-9;]*m", "", view.render_running_prompt_body(120).value)
    assert "menu for /version" in visible
    assert "❯ /version" in visible  # echo lives inside the panel too
    assert "menu for /version" not in capsys.readouterr().out


def test_reset_prompt_renderer_resets_app_renderer(monkeypatch) -> None:
    import prompt_toolkit.application as pt_application

    view = object.__new__(_PromptLiveView)
    resets: list[str] = []

    class _Renderer:
        def reset(self) -> None:
            resets.append("reset")

    class _App:
        renderer = _Renderer()

    monkeypatch.setattr(pt_application, "get_app_or_none", lambda: _App())
    view._reset_prompt_renderer("test")
    assert resets == ["reset"]


def test_reset_prompt_renderer_survives_no_app_and_renderer_failure(monkeypatch) -> None:
    import prompt_toolkit.application as pt_application

    view = object.__new__(_PromptLiveView)

    monkeypatch.setattr(pt_application, "get_app_or_none", lambda: None)
    view._reset_prompt_renderer("no-app")  # must not raise

    class _BoomRenderer:
        def reset(self) -> None:
            raise RuntimeError("boom")

    class _App:
        renderer = _BoomRenderer()

    monkeypatch.setattr(pt_application, "get_app_or_none", lambda: _App())
    view._reset_prompt_renderer("boom")  # must not raise


def test_resize_change_forces_absolute_prompt_repaint(monkeypatch) -> None:
    view = object.__new__(_PromptLiveView)
    view._last_terminal_size = (80, 24)
    view._resize_recovery_remaining = 0
    view._force_refresh = False
    view._current_terminal_size = lambda: (100, 30)  # type: ignore[method-assign]

    resets: list[str] = []
    monkeypatch.setattr(view, "_reset_prompt_renderer", lambda reason: resets.append(reason))

    view._tick_resize_recovery()

    assert resets == ["resize"]


@pytest.mark.asyncio
async def test_handoff_failure_forces_absolute_prompt_repaint(monkeypatch) -> None:
    class _PromptSession:
        def invalidate(self) -> None:
            pass

    async def _run_in_terminal(func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("terminal suspended")

    monkeypatch.setattr(_interactive_mod, "run_in_terminal", _run_in_terminal)
    monkeypatch.setattr(_live_view_mod.console, "_force_terminal", True)

    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    resets: list[str] = []
    monkeypatch.setattr(view, "_reset_prompt_renderer", lambda reason: resets.append(reason))
    view._pending_scrollback.append((Text("must survive"), True))

    await view._flush_pending_scrollback()

    assert resets == ["handoff-fail"]
