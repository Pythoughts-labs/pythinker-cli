from __future__ import annotations

import json

import pytest
from pythinker_core.message import ToolCall
from pythinker_core.tooling import ToolError, ToolOk
from rich.console import Console

from pythinker_code.ui.shell.tool_renderers import (
    clear_tool_renderers,
    register_builtin_renderers,
)
from pythinker_code.ui.shell.visualize import _ToolCallBlock, _worklog
from pythinker_code.wire.types import ToolResult


@pytest.fixture(autouse=True)
def _legacy_tui_style(monkeypatch):
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "pythinker")


def _plain(renderable) -> str:
    console = Console(record=True, width=120, color_system=None)
    console.print(renderable)
    return console.export_text()


def _tool_call(name: str, arguments: str = "{}") -> ToolCall:
    return ToolCall(id=f"tc-{name}", function=ToolCall.FunctionBody(name=name, arguments=arguments))


def _tool_call_with_id(call_id: str, name: str, arguments: str = "{}") -> ToolCall:
    return ToolCall(id=call_id, function=ToolCall.FunctionBody(name=name, arguments=arguments))


class TestExtractFullUrl:
    """Tests for _ToolCallBlock._extract_full_url static method."""

    def test_fetchurl_normal_url(self):
        url = _ToolCallBlock._extract_full_url(
            '{"url": "https://example.com/very/long/path"}', "FetchURL"
        )
        assert url == "https://example.com/very/long/path"

    def test_fetchurl_short_url(self):
        url = _ToolCallBlock._extract_full_url('{"url": "https://x.co"}', "FetchURL")
        assert url == "https://x.co"

    def test_non_fetchurl_tool(self):
        url = _ToolCallBlock._extract_full_url('{"url": "https://example.com"}', "ReadFile")
        assert url is None

    def test_arguments_none(self):
        url = _ToolCallBlock._extract_full_url(None, "FetchURL")
        assert url is None

    def test_invalid_json(self):
        url = _ToolCallBlock._extract_full_url("not json", "FetchURL")
        assert url is None

    def test_missing_url_field(self):
        url = _ToolCallBlock._extract_full_url('{"query": "hello"}', "FetchURL")
        assert url is None

    def test_empty_string(self):
        url = _ToolCallBlock._extract_full_url("", "FetchURL")
        assert url is None


def test_tool_call_block_renders_running_worklog_entry():
    block = _ToolCallBlock(_tool_call("ReadFile", '{"file_path":"src/app.py"}'))
    output = _plain(block.compose())

    assert "Read" in output
    assert "src/app.py" in output
    assert "running" in output.lower()


