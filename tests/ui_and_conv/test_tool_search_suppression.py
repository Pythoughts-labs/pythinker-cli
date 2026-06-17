"""Tests for ToolSearch scrollback suppression.

Multiple ToolSearch calls in a single turn must produce at most one scrollback
entry — the last one. Intermediate probes are discarded silently, mirroring the
blackbox reference's ``isAbsorbedSilently`` behaviour for ToolSearch.
"""

from __future__ import annotations

import importlib

import pytest
from pythinker_core.message import ToolCall
from pythinker_core.tooling import ToolResult, ToolReturnValue

from pythinker_code.soul.live_tokens import reset_for_tests
from pythinker_code.ui.shell.visualize import _LiveView
from pythinker_code.wire.types import StatusUpdate, StepRetry, TextPart, TurnBegin

_live_view_module = importlib.import_module("pythinker_code.ui.shell.visualize._live_view")


@pytest.fixture(autouse=True)
def _reset_tokens():
    reset_for_tests()
    yield
    reset_for_tests()


def _ts_call(call_id: str = "ts-1") -> ToolCall:
    return ToolCall(
        id=call_id,
        function=ToolCall.FunctionBody(name="ToolSearch", arguments='{"query":null}'),
    )


def _ts_result(call_id: str = "ts-1", n_tools: int = 10) -> ToolResult:
    tools_text = "\n".join(f"- Tool{i} - description {i}" for i in range(n_tools))
    return ToolResult(
        tool_call_id=call_id,
        return_value=ToolReturnValue(
            is_error=False,
            output=tools_text,
            message="",
            display=[],
        ),
    )


def _make_view(monkeypatch, printed: list) -> _LiveView:
    monkeypatch.setattr(_live_view_module, "_print_action_block", lambda b: printed.append(b))
    monkeypatch.setattr(_live_view_module, "emit_scrollback_block", lambda *a, **kw: None)
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="work"))
    return view


def test_single_tool_search_suppressed_until_turn_end(monkeypatch) -> None:
    """A single ToolSearch should not print mid-turn; it flushes at turn end."""
    printed: list = []
    view = _make_view(monkeypatch, printed)

    view.dispatch_wire_message(_ts_call("ts-1"))
    view.dispatch_wire_message(_ts_result("ts-1"))
    assert len(printed) == 0, "ToolSearch must not print to scrollback mid-turn"

    view.cleanup(is_interrupt=False)
    assert len(printed) == 1, "ToolSearch must print exactly once at turn end"


def test_consecutive_tool_searches_collapse_to_one(monkeypatch) -> None:
    """Three consecutive ToolSearch calls → exactly one scrollback entry."""
    printed: list = []
    view = _make_view(monkeypatch, printed)

    for i in range(3):
        view.dispatch_wire_message(_ts_call(f"ts-{i}"))
        view.dispatch_wire_message(_ts_result(f"ts-{i}", n_tools=i + 10))

    assert len(printed) == 0, "No ToolSearch should print mid-turn"
    view.cleanup(is_interrupt=False)
    assert len(printed) == 1, f"Expected 1 collapsed ToolSearch print, got {len(printed)}"


def test_tool_search_flushes_before_real_tool(monkeypatch) -> None:
    """Held ToolSearch prints before a subsequent real tool, in order."""
    printed: list = []
    view = _make_view(monkeypatch, printed)

    view.dispatch_wire_message(_ts_call("ts-1"))
    view.dispatch_wire_message(_ts_result("ts-1"))
    assert len(printed) == 0

    edit_call = ToolCall(
        id="edit-1",
        function=ToolCall.FunctionBody(name="Edit", arguments='{"path":"f.py","content":"x"}'),
    )
    view.dispatch_wire_message(edit_call)
    view.dispatch_wire_message(
        ToolResult(
            tool_call_id="edit-1",
            return_value=ToolReturnValue(is_error=False, output="ok", message="", display=[]),
        )
    )
    assert len(printed) == 2, "ToolSearch + Edit must both have printed"


def test_tool_search_does_not_cross_text_boundary(monkeypatch) -> None:
    """ToolSearch groups are not collapsed across an assistant text block."""
    printed: list = []
    text_emitted: list = []
    monkeypatch.setattr(_live_view_module, "_print_action_block", lambda b: printed.append(b))
    monkeypatch.setattr(
        _live_view_module, "emit_scrollback_block", lambda *a, **kw: text_emitted.append(a)
    )
    view = _LiveView(StatusUpdate())
    view.dispatch_wire_message(TurnBegin(user_input="work"))

    # First ToolSearch, then assistant text, then another ToolSearch.
    view.dispatch_wire_message(_ts_call("ts-1"))
    view.dispatch_wire_message(_ts_result("ts-1"))
    # Text forces a flush of the first TS group and the text itself.
    view.dispatch_wire_message(TextPart(text="Thinking..."))
    view.dispatch_wire_message(_ts_call("ts-2"))
    view.dispatch_wire_message(_ts_result("ts-2"))
    view.cleanup(is_interrupt=False)

    assert len(printed) == 2, "ToolSearch blocks separated by text must not collapse together"


def test_tool_search_discarded_on_retry(monkeypatch) -> None:
    """A held ToolSearch from a failed step attempt must not appear in scrollback."""
    printed: list = []
    view = _make_view(monkeypatch, printed)

    view.dispatch_wire_message(_ts_call("ts-1"))
    view.dispatch_wire_message(_ts_result("ts-1"))
    assert len(printed) == 0

    view.discard_retry_attempt(
        StepRetry(n=1, next_attempt=2, max_attempts=3, wait_s=0.0, error_type="ValueError")
    )
    view.cleanup(is_interrupt=False)
    assert len(printed) == 0, "Retried ToolSearch must not appear in scrollback"
