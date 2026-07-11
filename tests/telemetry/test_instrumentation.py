"""Tests for telemetry event behavior and schema correctness.

These tests exercise the telemetry API directly and verify that calls to
track() and related helpers produce the expected event names, properties,
queue entries, and sink-forwarded payloads under the correct conditions.
They do NOT verify that specific production UI/soul call sites are still
instrumented — that coverage belongs in integration tests.
Transport/infrastructure tests are in test_telemetry.py.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import pythinker_code.telemetry as telemetry_mod
from pythinker_code.telemetry import attach_sink, disable, set_context, track
from pythinker_code.telemetry.sink import EventSink

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_telemetry_state():
    """Reset telemetry module state before each test."""
    telemetry_mod._event_queue.clear()
    telemetry_mod._device_id = None
    telemetry_mod._session_id = None
    telemetry_mod._client_info = None
    telemetry_mod._session_started_sessions.clear()
    telemetry_mod._sink = None
    telemetry_mod._disabled = False
    yield
    telemetry_mod._event_queue.clear()
    telemetry_mod._device_id = None
    telemetry_mod._session_id = None
    telemetry_mod._client_info = None
    telemetry_mod._session_started_sessions.clear()
    telemetry_mod._sink = None
    telemetry_mod._disabled = False


def _collect_events() -> list[dict[str, Any]]:
    """Return a snapshot of queued events."""
    return list(telemetry_mod._event_queue)


def _collect_sink_events(sink_mock: MagicMock) -> list[dict[str, Any]]:
    """Extract events forwarded to a mock sink."""
    return [call[0][0] for call in sink_mock.accept.call_args_list]


# ---------------------------------------------------------------------------
# 1. Slash command counting correctness
# ---------------------------------------------------------------------------


class TestSlashCommandCounting:
    """Verify that slash commands emit exactly one input_command event."""

    def test_shell_slash_command_tracks_once(self):
        """A shell-level slash command emits input_command with the command name."""
        # Simulate what _run_slash_command does: one track call
        track("input_command", command="model")
        events = _collect_events()
        matching = [e for e in events if e["event"] == "input_command"]
        assert len(matching) == 1
        assert matching[0]["properties"]["command"] == "model"

    def test_soul_slash_command_tracks_once(self):
        """A soul-level slash command emits input_command (not double-counted)."""
        # Soul-level commands are tracked at the shell layer before dispatch
        track("input_command", command="compact")
        events = _collect_events()
        matching = [e for e in events if e["event"] == "input_command"]
        assert len(matching) == 1
        assert matching[0]["properties"]["command"] == "compact"

    def test_invalid_command_tracks_separate_event(self):
        """Invalid slash commands emit input_command_invalid, not input_command."""
        track("input_command_invalid")
        events = _collect_events()
        assert any(e["event"] == "input_command_invalid" for e in events)
        assert not any(e["event"] == "input_command" for e in events)

    def test_no_double_counting_shell_and_soul(self):
        """Shell and soul layers must not both emit for the same command invocation."""
        # Simulate: only one track call per command execution path
        track("input_command", command="yolo")
        events = _collect_events()
        cmd_events = [e for e in events if e["event"] == "input_command"]
        assert len(cmd_events) == 1

    def test_command_property_is_string_enum(self):
        """Command property must be a string (enum-like), not an int or bool."""
        track("input_command", command="clear")
        event = _collect_events()[-1]
        assert isinstance(event["properties"]["command"], str)


# ---------------------------------------------------------------------------
# 2. Tool approval path completeness
# ---------------------------------------------------------------------------


class TestToolApprovalPaths:
    """Every approval path must emit exactly one of the two tool tracking events."""

    def test_manual_approve(self):
        """User clicking approve emits tool_approved with approval_mode=manual."""
        track("tool_approved", tool_name="Bash", approval_mode="manual")
        events = _collect_events()
        assert events[-1]["event"] == "tool_approved"
        assert events[-1]["properties"]["tool_name"] == "Bash"
        assert events[-1]["properties"]["approval_mode"] == "manual"

    def test_approve_for_session(self):
        """'Approve for session' emits tool_approved with approval_mode=manual."""
        track("tool_approved", tool_name="WriteFile", approval_mode="manual")
        events = _collect_events()
        assert events[-1]["event"] == "tool_approved"
        assert events[-1]["properties"]["approval_mode"] == "manual"

    def test_yolo_approve(self):
        """Yolo auto-approval emits tool_approved with approval_mode=yolo."""
        track("tool_approved", tool_name="Bash", approval_mode="yolo")
        event = _collect_events()[-1]
        assert event["properties"]["approval_mode"] == "yolo"

    def test_auto_session_approve(self):
        """Session-cached auto-approval emits approval_mode=auto_session."""
        track("tool_approved", tool_name="ReadFile", approval_mode="auto_session")
        event = _collect_events()[-1]
        assert event["properties"]["approval_mode"] == "auto_session"

    def test_manual_reject(self):
        """User clicking reject emits tool_rejected with approval_mode=manual."""
        track("tool_rejected", tool_name="Bash", approval_mode="manual")
        events = _collect_events()
        assert events[-1]["event"] == "tool_rejected"
        assert events[-1]["properties"]["tool_name"] == "Bash"
        assert events[-1]["properties"]["approval_mode"] == "manual"

    def test_cancelled_approval(self):
        """ApprovalCancelledError (e.g. Esc) emits tool_rejected with approval_mode=cancelled."""
        track("tool_rejected", tool_name="Bash", approval_mode="cancelled")
        events = _collect_events()
        assert events[-1]["event"] == "tool_rejected"
        assert events[-1]["properties"]["approval_mode"] == "cancelled"

    def test_approval_events_are_mutually_exclusive(self):
        """Each approval path emits exactly one event — they never overlap."""
        track("tool_approved", tool_name="Bash")
        events = _collect_events()
        approval_events = [e for e in events if e["event"] in ("tool_approved", "tool_rejected")]
        assert len(approval_events) == 1

    def test_tool_name_always_present(self):
        """All tool approval events include tool_name."""
        for event_name in ("tool_approved", "tool_rejected"):
            telemetry_mod._event_queue.clear()
            track(event_name, tool_name="SomeTool")
            event = _collect_events()[-1]
            assert "tool_name" in event["properties"], f"{event_name} missing tool_name"


# ---------------------------------------------------------------------------
# 3. API error classification
# ---------------------------------------------------------------------------


class TestAPIErrorClassification:
    """Verify the error_type mapping in api_error events.

    Tests call the real classifier function, so any drift in the production
    mapping shows up here.
    """

    def _mk_status_error(self, status: int, message: str = ""):
        from pythinker_core.chat_provider import APIStatusError

        exc = APIStatusError.__new__(APIStatusError)
        exc.status_code = status
        exc.args = (message,) if message else ()
        return exc

    def test_429_maps_to_rate_limit(self):
        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, sc = classify_api_error(self._mk_status_error(429))
        assert et == "rate_limit"
        assert sc == 429

    def test_401_maps_to_auth(self):
        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, sc = classify_api_error(self._mk_status_error(401))
        assert et == "auth"
        assert sc == 401

    def test_403_maps_to_auth(self):
        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, _ = classify_api_error(self._mk_status_error(403))
        assert et == "auth"

    def test_500_maps_to_5xx_server(self):
        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, sc = classify_api_error(self._mk_status_error(500))
        assert et == "5xx_server"
        assert sc == 500

    def test_502_maps_to_5xx_server(self):
        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, _ = classify_api_error(self._mk_status_error(502))
        assert et == "5xx_server"

    def test_400_maps_to_4xx_client(self):
        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, sc = classify_api_error(self._mk_status_error(400))
        assert et == "4xx_client"
        assert sc == 400

    def test_422_maps_to_4xx_client(self):
        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, _ = classify_api_error(self._mk_status_error(422))
        assert et == "4xx_client"

    def test_400_with_context_length_maps_to_context_overflow(self):
        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, _ = classify_api_error(self._mk_status_error(400, "Context length exceeded"))
        assert et == "context_overflow"

    def test_400_with_max_tokens_maps_to_context_overflow(self):
        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, _ = classify_api_error(self._mk_status_error(400, "Exceeded max tokens"))
        assert et == "context_overflow"

    def test_400_with_maximum_context_maps_to_context_overflow(self):
        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, _ = classify_api_error(self._mk_status_error(422, "Maximum context window exceeded"))
        assert et == "context_overflow"

    def test_connection_error_maps_to_network(self):
        from pythinker_core.chat_provider import APIConnectionError

        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, sc = classify_api_error(APIConnectionError.__new__(APIConnectionError))
        assert et == "network"
        assert sc is None

    def test_api_timeout_maps_to_timeout(self):
        from pythinker_core.chat_provider import APITimeoutError

        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, _ = classify_api_error(APITimeoutError.__new__(APITimeoutError))
        assert et == "timeout"

    def test_builtin_timeout_maps_to_timeout(self):
        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, _ = classify_api_error(TimeoutError("timed out"))
        assert et == "timeout"

    def test_empty_response_maps_to_empty_response(self):
        from pythinker_core.chat_provider import APIEmptyResponseError

        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, sc = classify_api_error(APIEmptyResponseError.__new__(APIEmptyResponseError))
        assert et == "empty_response"
        assert sc is None

    def test_generic_exception_maps_to_other(self):
        from pythinker_code.soul.pythinkersoul import classify_api_error

        et, sc = classify_api_error(RuntimeError("unexpected"))
        assert et == "other"
        assert sc is None

    def test_status_code_is_none_for_non_http_errors(self):
        """Only APIStatusError should produce a non-None status_code."""
        from pythinker_code.soul.pythinkersoul import classify_api_error

        _, sc = classify_api_error(RuntimeError("other"))
        assert sc is None

    def test_classification_emits_correct_track_call(self):
        """The classified error_type is passed as a string property."""
        track("api_error", error_type="rate_limit")
        event = _collect_events()[-1]
        assert event["event"] == "api_error"
        assert event["properties"]["error_type"] == "rate_limit"
        assert isinstance(event["properties"]["error_type"], str)

    def test_api_error_with_status_code_field(self):
        """When status_code is available it is included in the event properties."""
        track("api_error", error_type="5xx_server", status_code=503)
        event = _collect_events()[-1]
        assert event["properties"]["status_code"] == 503
        assert isinstance(event["properties"]["status_code"], int)


# ---------------------------------------------------------------------------
# 4. Cancel / interrupt correctness
# ---------------------------------------------------------------------------


class TestCancelInterrupt:
    """Verify cancel and interrupt events."""

    def test_esc_emits_cancel(self):
        """Pressing Esc during streaming emits cancel."""
        track("cancel")
        events = _collect_events()
        assert events[-1]["event"] == "cancel"

    def test_esc_in_question_panel_emits_dismissed(self):
        """Pressing Esc on question panel emits question_dismissed, not cancel."""
        track("question_dismissed")
        events = _collect_events()
        assert events[-1]["event"] == "question_dismissed"
        assert not any(e["event"] == "cancel" for e in events)

    def test_run_cancelled_emits_turn_interrupted(self):
        """RunCancelled exception emits turn_interrupted with at_step."""
        track("turn_interrupted", at_step=3)
        event = _collect_events()[-1]
        assert event["event"] == "turn_interrupted"
        assert event["properties"]["at_step"] == 3

    def test_turn_interrupted_at_step_is_int(self):
        """at_step property must be an integer."""
        track("turn_interrupted", at_step=0)
        event = _collect_events()[-1]
        assert isinstance(event["properties"]["at_step"], int)

    def test_cancel_and_dismissed_are_distinct(self):
        """cancel and question_dismissed are different events."""
        track("cancel")
        track("question_dismissed")
        events = _collect_events()
        event_names = [e["event"] for e in events]
        assert "cancel" in event_names
        assert "question_dismissed" in event_names


# ---------------------------------------------------------------------------
# 5. Core infrastructure edge cases
# ---------------------------------------------------------------------------


class TestInfrastructureEdgeCases:
    """Tests for telemetry infrastructure behavior under edge conditions."""

    def test_disabled_track_is_noop(self):
        """After disable(), track() is a silent no-op."""
        disable()
        track("should_be_dropped")
        assert len(telemetry_mod._event_queue) == 0

    def test_disabled_with_sink_clears_buffer(self):
        """disable() clears both queue and sink buffer."""
        mock_sink = MagicMock(spec=EventSink)
        attach_sink(mock_sink)
        track("event_before")
        disable()
        mock_sink.clear_buffer.assert_called_once()

    def test_flush_sync_empty_buffer_is_noop(self):
        """flush_sync with empty buffer does not emit to OTel."""
        sink = EventSink(version="1.0.0")
        with patch("pythinker_code.telemetry.otel.emit_log") as mock_emit:
            sink.flush_sync()
        mock_emit.assert_not_called()

    def test_flush_sync_emits_to_otel(self):
        """flush_sync forwards every buffered event to OTel logs."""
        sink = EventSink(version="1.0.0")
        sink.accept({"event": "test", "timestamp": 1.0, "properties": {}})
        with patch("pythinker_code.telemetry.otel.emit_log") as mock_emit:
            sink.flush_sync()
        mock_emit.assert_called_once()
        kwargs = mock_emit.call_args.kwargs
        assert kwargs["name"] == "test"
        assert kwargs["timestamp_ns"] == 1_000_000_000

    def test_queue_overflow_preserves_newest(self):
        """When queue overflows, oldest events are dropped, newest kept."""
        for i in range(telemetry_mod._MAX_QUEUE_SIZE + 50):
            track(f"evt_{i}")
        events = _collect_events()
        assert len(events) == telemetry_mod._MAX_QUEUE_SIZE
        # Newest event should be last
        assert events[-1]["event"] == f"evt_{telemetry_mod._MAX_QUEUE_SIZE + 49}"
        # Oldest surviving event
        assert events[0]["event"] == "evt_50"


# ---------------------------------------------------------------------------
# 6. Specific event property correctness
# ---------------------------------------------------------------------------


def _mock_store_for(runtime: Any) -> MagicMock:
    """A mock BackgroundTaskStore whose ``update_runtime`` runs the callback and
    returns the (mutated) runtime, mirroring the real read-modify-write store so
    the manager's ``_transition_status`` path is actually exercised."""
    store = MagicMock()
    store.read_runtime.return_value = runtime

    def _update(task_id: str, update_fn: Any) -> Any:
        rt = store.read_runtime(task_id)
        update_fn(rt)
        return rt

    store.update_runtime.side_effect = _update
    return store


