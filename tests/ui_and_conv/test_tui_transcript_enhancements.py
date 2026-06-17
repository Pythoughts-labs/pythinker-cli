"""Transcript affordances for compact TUI tool/question/progress rows."""

from __future__ import annotations

import importlib
import json

import pytest
from pythinker_core.message import ToolCall
from pythinker_core.tooling import ToolOk

from pythinker_code.ui.shell.components import render_plain
from pythinker_code.ui.shell.tool_renderers import clear_tool_renderers, register_builtin_renderers
from pythinker_code.ui.shell.visualize import (
    _LiveView,
    _ProgressNoteBlock,
    _QuestionAnsweredBlock,
)
from pythinker_code.wire.types import (
    ProgressNote,
    QuestionAnswered,
    StatusUpdate,
    ToolResult,
    TurnBegin,
)

_live_view_mod = importlib.import_module("pythinker_code.ui.shell.visualize._live_view")


@pytest.fixture(autouse=True)
def _reset_live_tokens():
    from pythinker_code.soul.live_tokens import reset_for_tests

    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture(autouse=True)
def _builtin_renderers():
    clear_tool_renderers()
    register_builtin_renderers()
    yield
    clear_tool_renderers()


def _tool_call(name: str, args: dict[str, object]) -> ToolCall:
    return ToolCall(
        id="tc-1",
        function=ToolCall.FunctionBody(name=name, arguments=json.dumps(args)),
    )


def test_finished_expandable_tool_card_remains_available_after_flush(monkeypatch) -> None:
    printed: list[object] = []
    monkeypatch.setattr(
        _live_view_mod.console,
        "print",
        lambda *args, **_kwargs: printed.extend(args),
    )

    view = _LiveView(StatusUpdate())
    body = "\n".join(f"line {i}" for i in range(20))

    view.dispatch_wire_message(_tool_call("ReadFile", {"path": "/repo/big.py"}))
    view.dispatch_wire_message(ToolResult(tool_call_id="tc-1", return_value=ToolOk(output=body)))

    assert "tc-1" not in view._tool_call_blocks
    assert view.has_expandable_panel() is True

    block = view._completed_expandable_tool_card()
    assert block is not None
    expanded = render_plain(block.render_expanded(), width=100)
    assert "Read 20 lines from big.py" in expanded
    assert "line 0" in expanded
    assert "line 19" in expanded


def test_question_answered_block_matches_reference_transcript_shape() -> None:
    block = _QuestionAnsweredBlock(
        QuestionAnswered(
            request_id="q-1",
            tool_call_id="tc-ask",
            answers={"How should I proceed?": "Keep going now"},
        )
    )

    rendered = render_plain(block.compose(), width=100)

    assert "User answered Pythinker's questions:" in rendered
    assert "How should I proceed?" in rendered
    assert "→ Keep going now" in rendered


def test_progress_note_block_renders_checkpoint_style_note() -> None:
    block = _ProgressNoteBlock(
        ProgressNote(
            title="Checkpoint — Phase A is underway",
            body="First increment landed. Remaining work is runner/context wiring.",
        )
    )

    rendered = render_plain(block.compose(), width=100)

    assert "Checkpoint — Phase A is underway" in rendered
    assert "First increment landed" in rendered


def test_progress_note_block_strips_ansi_from_title() -> None:
    """A model-controlled progress-note title must not pass terminal escapes through.

    The title is rendered via rich Text(), which forwards raw control bytes, so an
    OSC 52 clipboard-write or CSI sequence smuggled into the title would reach the
    terminal unless sanitised — the body path already goes through PythinkerMarkdown.
    """
    block = _ProgressNoteBlock(
        ProgressNote(
            title="\x1b]52;c;ZXZpbA==\x07Checkpoint\x1b[31m!",
            body="",
        )
    )

    rendered = render_plain(block.compose(), width=100)

    assert "\x1b]52" not in rendered
    assert "Checkpoint" in rendered


def test_working_indicator_includes_turn_output_tokens(monkeypatch) -> None:
    from pythinker_code.soul.live_tokens import add_total_output_tokens, reset_for_tests

    reset_for_tests()
    try:
        monkeypatch.setattr(_live_view_mod.time, "monotonic", lambda: 10.0)
        view = _LiveView(StatusUpdate())
        view.dispatch_wire_message(TurnBegin(user_input="work"))
        # Output produced this turn (main agent + any subagents) — the live
        # readout tracks throughput, not the static context-window size.
        add_total_output_tokens(110_800)

        rendered = render_plain(view._working_indicator(), width=100)

        assert "110.8k" in rendered
        assert "tokens" in rendered
    finally:
        reset_for_tests()
