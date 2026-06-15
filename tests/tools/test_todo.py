"""Tests for SetTodoList tool."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pythinker_code.scratchpad import session_scratch_path
from pythinker_code.soul.agent import Runtime
from pythinker_code.tools.todo import Params, SetTodoList, Todo
from pythinker_code.wire.types import TodoListUpdated


class _RecordingWire:
    def __init__(self, captured: list[object]) -> None:
        self.soul_side = self
        self._captured = captured

    def send(self, msg: object) -> None:
        self._captured.append(msg)


@pytest.fixture
def set_todo_list_tool(runtime: Runtime) -> SetTodoList:
    """Create a SetTodoList tool instance with runtime."""
    return SetTodoList(runtime)


class TestParamsJsonStringCoercion:
    """Regression: LLM occasionally passes todos as a JSON-encoded string instead of a list."""

    def test_todos_as_json_string_is_parsed(self):
        """Params must accept todos as a JSON string and coerce it to a list."""
        import json

        raw = json.dumps([{"title": "Explore agent", "status": "pending"}])
        params = Params(todos=raw)  # type: ignore[arg-type]
        assert params.todos is not None
        assert len(params.todos) == 1
        assert params.todos[0].title == "Explore agent"
        assert params.todos[0].status == "pending"

    def test_todos_as_normal_list_still_works(self):
        params = Params(todos=[Todo(title="Normal task", status="done")])
        assert params.todos is not None
        assert params.todos[0].title == "Normal task"

    def test_completed_status_alias_normalizes_to_done(self):
        params = Params(todos=[{"title": "Write report", "status": "completed"}])  # type: ignore[list-item]
        assert params.todos is not None
        assert params.todos[0].status == "done"

    def test_todos_none_still_works(self):
        params = Params(todos=None)
        assert params.todos is None

    def test_invalid_json_string_fails_validation(self):
        """Invalid JSON strings must fail validation (fail-closed)."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Params(todos="not valid json")  # type: ignore[arg-type]