class TestEventPropertyCorrectness:
    """Verify specific events carry the right property types and values."""

    def test_yolo_toggle_enabled_bool(self):
        """yolo_toggle.enabled is a bool."""
        track("yolo_toggle", enabled=True)
        event = _collect_events()[-1]
        assert isinstance(event["properties"]["enabled"], bool)
        assert event["properties"]["enabled"] is True

        telemetry_mod._event_queue.clear()
        track("yolo_toggle", enabled=False)
        event = _collect_events()[-1]
        assert event["properties"]["enabled"] is False

    def test_shortcut_mode_switch_to_mode(self):
        """shortcut_mode_switch.to_mode is a string enum."""
        track("shortcut_mode_switch", to_mode="agent")
        event = _collect_events()[-1]
        assert event["properties"]["to_mode"] == "agent"
        assert isinstance(event["properties"]["to_mode"], str)

    def test_question_answered_method_enum(self):
        """question_answered.method is a string enum."""
        for method in ("number_key", "enter", "escape"):
            telemetry_mod._event_queue.clear()
            track("question_answered", method=method)
            event = _collect_events()[-1]
            assert event["properties"]["method"] == method

    def test_tool_error_has_tool_name_and_error_type(self):
        """tool_error includes tool_name and error_type (Python exception class name)."""
        track("tool_error", tool_name="Bash", error_type="RuntimeError")
        event = _collect_events()[-1]
        assert event["event"] == "tool_error"
        assert event["properties"]["tool_name"] == "Bash"
        assert event["properties"]["error_type"] == "RuntimeError"

    def test_tool_call_success_has_no_error_type(self):
        """tool_call success path: tool_name + success=True + duration_ms, no error_type."""
        track("tool_call", tool_name="ReadFile", success=True, duration_ms=123)
        event = _collect_events()[-1]
        assert event["event"] == "tool_call"
        assert event["properties"]["tool_name"] == "ReadFile"
        assert event["properties"]["success"] is True
        assert event["properties"]["duration_ms"] == 123
        assert isinstance(event["properties"]["duration_ms"], int)
        assert "error_type" not in event["properties"]

    def test_tool_call_failure_has_error_type(self):
        """tool_call failure path includes error_type from Python exception name."""
        track(
            "tool_call",
            tool_name="Bash",
            success=False,
            duration_ms=42,
            error_type="TimeoutError",
        )
        event = _collect_events()[-1]
        assert event["properties"]["success"] is False
        assert event["properties"]["error_type"] == "TimeoutError"

    def test_oauth_refresh_success_has_no_reason(self):
        """oauth_refresh success: only success=True, no reason field."""
        track("oauth_refresh", success=True)
        event = _collect_events()[-1]
        assert event["properties"]["success"] is True
        assert "reason" not in event["properties"]

    def test_oauth_refresh_unauthorized_has_reason(self):
        """OAuthUnauthorized path: success=False + reason=unauthorized."""
        track("oauth_refresh", success=False, reason="unauthorized")
        event = _collect_events()[-1]
        assert event["properties"]["success"] is False
        assert event["properties"]["reason"] == "unauthorized"

    def test_oauth_refresh_generic_failure_has_reason(self):
        """Generic Exception path: success=False + reason=network_or_other."""
        track("oauth_refresh", success=False, reason="network_or_other")
        event = _collect_events()[-1]
        assert event["properties"]["reason"] == "network_or_other"

    def test_mcp_connected_has_total_count(self):
        """mcp_connected has server_count and total_count."""
        track("mcp_connected", server_count=2, total_count=3)
        event = _collect_events()[-1]
        assert event["properties"]["server_count"] == 2
        assert event["properties"]["total_count"] == 3

    def test_mcp_failed_has_failed_count(self):
        """mcp_failed has failed_count and total_count."""
        track("mcp_failed", failed_count=1, total_count=3)
        event = _collect_events()[-1]
        assert event["properties"]["failed_count"] == 1
        assert event["properties"]["total_count"] == 3

    def test_session_load_failed_has_reason(self):
        """session_load_failed includes the Python exception class name as reason."""
        track("session_load_failed", reason="JSONDecodeError")
        event = _collect_events()[-1]
        assert event["event"] == "session_load_failed"
        assert event["properties"]["reason"] == "JSONDecodeError"
        assert isinstance(event["properties"]["reason"], str)

    def test_exit_event_has_duration(self):
        """exit includes duration_s (float)."""
        track("exit", duration_s=123.456)
        event = _collect_events()[-1]
        assert isinstance(event["properties"]["duration_s"], float)

    def test_startup_perf_has_four_phase_timings(self):
        """startup_perf has duration_ms + config_ms + init_ms + mcp_ms (all int)."""
        track(
            "startup_perf",
            duration_ms=342,
            config_ms=42,
            init_ms=100,
            mcp_ms=180,
        )
        event = _collect_events()[-1]
        for field in ("duration_ms", "config_ms", "init_ms", "mcp_ms"):
            assert field in event["properties"], f"missing {field}"
            assert isinstance(event["properties"][field], int)

    def test_model_switch_has_model_string(self):
        """model_switch.model is a string."""
        track("model_switch", model="pythinker-ai")
        event = _collect_events()[-1]
        assert event["properties"]["model"] == "pythinker-ai"

    def test_hook_triggered_properties(self):
        """hook_triggered has event_type and action."""
        track("hook_triggered", event_type="PreToolUse", action="block")
        event = _collect_events()[-1]
        assert event["properties"]["event_type"] == "PreToolUse"
        assert event["properties"]["action"] == "block"

    def test_started_event_has_yolo(self):
        """started includes resumed (bool) and yolo (bool)."""
        track("started", resumed=False, yolo=True)
        event = _collect_events()[-1]
        assert event["event"] == "started"
        assert event["properties"]["resumed"] is False
        assert event["properties"]["yolo"] is True

    def test_background_task_completed_success_no_reason(self):
        """Success path: no `reason` field."""
        track("background_task_completed", success=True, duration_s=45.2)
        event = _collect_events()[-1]
        assert event["properties"]["success"] is True
        assert isinstance(event["properties"]["duration_s"], float)
        assert "reason" not in event["properties"]

    def test_background_task_completed_failure_reason_error(self):
        """_mark_task_failed emits reason='error'."""
        track(
            "background_task_completed",
            success=False,
            duration_s=10.0,
            reason="error",
        )
        event = _collect_events()[-1]
        assert event["properties"]["reason"] == "error"

    def test_background_task_completed_failure_reason_timeout(self):
        """_mark_task_timed_out emits reason='timeout'."""
        track(
            "background_task_completed",
            success=False,
            duration_s=300.0,
            reason="timeout",
        )
        event = _collect_events()[-1]
        assert event["properties"]["reason"] == "timeout"

    def test_background_task_completed_failure_reason_killed(self):
        """_mark_task_killed emits reason='killed'."""
        track(
            "background_task_completed",
            success=False,
            duration_s=5.0,
            reason="killed",
        )
        event = _collect_events()[-1]
        assert event["properties"]["reason"] == "killed"

    def test_background_task_no_event_without_start_time(self):
        """_mark_task_completed must NOT emit track when started_at is None."""
        from pythinker_code.background.manager import BackgroundTaskManager
        from pythinker_code.background.models import TaskRuntime

        runtime = TaskRuntime(status="running", started_at=None)
        mock_store = _mock_store_for(runtime)

        manager = object.__new__(BackgroundTaskManager)
        manager._store = mock_store

        with patch("pythinker_code.telemetry.track") as mock_track:
            manager._mark_task_completed("task-no-start")

        mock_track.assert_not_called()

    def test_mark_task_killed_emits_completed_event(self):
        """_mark_task_killed must emit background_task_completed(success=False)."""
        from pythinker_code.background.manager import BackgroundTaskManager
        from pythinker_code.background.models import TaskRuntime

        runtime = TaskRuntime(status="running", started_at=1000.0)

        mock_store = _mock_store_for(runtime)

        manager = object.__new__(BackgroundTaskManager)
        manager._store = mock_store

        with patch("pythinker_code.telemetry.track") as mock_track:
            manager._mark_task_killed("task-123", "Killed by user")

        mock_track.assert_called_once()
        call_args = mock_track.call_args
        assert call_args[0][0] == "background_task_completed"
        assert call_args[1]["success"] is False
        assert "duration_s" in call_args[1]

    def test_mark_task_killed_no_event_without_start_time(self):
        """_mark_task_killed must NOT emit track when started_at is None."""
        from pythinker_code.background.manager import BackgroundTaskManager
        from pythinker_code.background.models import TaskRuntime

        runtime = TaskRuntime(status="running", started_at=None)
        mock_store = _mock_store_for(runtime)

        manager = object.__new__(BackgroundTaskManager)
        manager._store = mock_store

        with patch("pythinker_code.telemetry.track") as mock_track:
            manager._mark_task_killed("task-no-start", "Killed by user")

        mock_track.assert_not_called()

    def test_timestamp_is_recent(self):
        """All events get a timestamp close to now."""
        before = time.time()
        track("test")
        after = time.time()
        event = _collect_events()[-1]
        assert before <= event["timestamp"] <= after


