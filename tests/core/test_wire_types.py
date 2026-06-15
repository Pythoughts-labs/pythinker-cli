from __future__ import annotations

from pythinker_code.wire.types import (
    AgentListDelta,
    ContextOverflowRecovered,
    SubagentToolFallback,
    TodoListUpdated,
    ToolUseSkipped,
    WireMessageEnvelope,
)


def test_todo_list_updated_round_trip_via_envelope() -> None:
    evt = TodoListUpdated(items=(("Investigate", "in_progress"),), complete=False, source="tool")
    env = WireMessageEnvelope.from_wire_message(evt)
    assert env.type == "TodoListUpdated"
    assert env.to_wire_message() == evt


def test_subagent_tool_fallback_round_trip() -> None:
    evt = SubagentToolFallback(
        reason="unavailable_agent_type",
        requested_type="missing-coder",
        available_types=("explore", "plan"),
    )
    env = WireMessageEnvelope.from_wire_message(evt)
    assert env.to_wire_message() == evt


def test_agent_list_delta_round_trip() -> None:
    evt = AgentListDelta(items=("- explore: explore code (Tools: *)",), complete=True)
    env = WireMessageEnvelope.from_wire_message(evt)
    assert env.to_wire_message() == evt


def test_tool_use_skipped_round_trip() -> None:
    evt = ToolUseSkipped(
        tool_call_id="tc_1",
        tool_name="Shell",
        reason="concurrent_inflight",
        resumed=True,
    )
    env = WireMessageEnvelope.from_wire_message(evt)
    assert env.to_wire_message() == evt


def test_context_overflow_recovered_round_trip() -> None:
    evt = ContextOverflowRecovered(outcome="recovered", trigger_step=3)
    env = WireMessageEnvelope.from_wire_message(evt)
    assert env.to_wire_message() == evt


def test_new_wire_events_are_frozen_and_forbid_extra_fields() -> None:
    evt = ToolUseSkipped(tool_call_id="tc_1", tool_name="Shell", reason="dedup")

    try:
        evt.resumed = True  # type: ignore[misc]
    except Exception as exc:
        assert type(exc).__name__ == "ValidationError"
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("ToolUseSkipped must be frozen")

    try:
        TodoListUpdated.model_validate(
            {"items": [], "complete": True, "source": "tool", "unexpected": True}
        )
    except Exception as exc:
        assert type(exc).__name__ == "ValidationError"
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("TodoListUpdated must reject extra fields")