class TestSetTodoListOutputNotEmpty:
    """Regression test for issue #1710: SetTodoList storm.

    The root cause is that SetTodoList returned output="" which meant the model
    only saw '<system>Todo list updated</system>' — no confirmation of what it
    saved. This led to repeated calls (a "storm") especially when Shell was disabled.
    """

    async def test_write_mode_returns_nonempty_output(
        self, set_todo_list_tool: SetTodoList, runtime: Runtime
    ):
        """When todos are provided, the tool must return a non-empty output
        so the model gets meaningful feedback (not just 'Todo list updated')."""
        params = Params(
            todos=[
                Todo(title="Analyze code", status="pending"),
                Todo(title="Write tests", status="in_progress"),
                Todo(title="Read requirements", status="done"),
            ]
        )
        result = await set_todo_list_tool(params)
        assert not result.is_error
        # The critical assertion: output must NOT be empty
        assert result.output != "", (
            "SetTodoList output must not be empty — this is the root cause of issue #1710. "
            "The model needs to see confirmation of the todo state it just set."
        )
        assert result.message == "Todo list updated"
        scratch_file = session_scratch_path(runtime.session.work_dir, session_id=runtime.session.id)
        assert scratch_file.is_file()
        scratch_text = scratch_file.read_text(encoding="utf-8")
        assert "— todo update" in scratch_text
        assert "items: 3; done: 1; in_progress: 1; pending: 1" in scratch_text
        assert "active: Write tests" in scratch_text

    async def test_write_mode_persists_completed_alias_as_done(
        self, set_todo_list_tool: SetTodoList, runtime: Runtime
    ):
        from pythinker_code.session_state import load_session_state
        from pythinker_code.tools.display import TodoDisplayBlock

        params = Params(todos=[{"title": "Write report", "status": "completed"}])  # type: ignore[list-item]
        result = await set_todo_list_tool(params)

        assert not result.is_error
        assert isinstance(result.display[0], TodoDisplayBlock)
        assert result.display[0].items[0].status == "done"
        state = load_session_state(runtime.session.dir)
        assert state.todos[0].status == "done"

    async def test_read_mode_returns_current_todos(self, set_todo_list_tool: SetTodoList):
        """When no todos are provided (None), the tool should return the current
        todo list from persistent storage, including status."""
        # First write some todos
        write_params = Params(
            todos=[
                Todo(title="Task A", status="pending"),
                Todo(title="Task B", status="done"),
            ]
        )
        await set_todo_list_tool(write_params)

        # Then read without providing todos
        read_params = Params(todos=None)
        result = await set_todo_list_tool(read_params)
        assert not result.is_error
        assert "Task A" in result.output
        assert "Task B" in result.output
        assert "pending" in result.output
        assert "done" in result.output

    async def test_read_mode_empty_list(self, set_todo_list_tool: SetTodoList):
        """Reading with no prior todos should return a clear empty message."""
        read_params = Params(todos=None)
        result = await set_todo_list_tool(read_params)
        assert not result.is_error
        assert result.output  # non-empty even when no todos

    async def test_write_mode_emits_todo_list_updated_wire_event(
        self, set_todo_list_tool: SetTodoList, monkeypatch: pytest.MonkeyPatch
    ):
        captured: list[object] = []
        monkeypatch.setattr(
            "pythinker_code.soul.get_wire_or_none", lambda: _RecordingWire(captured)
        )

        result = await set_todo_list_tool(
            Params(todos=[Todo(title="Investigate", status="in_progress")])
        )

        assert not result.is_error
        assert captured == [
            TodoListUpdated(
                items=(("Investigate", "in_progress"),),
                complete=False,
                source="tool",
            )
        ]

    async def test_read_mode_emits_todo_list_updated_wire_event(
        self, set_todo_list_tool: SetTodoList, monkeypatch: pytest.MonkeyPatch
    ):
        await set_todo_list_tool(Params(todos=[Todo(title="Task A", status="done")]))
        captured: list[object] = []
        monkeypatch.setattr(
            "pythinker_code.soul.get_wire_or_none", lambda: _RecordingWire(captured)
        )

        result = await set_todo_list_tool(Params(todos=None))

        assert not result.is_error
        assert captured == [
            TodoListUpdated(items=(("Task A", "done"),), complete=True, source="tool")
        ]

    async def test_cancelled_status_is_accepted_and_persisted(
        self, set_todo_list_tool: SetTodoList, runtime: Runtime
    ):
        """A `cancelled` todo is a valid status: it must round-trip through the
        tool and stay visible in the list (not silently dropped), so the plan
        history the user watches stays honest when scope changes."""
        write_params = Params(
            todos=[
                Todo(title="Migrate auth", status="done"),
                Todo(title="Update legacy shim", status="cancelled"),
                Todo(title="Write tests", status="in_progress"),
            ]
        )
        result = await set_todo_list_tool(write_params)
        assert not result.is_error

        # The cancelled item is counted in the scratch recap (not omitted).
        scratch_file = session_scratch_path(runtime.session.work_dir, session_id=runtime.session.id)
        scratch_text = scratch_file.read_text(encoding="utf-8")
        assert "cancelled: 1" in scratch_text

        # And it survives a read-back rather than being dropped.
        read_result = await set_todo_list_tool(Params(todos=None))
        assert "Update legacy shim" in read_result.output
        assert "cancelled" in read_result.output

    def test_cancelled_status_renders_distinctly(self):
        """The shell renderer must have a distinct icon/style for cancelled todos."""
        from pythinker_code.ui.shell.tool_renderers.todo import _ICONS, _status_title

        assert "cancelled" in _ICONS
        assert _ICONS["cancelled"] != _ICONS["done"]
        styled = _status_title("cancelled", "obsolete task")
        assert "obsolete task" in styled.plain

    async def test_write_empty_list_clears_todos(self, set_todo_list_tool: SetTodoList):
        """Passing an empty list [] should clear all todos."""
        # Write some todos first
        write_params = Params(todos=[Todo(title="Task A", status="pending")])
        await set_todo_list_tool(write_params)

        # Clear with empty list
        clear_params = Params(todos=[])
        result = await set_todo_list_tool(clear_params)
        assert not result.is_error
        assert result.output == "Todo list updated"

        # Verify cleared
        read_params = Params(todos=None)
        result = await set_todo_list_tool(read_params)
        assert isinstance(result.output, str)
        assert "empty" in result.output.lower() or result.output.strip() == "Todo list is empty."

    async def test_root_todos_persisted_to_disk(
        self, set_todo_list_tool: SetTodoList, runtime: Runtime
    ):
        """Write mode should persist todos to disk via SessionState."""
        from pythinker_code.session_state import load_session_state

        params = Params(
            todos=[
                Todo(title="Disk task", status="in_progress"),
                Todo(title="Another task", status="done"),
            ]
        )
        await set_todo_list_tool(params)

        # Verify by loading directly from disk, bypassing in-memory state
        disk_state = load_session_state(runtime.session.dir)
        assert len(disk_state.todos) == 2
        assert disk_state.todos[0].title == "Disk task"
        assert disk_state.todos[0].status == "in_progress"
        assert disk_state.todos[1].title == "Another task"
        assert disk_state.todos[1].status == "done"

    async def test_write_mode_display_block(self, set_todo_list_tool: SetTodoList):
        """Write mode should still produce TodoDisplayBlock for UI rendering."""
        from pythinker_code.tools.display import TodoDisplayBlock

        params = Params(todos=[Todo(title="UI task", status="pending")])
        result = await set_todo_list_tool(params)
        assert len(result.display) == 1
        assert isinstance(result.display[0], TodoDisplayBlock)
        assert result.display[0].items[0].title == "UI task"

    async def test_read_mode_no_display_block(self, set_todo_list_tool: SetTodoList):
        """Read mode should not produce display blocks (no UI side-effect)."""
        read_params = Params(todos=None)
        result = await set_todo_list_tool(read_params)
        assert result.display == []