# ---------------------------------------------------------------------------
# 7. Context enrichment
# ---------------------------------------------------------------------------


def _flush_and_capture_attrs(sink: EventSink) -> dict[str, Any]:
    """Run flush_sync with otel.emit_log patched, return the first event's attrs."""
    captured: list[dict[str, Any]] = []

    def _capture(
        *,
        name: str,
        attributes: dict[str, Any],
        severity: str = "info",
        timestamp_ns: int | None = None,
    ):
        captured.append(attributes)

    with patch("pythinker_code.telemetry.otel.emit_log", side_effect=_capture):
        sink.flush_sync()
    assert captured, "expected at least one emitted event"
    return captured[0]


class TestContextEnrichment:
    """Verify EventSink enriches events correctly before forwarding to OTel."""

    def test_enrichment_adds_version_platform(self):
        sink = EventSink(version="2.0.0", model="test-model")
        sink.accept({"event": "test", "timestamp": 1.0, "properties": {}})
        attrs = _flush_and_capture_attrs(sink)
        assert attrs["context.version"] == "2.0.0"
        assert attrs["context.model"] == "test-model"
        assert "context.platform" in attrs
        assert "context.arch" in attrs

    def test_enrichment_does_not_mutate_input(self):
        """accept() must not mutate the caller's dict."""
        sink = EventSink(version="1.0.0")
        original = {"event": "test", "timestamp": 1.0, "properties": {}}
        sink.accept(original)
        assert "context" not in original

    def test_model_set_at_init(self):
        sink = EventSink(version="1.0.0", model="test-model")
        sink.accept({"event": "test", "timestamp": 1.0, "properties": {}})
        attrs = _flush_and_capture_attrs(sink)
        assert attrs["context.model"] == "test-model"

    def test_device_and_session_ids_propagate(self):
        """device_id and session_id set via set_context() appear in events."""
        set_context(device_id="dev-abc", session_id="sess-xyz")
        track("test_event")
        event = _collect_events()[-1]
        assert event["device_id"] == "dev-abc"
        assert event["session_id"] == "sess-xyz"

    def test_enrichment_adds_runtime_python(self):
        sink = EventSink(version="1.0.0")
        sink.accept({"event": "test", "timestamp": 1.0, "properties": {}})
        attrs = _flush_and_capture_attrs(sink)
        assert attrs["context.runtime"] == "python"