def test_tool_call_block_renders_running_subagent_with_text_safe_solid_circle(monkeypatch):
    monkeypatch.setattr(_worklog, "blink_visible", lambda now=None: True)
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"Audit UI"}'))
    output = _plain(block.compose())

    assert "●" in output
    assert not any(frame in output for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
    assert "Subagent" in output
    assert "Audit UI" in output
    assert "running" in output.lower()


def test_tool_call_block_renders_completed_worklog_entry():
    block = _ToolCallBlock(_tool_call("Grep", '{"pattern":"FIXME"}'))
    block.finish(ToolOk(output=""))
    output = _plain(block.compose())

    assert "Search" in output
    assert "FIXME" in output
    assert "completed" in output.lower()


def test_tool_call_block_renders_failed_worklog_entry():
    block = _ToolCallBlock(_tool_call("Bash", '{"command":"pytest"}'))
    block.finish(ToolError(message="exit code 1", brief="failed"))
    output = _plain(block.compose())

    assert "Shell" in output
    assert "pytest" in output
    assert "failed" in output.lower()
    assert "exit code 1" in output


def test_card_result_text_includes_error_message_before_output():
    result = ToolError(
        message="Pattern `**/*.py` starts with '**' which is not allowed.",
        output="src/\npackages/",
        brief="Unsafe pattern",
    )

    text = _ToolCallBlock._card_result_text(result)

    assert text.startswith("Pattern `**/*.py` starts")
    assert "src/" in text


def test_tool_call_block_truncates_long_shell_command_target():
    command = "python - <<'PY'\n" + "print('x')\n" * 20 + "PY"
    block = _ToolCallBlock(_tool_call("Bash", json.dumps({"command": command})))
    output = _plain(block.compose())

    assert "python - <<'PY'" in output
    assert "print('x')" in output
    assert command not in output
    assert "..." in output


def test_tool_call_block_renders_denied_as_denied_not_failed():
    block = _ToolCallBlock(_tool_call("Bash", '{"command":"rm -rf /"}'))
    block.finish(ToolError(message="user dismissed permission", brief="denied"))
    output = _plain(block.compose())

    assert "Shell" in output
    assert "denied" in output.lower()
    assert "failed" not in output.lower()


def test_tool_call_block_renders_display_cards_under_completed_entry():
    block = _ToolCallBlock(_tool_call("Bash", '{"command":"pytest"}'))
    block.finish(ToolOk(output="", brief="Tests passed\n\nAll clear"))
    output = _plain(block.compose())

    assert "Shell" in output
    assert "Report" in output
    assert "Tests passed" in output


def test_completed_subagent_renders_compact_summary():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"Audit UI"}'))
    block.set_subagent_metadata("a143aa989", "explore")
    for index in range(7):
        call = _tool_call_with_id(
            f"sub-{index}",
            "ReadFile",
            json.dumps({"path": f"web/src/components/file-{index}.tsx"}),
        )
        block.append_sub_tool_call(call)
        block.finish_sub_tool_call(ToolResult(tool_call_id=call.id, return_value=ToolOk(output="")))

    block.finish(ToolOk(output=""))
    output = _plain(block.compose())

    assert "Subagent" in output
    assert "completed" in output.lower()
    assert "7 tool calls" in output
    assert "tools: Read ×7" in output
    assert output.count("ReadFile") <= 4


def test_completed_subagent_summarizes_changed_files_and_tool_counts():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"Implement UI"}'))
    calls = [
        _tool_call_with_id("read-1", "ReadFile", json.dumps({"path": "src/app.py"})),
        _tool_call_with_id("read-2", "ReadFile", json.dumps({"path": "src/ui.py"})),
        _tool_call_with_id("write-1", "WriteFile", json.dumps({"path": "src/new.py"})),
        _tool_call_with_id("edit-1", "StrReplaceFile", json.dumps({"path": "src/existing.py"})),
        _tool_call_with_id("shell-1", "Shell", json.dumps({"command": "pytest"})),
    ]
    for call in calls:
        block.append_sub_tool_call(call)
        block.finish_sub_tool_call(ToolResult(tool_call_id=call.id, return_value=ToolOk(output="")))

    block.finish(ToolOk(output="done"))
    output = _plain(block.compose())

    assert "tools:" in output
    assert "Read ×2" in output
    assert "Write" in output
    assert "Edit" in output
    assert "Shell" in output
    assert "changed: src/new.py, src/existing.py" in output


def test_append_sub_output_part_accumulates_text():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"ls"}')
    block.append_sub_tool_call(call)
    block.append_sub_output_part("sub-1", "file1.py\n")
    block.append_sub_output_part("sub-1", "file2.py\n")
    combined = "".join(block._subagent_output_parts["sub-1"])
    assert "file1.py" in combined
    assert "file2.py" in combined


def test_append_sub_output_part_discards_unknown_call_id():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    # no append_sub_tool_call — id is unknown
    block.append_sub_output_part("ghost-id", "should be ignored\n")
    assert "ghost-id" not in block._subagent_output_parts