class TestSetTodoListSubagent:
    """Test SetTodoList behavior in subagent context."""

    async def test_subagent_uses_independent_storage(self, runtime: Runtime):
        """Subagent todos should be stored independently from root agent."""
        # Create root tool and set a todo
        root_tool = SetTodoList(runtime)
        await root_tool(Params(todos=[Todo(title="Root task", status="pending")]))

        # Create a subagent runtime
        subagent_runtime = runtime.copy_for_subagent(
            agent_id="test-sub-1",
            subagent_type="coder",
        )
        # Initialize the subagent instance directory
        assert subagent_runtime.subagent_store is not None
        subagent_runtime.subagent_store.instance_dir("test-sub-1", create=True)

        sub_tool = SetTodoList(subagent_runtime)

        # Subagent should start with empty todos
        result = await sub_tool(Params(todos=None))
        assert isinstance(result.output, str)
        assert "empty" in result.output.lower() or "Root task" not in result.output

        # Subagent writes its own todo
        await sub_tool(Params(todos=[Todo(title="Sub task", status="in_progress")]))
        result = await sub_tool(Params(todos=None))
        assert "Sub task" in result.output

        # Root agent should still have its own todo
        result = await root_tool(Params(todos=None))
        assert "Root task" in result.output
        assert "Sub task" not in result.output

    async def test_subagent_no_store_or_id_graceful(self, runtime: Runtime):
        """When subagent_store or subagent_id is None, save is a no-op and load returns empty."""
        subagent_runtime = runtime.copy_for_subagent(
            agent_id="test-sub-2",
            subagent_type="coder",
        )
        # Force store/id to None to simulate edge case
        subagent_runtime.subagent_store = None
        subagent_runtime.subagent_id = None

        tool = SetTodoList(subagent_runtime)

        # Write should silently succeed (no-op)
        result = await tool(Params(todos=[Todo(title="Ghost task", status="pending")]))
        assert not result.is_error
        assert result.output == "Todo list updated"

        # Read should return empty
        result = await tool(Params(todos=None))
        assert not result.is_error
        assert isinstance(result.output, str)
        assert "empty" in result.output.lower()

    async def test_corrupted_subagent_state_file(self, runtime: Runtime):
        """Corrupted subagent state.json should be handled gracefully."""
        subagent_runtime = runtime.copy_for_subagent(
            agent_id="test-sub-3",
            subagent_type="coder",
        )
        assert subagent_runtime.subagent_store is not None
        instance_dir = subagent_runtime.subagent_store.instance_dir("test-sub-3", create=True)

        # Write corrupted JSON to state.json
        state_file = instance_dir / "state.json"
        state_file.write_text("not valid json {{{", encoding="utf-8")

        tool = SetTodoList(subagent_runtime)

        # Read should return empty (corrupted file treated as empty)
        result = await tool(Params(todos=None))
        assert not result.is_error
        assert isinstance(result.output, str)
        assert "empty" in result.output.lower()

        # Write should overwrite the corrupted file successfully
        result = await tool(Params(todos=[Todo(title="Recovery task", status="pending")]))
        assert not result.is_error

        # Verify recovery
        result = await tool(Params(todos=None))
        assert "Recovery task" in result.output

    async def test_subagent_malformed_individual_item(self, runtime: Runtime):
        """Malformed individual items in state.json should be skipped, valid ones preserved."""
        import json

        subagent_runtime = runtime.copy_for_subagent(
            agent_id="test-sub-4",
            subagent_type="coder",
        )
        assert subagent_runtime.subagent_store is not None
        instance_dir = subagent_runtime.subagent_store.instance_dir("test-sub-4", create=True)

        # Write JSON with one valid and one invalid todo item
        state_file = instance_dir / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "todos": [
                        {"title": "Valid task", "status": "pending"},
                        {"bad": "item"},  # missing title and status
                        {"title": "Also valid", "status": "done"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        tool = SetTodoList(subagent_runtime)
        result = await tool(Params(todos=None))
        assert not result.is_error
        assert "Valid task" in result.output
        assert "Also valid" in result.output
        # The malformed item should be silently skipped
        assert "bad" not in result.output


class TestSingleInProgressInvariant:
    """At most one step in_progress at a time — softened to a notice because
    pythinker's parallel-subagent fan-out legitimately tracks one in_progress
    sub-todo per running child (system.md orchestration rules)."""

    async def test_multiple_in_progress_accepted_with_notice(
        self, set_todo_list_tool: SetTodoList, runtime: Runtime
    ):
        result = await set_todo_list_tool(
            Params(
                todos=[
                    Todo(title="Task A", status="in_progress"),
                    Todo(title="Task B", status="in_progress"),
                ]
            )
        )
        assert not result.is_error
        assert "at most one" in result.output
        # The list is persisted despite the notice (parallel fan-out is legal).
        assert len(runtime.session.state.todos) == 2

    async def test_exactly_one_in_progress_accepted(self, set_todo_list_tool: SetTodoList):
        result = await set_todo_list_tool(
            Params(
                todos=[
                    Todo(title="Task A", status="done"),
                    Todo(title="Task B", status="in_progress"),
                    Todo(title="Task C", status="pending"),
                ]
            )
        )
        assert not result.is_error

    async def test_zero_in_progress_accepted(self, set_todo_list_tool: SetTodoList):
        result = await set_todo_list_tool(
            Params(
                todos=[
                    Todo(title="Task A", status="done"),
                    Todo(title="Task B", status="pending"),
                ]
            )
        )
        assert not result.is_error

    async def test_read_mode_unaffected(self, set_todo_list_tool: SetTodoList):
        result = await set_todo_list_tool(Params(todos=None))
        assert not result.is_error

    async def test_subagent_list_normalized_to_single_in_progress(self, runtime: Runtime):
        """A subagent is a single sequential worker: extra in_progress items are
        demoted to pending (first one wins, order preserved) and the
        normalization is reported in the tool output."""
        subagent_runtime = runtime.copy_for_subagent(
            agent_id="test-sub-norm",
            subagent_type="coder",
        )
        assert subagent_runtime.subagent_store is not None
        subagent_runtime.subagent_store.instance_dir("test-sub-norm", create=True)
        sub_tool = SetTodoList(subagent_runtime)

        result = await sub_tool(
            Params(
                todos=[
                    Todo(title="Task A", status="in_progress"),
                    Todo(title="Task B", status="in_progress"),
                    Todo(title="Task C", status="pending"),
                ]
            )
        )

        assert not result.is_error
        assert "normalized 1 extra in_progress" in result.output

        read_back = await sub_tool(Params(todos=None))
        assert "[in_progress] Task A" in read_back.output
        assert "[pending] Task B" in read_back.output
        assert "[pending] Task C" in read_back.output

    async def test_subagent_single_in_progress_not_normalized(self, runtime: Runtime):
        """Positive control: a well-formed subagent list passes through untouched."""
        subagent_runtime = runtime.copy_for_subagent(
            agent_id="test-sub-ok",
            subagent_type="coder",
        )
        assert subagent_runtime.subagent_store is not None
        subagent_runtime.subagent_store.instance_dir("test-sub-ok", create=True)
        sub_tool = SetTodoList(subagent_runtime)

        result = await sub_tool(
            Params(
                todos=[
                    Todo(title="Task A", status="done"),
                    Todo(title="Task B", status="in_progress"),
                ]
            )
        )

        assert not result.is_error
        assert "normalized" not in result.output


async def test_write_mode_emits_todo_list_updated_wire_event(
    set_todo_list_tool: SetTodoList,
) -> None:
    captured: list[object] = []
    with patch("pythinker_code.soul.get_wire_or_none", return_value=_RecordingWire(captured)):
        result = await set_todo_list_tool(
            Params(todos=[Todo(title="Investigate", status="in_progress")])
        )
    assert not result.is_error
    updated = [e for e in captured if isinstance(e, TodoListUpdated)]
    assert len(updated) == 1
    assert updated[0].items == (("Investigate", "in_progress"),)
    assert updated[0].source == "tool"