# ---------------------------------------------------------------------------
# 7b. Client info (wire/acp)
# ---------------------------------------------------------------------------


class TestSessionStarted:
    """Verify session_started attribution and client info handling."""

    def test_context_never_contains_client_info(self):
        """Client attribution belongs on session_started properties, not context."""
        from pythinker_code.telemetry import set_client_info

        set_client_info(name="vscode", version="1.90.0")
        sink = EventSink(version="1.0.0")
        sink.accept({"event": "t", "timestamp": 1.0, "properties": {}})
        attrs = _flush_and_capture_attrs(sink)
        # context.* keys are flattened; ensure none describe wire/acp client.
        assert "context.client_name" not in attrs
        assert "context.client_version" not in attrs

    def test_set_client_info_empty_name_is_ignored(self):
        """Empty string name must not overwrite any previously set info."""
        from pythinker_code.telemetry import set_client_info

        set_client_info(name="cursor", version="0.40.0")
        set_client_info(name="", version="anything")
        assert telemetry_mod._client_info == ("cursor", "0.40.0")

    def test_set_client_info_overwrites_previous(self):
        """Non-empty set_client_info replaces the tuple atomically."""
        from pythinker_code.telemetry import set_client_info

        set_client_info(name="vscode", version="1.90.0")
        set_client_info(name="zed", version="0.180.0")
        assert telemetry_mod._client_info == ("zed", "0.180.0")

    def test_client_info_stored_as_tuple(self):
        """_client_info is stored as a tuple so readers never see a half-update."""
        from pythinker_code.telemetry import set_client_info

        set_client_info(name="pythinker-web", version="2.0.0")
        assert telemetry_mod._client_info == ("pythinker-web", "2.0.0")

    def test_track_session_started_shell(self):
        from pythinker_code.telemetry import track_session_started_once

        set_context(device_id="dev", session_id="sess-shell")
        track_session_started_once(ui_mode="shell", resumed=False)

        event = _collect_events()[-1]
        assert event["event"] == "session_started"
        assert event["properties"]["client_name"] == "shell"
        assert event["properties"]["client_version"] is None
        assert event["properties"]["ui_mode"] == "shell"
        assert event["properties"]["resumed"] is False

    def test_track_session_started_wire_uses_current_client_info(self):
        from pythinker_code.telemetry import set_client_info, track_session_started_once

        set_context(device_id="dev", session_id="sess-wire")
        set_client_info(name="kiwi", version="1.2.3")
        track_session_started_once(ui_mode="wire", resumed=True)

        event = _collect_events()[-1]
        assert event["event"] == "session_started"
        assert event["properties"]["client_name"] == "kiwi"
        assert event["properties"]["client_version"] == "1.2.3"
        assert event["properties"]["ui_mode"] == "wire"
        assert event["properties"]["resumed"] is True

    def test_track_session_started_once_per_session(self):
        from pythinker_code.telemetry import track_session_started_once

        set_context(device_id="dev", session_id="sess-once")
        track_session_started_once(ui_mode="wire", resumed=False, client_name="kiwi")
        track_session_started_once(ui_mode="wire", resumed=False, client_name="vscode")

        events = [event for event in _collect_events() if event["event"] == "session_started"]
        assert len(events) == 1
        assert events[0]["properties"]["client_name"] == "kiwi"

    def test_track_session_started_explicit_client_info_wins(self):
        from pythinker_code.telemetry import set_client_info, track_session_started_once

        set_context(device_id="dev", session_id="sess-explicit")
        set_client_info(name="kiwi", version="1.2.3")
        track_session_started_once(
            ui_mode="wire",
            resumed=False,
            client_name="pythinker-code-for-vs-code",
            client_version="1.90.0",
        )

        event = _collect_events()[-1]
        assert event["properties"]["client_name"] == "pythinker-code-for-vs-code"
        assert event["properties"]["client_version"] == "1.90.0"