def test_append_sub_output_part_caps_buffer_at_200_chars():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"find ."}')
    block.append_sub_tool_call(call)
    # Fill with >200 chars in one shot
    block.append_sub_output_part("sub-1", "x" * 300)
    combined = "".join(block._subagent_output_parts["sub-1"])
    assert len(combined) <= 200


def test_append_sub_output_part_caps_buffer_across_multiple_appends():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"ls"}')
    block.append_sub_tool_call(call)
    for _ in range(30):
        block.append_sub_output_part("sub-1", "x" * 10)  # 300 chars total, 10 at a time
    combined = "".join(block._subagent_output_parts["sub-1"])
    assert len(combined) <= 200


def test_append_sub_output_part_tracks_stderr():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"cat missing"}')
    block.append_sub_tool_call(call)
    block.append_sub_output_part("sub-1", "No such file\n", stream="stderr")
    assert block._subagent_output_had_stderr.get("sub-1") is True


def test_mark_sub_execution_started_records_id():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"ls"}')
    block.append_sub_tool_call(call)
    block.mark_sub_execution_started("sub-1")
    assert "sub-1" in block._subagent_execution_started


def test_mark_sub_execution_started_discards_unknown_id():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    block.mark_sub_execution_started("ghost-id")  # should not raise
    assert "ghost-id" not in block._subagent_execution_started


def test_finish_sub_tool_call_cleans_up_output_state():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"ls"}')
    block.append_sub_tool_call(call)
    block.append_sub_output_part("sub-1", "output\n")
    block.mark_sub_execution_started("sub-1")
    block.finish_sub_tool_call(ToolResult(tool_call_id="sub-1", return_value=ToolOk(output="")))
    assert "sub-1" not in block._subagent_output_parts
    assert "sub-1" not in block._subagent_output_had_stderr
    assert "sub-1" not in block._subagent_execution_started


def test_running_agent_shows_ongoing_sub_tool_calls():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    call = _tool_call_with_id("sub-1", "Read", '{"file_path":"src/app.py"}')
    block.append_sub_tool_call(call)
    output = _plain(block.compose())
    assert "Read" in output
    assert "src/app.py" in output


def test_running_agent_shows_streamed_output_preview():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"grep -r TODO ."}')
    block.append_sub_tool_call(call)
    block.append_sub_output_part("sub-1", "src/app.py:42: # TODO: fix\n")
    output = _plain(block.compose())
    assert "src/app.py:42" in output


def test_running_agent_card_style_shows_ongoing_sub_tool_calls(monkeypatch):
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "card")
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan","prompt":"scan"}'))
    call = _tool_call_with_id("sub-1", "Read", '{"file_path":"src/app.py"}')
    block.append_sub_tool_call(call)
    output = _plain(block.compose())
    assert "Read" in output
    assert "src/app.py" in output


def test_running_agent_card_style_shows_streamed_output_preview(monkeypatch):
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "card")
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan","prompt":"scan"}'))
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"grep -r TODO ."}')
    block.append_sub_tool_call(call)
    block.mark_sub_execution_started("sub-1")
    block.append_sub_output_part("sub-1", "src/app.py:42: # TODO: fix\n")
    output = _plain(block.compose())
    assert "src/app.py:42" in output


def test_running_agent_shows_only_last_4_output_lines():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"find ."}')
    block.append_sub_tool_call(call)
    lines = [f"line{i}\n" for i in range(10)]
    block.append_sub_output_part("sub-1", "".join(lines))
    output = _plain(block.compose())
    assert "line9" in output
    assert "line6" in output
    assert "line5" not in output
    assert "line0" not in output


def test_running_agent_caps_visible_running_rows_at_2():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    for i in range(5):
        call = _tool_call_with_id(f"sub-{i}", "Read", f'{{"file_path":"src/file{i}.py"}}')
        block.append_sub_tool_call(call)
    output = _plain(block.compose())
    assert "more running" in output


