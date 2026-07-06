import json
from typing import Any, cast

from pythinker_core.message import ToolCall
from pythinker_core.tooling import ToolResult, ToolReturnValue

from pythinker_code.tools.display import DiffDisplayBlock
from pythinker_code.ui.shell.focus_model import FocusTuiModel
from pythinker_code.ui.shell.visualize import _PromptLiveView
from pythinker_code.wire.types import StatusUpdate


def test_focus_model_hides_files_by_default_and_counts_them() -> None:
    model = FocusTuiModel()
    model.begin_turn("fix tests")
    model.mark_file("src/a.py", "updated")
    model.mark_file("tests/test_a.py", "updated")

    rows = model.render_rows(width=80)

    assert model.visible_file_count() == 2
    assert any("files: 2" in row for row in rows)
    assert not any("src/a.py" in row for row in rows)
    assert not any("tests/test_a.py" in row for row in rows)


def test_focus_model_toggles_compact_file_shelf() -> None:
    model = FocusTuiModel()
    for index in range(7):
        model.mark_file(f"src/file_{index}.py", "updated")

    model.toggle_files()
    rows = model.render_rows(width=80)

    assert any("Files" in row for row in rows)
    assert any("src/file_6.py" in row for row in rows)
    assert any("+2 more" in row for row in rows)
    assert not any("src/file_0.py" in row for row in rows)


def test_focus_model_collapses_read_rows_by_default() -> None:
    model = FocusTuiModel()
    model.append_tool_row("read-1", "Read(test_paths.py)", "31 lines", expandable=True)

    rows = model.render_rows(width=80)

    assert rows == ["⏺ Read(test_paths.py)  31 lines  ctrl+o"]


class _PromptSession:
    def update_pinned_todos(self, _items: object) -> None:
        pass


def test_live_view_updates_focus_model_for_file_write() -> None:
    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    view.enable_focus_model()

    view.append_tool_call(
        ToolCall(
            id="write-1",
            function=ToolCall.FunctionBody(
                name="WriteFile",
                arguments=json.dumps({"path": "src/a.py", "content": "x"}),
            ),
        )
    )
    assert view.focus_model is not None
    assert any("files: 1" in row for row in view.focus_model.render_rows(80))

    view.append_tool_result(
        ToolResult(
            tool_call_id="write-1",
            return_value=ToolReturnValue(
                is_error=False,
                output="ok",
                message="ok",
                display=[DiffDisplayBlock(path="src/a.py", old_text="", new_text="x")],
            ),
        )
    )
    view.focus_model.toggle_files()
    rows = view.focus_model.render_rows(80)
    assert any("updated" in row and "src/a.py" in row for row in rows)


def test_live_view_marks_failed_write_without_display_path() -> None:
    view = _PromptLiveView(
        StatusUpdate(),
        prompt_session=cast(Any, _PromptSession()),
        steer=lambda _content: None,
    )
    view.enable_focus_model()

    view.append_tool_call(
        ToolCall(
            id="write-1",
            function=ToolCall.FunctionBody(
                name="WriteFile",
                arguments=json.dumps({"path": "src/a.py", "content": "x"}),
            ),
        )
    )
    assert view.focus_model is not None

    view.append_tool_result(
        ToolResult(
            tool_call_id="write-1",
            return_value=ToolReturnValue(
                is_error=True,
                output="write failed",
                message="write failed",
                display=[],
            ),
        )
    )
    view.focus_model.toggle_files()

    rows = view.focus_model.render_rows(80)
    assert any("failed" in row and "src/a.py" in row for row in rows)