# ---------------------------------------------------------------------------
# 7c. Compaction tracking (exercises real compact_context branches)
# ---------------------------------------------------------------------------


class TestCompactionTracking:
    """compaction_triggered must fire on both success and failure paths."""

    def _make_soul(self, *, before_tokens: int, estimated_after: int) -> Any:
        """Construct a minimal PythinkerSoul stub bypassing __init__."""
        from pythinker_code.soul.pythinkersoul import PythinkerSoul
        from pythinker_code.soul.request_lifecycle import RequestLifecycle

        soul = object.__new__(PythinkerSoul)

        runtime = MagicMock()
        runtime.llm = MagicMock()  # non-None so LLMNotSet is not raised
        runtime.session.id = "test-session"
        runtime.role = "non-root"  # skip active-task-snapshot branch
        runtime.background_tasks = MagicMock()
        soul._runtime = runtime

        ctx = MagicMock()
        ctx.token_count = before_tokens
        ctx.history = []
        ctx.replace_history = AsyncMock()
        soul._context = ctx

        soul._hook_engine = MagicMock()
        soul._hook_engine.trigger = AsyncMock()

        soul._compaction = MagicMock()

        soul._agent = MagicMock()
        soul._agent.system_prompt = "sys"

        loop_control = MagicMock()
        loop_control.max_retries_per_step = 1
        soul._loop_control = loop_control

        soul._checkpoint = AsyncMock()
        soul._checkpoint_with_user_message = False

        # _run_with_connection_recovery returns a value with .messages and
        # .estimated_token_count — shape it with MagicMock to avoid depending
        # on the internal NamedTuple layout.
        fake_result = MagicMock()
        # Non-empty so the post-compaction guard does not raise; the exact
        # contents do not affect telemetry assertions below.
        fake_result.messages = [MagicMock()]
        fake_result.estimated_token_count = estimated_after
        # No usage on this mock result, so the cumulative-usage accumulation
        # (subagent-2) is skipped — this harness bypasses __init__.
        fake_result.usage = None
        soul._run_with_connection_recovery = AsyncMock(return_value=fake_result)

        soul._injection_providers = []
        soul._request_lifecycle = RequestLifecycle([])
        soul._notified_context_generations = set()
        return soul

    @pytest.mark.asyncio
    async def test_auto_compaction_success_emits_event(self):
        """Auto-triggered success: track has trigger_type=auto + after_tokens + success=True."""
        soul = self._make_soul(before_tokens=12000, estimated_after=3000)

        with (
            patch("pythinker_code.soul.pythinkersoul.wire_send"),
            patch("pythinker_code.telemetry.track") as mock_track,
        ):
            await soul.compact_context()

        # Filter to the compaction event — other events (hook triggers etc.)
        # shouldn't go through telemetry.track.
        calls = [c for c in mock_track.call_args_list if c[0][0] == "compaction_triggered"]
        assert len(calls) == 1
        args, kwargs = calls[0]
        assert args[0] == "compaction_triggered"
        assert kwargs["trigger_type"] == "auto"
        assert kwargs["before_tokens"] == 12000
        assert kwargs["after_tokens"] == 3000
        assert kwargs["success"] is True

    @pytest.mark.asyncio
    async def test_manual_compaction_success_emits_event(self):
        """/compact with instruction yields trigger_type=manual."""
        soul = self._make_soul(before_tokens=8000, estimated_after=2000)

        with (
            patch("pythinker_code.soul.pythinkersoul.wire_send"),
            patch("pythinker_code.telemetry.track") as mock_track,
        ):
            await soul.compact_context(custom_instruction="focus on auth")

        calls = [c for c in mock_track.call_args_list if c[0][0] == "compaction_triggered"]
        assert len(calls) == 1
        assert calls[0][1]["trigger_type"] == "manual"
        assert calls[0][1]["success"] is True

    @pytest.mark.asyncio
    async def test_compaction_failure_emits_event_then_reraises(self):
        """On compaction failure: track success=False (no after_tokens), then re-raise."""
        soul = self._make_soul(before_tokens=50000, estimated_after=0)
        # Force the compaction to fail with a non-retryable error
        soul._run_with_connection_recovery = AsyncMock(side_effect=RuntimeError("compaction boom"))

        with (
            patch("pythinker_code.soul.pythinkersoul.wire_send"),
            patch("pythinker_code.telemetry.track") as mock_track,
            pytest.raises(RuntimeError, match="compaction boom"),
        ):
            await soul.compact_context()

        calls = [c for c in mock_track.call_args_list if c[0][0] == "compaction_triggered"]
        assert len(calls) == 1
        kwargs = calls[0][1]
        assert kwargs["trigger_type"] == "auto"
        assert kwargs["before_tokens"] == 50000
        assert kwargs["success"] is False
        assert "after_tokens" not in kwargs


# ---------------------------------------------------------------------------
# Model-family classification (gen_ai.model.family)
# ---------------------------------------------------------------------------

from pythinker_code.telemetry.metrics import classify_model_family  # noqa: E402


@pytest.mark.parametrize(
    ("model", "family"),
    [
        ("claude-sonnet-4-6", "claude"),
        ("gpt-5.2-codex", "codex"),
        ("gpt-5.2", "gpt"),
        ("gemini-3-pro", "gemini"),
        ("qwen3.7-max", "qwen"),
        ("Qwen/QwQ-32B", "qwen"),
        ("deepseek-v3.3", "deepseek"),
        ("kimi-k2-thinking", "kimi"),
        ("moonshot-v1-128k", "kimi"),
        ("MiniMax-M2.1", "minimax"),
        ("glm-5", "glm"),
        ("llama-4-maverick", "llama"),
        ("mistral-large-3", "mistral"),
        ("grok-4.1", "grok"),
        ("totally-unknown-model", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_classify_model_family(model, family):
    assert classify_model_family(model) == family