def test_running_agent_rows_stay_visible_with_finished_sub_tool_calls():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    for i in range(4):
        call = _tool_call_with_id(f"done-{i}", "Read", f'{{"file_path":"src/done{i}.py"}}')
        block.append_sub_tool_call(call)
        block.finish_sub_tool_call(ToolResult(tool_call_id=call.id, return_value=ToolOk(output="")))

    running = _tool_call_with_id("live-1", "Read", '{"file_path":"src/live.py"}')
    block.append_sub_tool_call(running)

    output = _plain(block.compose())
    assert "src/live.py" in output


def test_finished_sub_tool_calls_not_shown_in_output_preview():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"ls"}')
    block.append_sub_tool_call(call)
    block.append_sub_output_part("sub-1", "SHOULD_NOT_APPEAR\n")
    block.finish_sub_tool_call(ToolResult(tool_call_id="sub-1", return_value=ToolOk(output="")))
    output = _plain(block.compose())
    assert "SHOULD_NOT_APPEAR" not in output


@pytest.fixture
def _card_style_with_builtin_renderers(monkeypatch):
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "card")
    clear_tool_renderers()
    register_builtin_renderers()
    yield
    clear_tool_renderers()


@pytest.mark.parametrize(
    ("tool_name", "full_args"),
    [
        ("Shell", '{"command": "ls -la", "description": "List files"}'),
        ("Agent", '{"subagent_type": "coder", "description": "scan", "prompt": "scan repo"}'),
        ("StrReplaceFile", '{"path": "src/app.py", "old_string": "a", "new_string": "b"}'),
        ("WriteFile", '{"path": "src/app.py", "content": "x = 1"}'),
    ],
)
def test_streaming_args_never_flash_invalid_badge(
    _card_style_with_builtin_renderers, tool_name, full_args
):
    """The partial-JSON repair turns a key-without-value into null mid-stream;
    that must render as the pending state, never as the red <invalid> badge."""
    block = _ToolCallBlock(_tool_call(tool_name, ""))
    for ch in full_args:
        block.append_args_part(ch)
        rendered = _plain(block.compose())
        assert "<invalid>" not in rendered, f"flashed <invalid> after streaming {ch!r}"


def test_finished_call_with_non_string_command_still_shows_invalid_badge(
    _card_style_with_builtin_renderers,
):
    block = _ToolCallBlock(_tool_call("Shell", '{"command": 123}'))
    block.finish(ToolOk(output=""))
    assert "<invalid>" in _plain(block.compose())


def test_finished_call_with_null_command_still_shows_invalid_badge(
    _card_style_with_builtin_renderers,
):
    block = _ToolCallBlock(_tool_call("Shell", '{"command": null}'))
    block.finish(ToolOk(output=""))
    assert "<invalid>" in _plain(block.compose())


def test_run_agents_background_launch_stays_background_pending():
    block = _ToolCallBlock(
        _tool_call(
            "RunAgents",
            '{"summary":"scan","run_in_background":true,"agents":[{"name":"a","prompt":"p"}]}',
        )
    )
    block.finish(
        ToolOk(
            output=(
                "tool_status: launched\n"
                "mode: background\n"
                "agent_count: 1\n"
                "agents:\n"
                "- name: a\n"
                "  subagent_type: explore\n"
                "  status: starting\n"
                "  task_id: agent-abc\n"
            )
        )
    )
    assert block.finished
    assert block.is_background_pending


def test_run_agents_foreground_completion_is_not_background_pending():
    block = _ToolCallBlock(
        _tool_call(
            "RunAgents",
            '{"summary":"scan","run_in_background":false,"agents":[{"name":"a","prompt":"p"}]}',
        )
    )
    block.finish(
        ToolOk(
            output=(
                "tool_status: success\n"
                "mode: foreground\n"
                "agent_count: 1\n"
                "agents:\n"
                "- name: a\n"
                "  subagent_type: explore\n"
                "  status: completed\n"
            )
        )
    )
    assert block.finished
    assert not block.is_background_pending
