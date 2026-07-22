from __future__ import annotations

import json

import pytest
from pythinker_core.message import ToolCall
from pythinker_core.tooling import ToolError, ToolOk, ToolReturnValue
from rich.console import Console

from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolResultPayload,
    clear_tool_renderers,
    get_tool_renderer,
    register_builtin_renderers,
)
from pythinker_code.ui.shell.visualize import _blocks as blocks_module
from pythinker_code.ui.shell.visualize import _ToolCallBlock, _worklog
from pythinker_code.wire.types import ToolResult


@pytest.fixture(autouse=True)
def _legacy_tui_style(monkeypatch):
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "pythinker")


def _plain(renderable, *, width: int = 120) -> str:
    console = Console(record=True, width=width, color_system=None)
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


def test_completed_agent_shows_completion_without_legacy_tool_rollup():
    # A completed single Agent renders its result (via the result renderer in card
    # style, a plain "completed" label otherwise). The old per-tool rollup
    # ("N tool calls", "tools: Read ×N") is superseded, and raw nested tool
    # payloads never leak into the collapsed view.
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"Audit UI"}'))
    block.set_subagent_metadata("a143aa989", "explore", "Audit UI")
    for index in range(7):
        call = _tool_call_with_id(
            f"sub-{index}",
            "ReadFile",
            json.dumps({"path": f"web/src/components/file-{index}.tsx"}),
        )
        block.append_sub_tool_call(call, agent_id="a143aa989")
        block.finish_sub_tool_call(
            ToolResult(tool_call_id=call.id, return_value=ToolOk(output="")),
            agent_id="a143aa989",
        )

    block.finish(ToolOk(output=""))
    output = _plain(block.compose())

    assert "completed" in output.lower()
    assert "7 tool calls" not in output
    assert "tools:" not in output
    assert "Read ×7" not in output
    for leaked in ("ReadFile", "file-0.tsx", "web/src/components"):
        assert leaked not in output


def test_completed_agent_hides_changed_files_and_tool_counts():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"Implement UI"}'))
    block.set_subagent_metadata("agent-impl", "coder", "Implement UI")
    calls = [
        _tool_call_with_id("read-1", "ReadFile", json.dumps({"path": "src/app.py"})),
        _tool_call_with_id("write-1", "WriteFile", json.dumps({"path": "src/new.py"})),
        _tool_call_with_id("edit-1", "StrReplaceFile", json.dumps({"path": "src/existing.py"})),
        _tool_call_with_id("shell-1", "Shell", json.dumps({"command": "pytest"})),
    ]
    for call in calls:
        block.append_sub_tool_call(call, agent_id="agent-impl")
        block.finish_sub_tool_call(
            ToolResult(tool_call_id=call.id, return_value=ToolOk(output="")),
            agent_id="agent-impl",
        )

    block.finish(ToolOk(output="done"))
    output = _plain(block.compose())

    assert "completed" in output.lower()
    assert "tools:" not in output
    assert "changed:" not in output
    for leaked in ("WriteFile", "StrReplaceFile", "src/new.py", "src/existing.py", "pytest"):
        assert leaked not in output


def test_agent_sub_output_updates_activity_without_buffering_raw_text():
    # Single Agent uses the payload-free semantic model: streamed sub-output moves
    # the owner's activity to "running" but the raw text is never buffered or shown.
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    block.set_subagent_metadata("agent-1", "coder", "scan")
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"ls"}')
    block.append_sub_tool_call(call, agent_id="agent-1")
    block.append_sub_output_part("sub-1", "file1.py\n", agent_id="agent-1")
    block.append_sub_output_part("sub-1", "file2.py\n", agent_id="agent-1")

    assert "sub-1" not in block._subagent_output_parts
    output = _plain(block.compose())
    assert "running command…" in output
    for leaked in ("file1.py", "file2.py"):
        assert leaked not in output


def test_append_sub_output_part_discards_unknown_call_id():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    # no append_sub_tool_call / no owner — id is unknown, nothing is buffered
    block.append_sub_output_part("ghost-id", "should be ignored\n")
    assert "ghost-id" not in block._subagent_output_parts


def test_agent_sub_output_never_leaks_regardless_of_volume():
    # No raw payload is ever surfaced for a single Agent, even for large output.
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    block.set_subagent_metadata("agent-1", "coder", "scan")
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"find ."}')
    block.append_sub_tool_call(call, agent_id="agent-1")
    block.append_sub_output_part("sub-1", "SECRET_" + "x" * 300, agent_id="agent-1")

    assert "sub-1" not in block._subagent_output_parts
    assert "SECRET_" not in _plain(block.compose())


def test_agent_sub_stderr_does_not_buffer_raw_text():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    block.set_subagent_metadata("agent-1", "coder", "scan")
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"cat missing"}')
    block.append_sub_tool_call(call, agent_id="agent-1")
    block.append_sub_output_part("sub-1", "No such file\n", stream="stderr", agent_id="agent-1")

    assert "sub-1" not in block._subagent_output_had_stderr
    assert "No such file" not in _plain(block.compose())


def test_mark_sub_execution_started_records_id():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"ls"}')
    block.append_sub_tool_call(call)
    block.mark_sub_execution_started("sub-1")
    assert "sub-1" in block._subagent_execution_started


def test_mark_sub_execution_started_unknown_id_renders_no_phantom_activity():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    block.mark_sub_execution_started("ghost-id")  # should not raise
    # An unknown id creates no semantic activity row for the single Agent.
    assert "ghost-id" not in block._subagent_activities
    assert "ghost-id" not in _plain(block.compose())


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


def test_running_agent_shows_semantic_activity_not_raw_tool_calls():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    block.set_subagent_metadata("agent-1", "coder", "scan")
    call = _tool_call_with_id("sub-1", "Read", '{"file_path":"src/app.py"}')
    block.append_sub_tool_call(call, agent_id="agent-1")
    block.mark_sub_execution_started("sub-1", agent_id="agent-1")
    output = _plain(block.compose())
    assert "reading…" in output
    assert "Read" not in output
    assert "src/app.py" not in output


def test_running_agent_suppresses_streamed_output_preview():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    block.set_subagent_metadata("agent-1", "coder", "scan")
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"grep -r TODO ."}')
    block.append_sub_tool_call(call, agent_id="agent-1")
    block.append_sub_output_part("sub-1", "src/app.py:42: # TODO: fix\n", agent_id="agent-1")
    output = _plain(block.compose())
    assert "running command…" in output
    assert "src/app.py:42" not in output
    assert "grep -r TODO" not in output


def test_running_agent_card_style_shows_semantic_activity_not_raw_tool_calls(monkeypatch):
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "card")
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan","prompt":"scan"}'))
    block.set_subagent_metadata("agent-1", "coder", "scan")
    call = _tool_call_with_id("sub-1", "Read", '{"file_path":"src/app.py"}')
    block.append_sub_tool_call(call, agent_id="agent-1")
    block.mark_sub_execution_started("sub-1", agent_id="agent-1")
    output = _plain(block.compose())
    assert "reading…" in output
    assert "src/app.py" not in output


def test_running_agent_card_style_suppresses_streamed_output_preview(monkeypatch):
    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "card")
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan","prompt":"scan"}'))
    block.set_subagent_metadata("agent-1", "coder", "scan")
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"grep -r TODO ."}')
    block.append_sub_tool_call(call, agent_id="agent-1")
    block.mark_sub_execution_started("sub-1", agent_id="agent-1")
    block.append_sub_output_part("sub-1", "src/app.py:42: # TODO: fix\n", agent_id="agent-1")
    output = _plain(block.compose())
    assert "running command…" in output
    assert "src/app.py:42" not in output


def test_running_agent_never_leaks_streamed_output_lines():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    block.set_subagent_metadata("agent-1", "coder", "scan")
    call = _tool_call_with_id("sub-1", "Bash", '{"command":"find ."}')
    block.append_sub_tool_call(call, agent_id="agent-1")
    lines = [f"line{i}\n" for i in range(10)]
    block.append_sub_output_part("sub-1", "".join(lines), agent_id="agent-1")
    output = _plain(block.compose())
    assert "running command…" in output
    for i in range(10):
        assert f"line{i}" not in output


def test_running_agent_renders_one_payload_free_row_per_active_agent():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    for i in range(5):
        agent_id = f"agent-{i}"
        block.set_subagent_metadata(agent_id, "coder", f"task {i}")
        call = _tool_call_with_id(f"sub-{i}", "Read", f'{{"file_path":"src/file{i}.py"}}')
        block.append_sub_tool_call(call, agent_id=agent_id)
        block.mark_sub_execution_started(f"sub-{i}", agent_id=agent_id)
    output = _plain(block.compose())
    assert output.count("reading…") >= 1
    for i in range(5):
        assert f"src/file{i}.py" not in output


def test_running_agent_activity_survives_finished_sub_tool_calls():
    block = _ToolCallBlock(_tool_call("Agent", '{"description":"scan"}'))
    block.set_subagent_metadata("agent-1", "coder", "scan")
    for i in range(4):
        call = _tool_call_with_id(f"done-{i}", "Read", f'{{"file_path":"src/done{i}.py"}}')
        block.append_sub_tool_call(call, agent_id="agent-1")
        block.finish_sub_tool_call(
            ToolResult(tool_call_id=call.id, return_value=ToolOk(output="")),
            agent_id="agent-1",
        )

    running = _tool_call_with_id("live-1", "Read", '{"file_path":"src/live.py"}')
    block.append_sub_tool_call(running, agent_id="agent-1")
    block.mark_sub_execution_started("live-1", agent_id="agent-1")

    output = _plain(block.compose())
    assert "reading…" in output
    assert "src/live.py" not in output
    assert "src/done0.py" not in output


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


def test_streaming_large_arguments_bounds_label_extraction_scans(monkeypatch) -> None:
    original = blocks_module.extract_key_argument
    extraction_calls = 0
    first_non_empty_call: int | None = None

    def _recording_extract(arguments: str, tool_name: str) -> str | None:
        nonlocal extraction_calls, first_non_empty_call
        extraction_calls += 1
        result = original(arguments, tool_name)
        if result and first_non_empty_call is None:
            first_non_empty_call = extraction_calls
        return result

    monkeypatch.setattr(blocks_module, "extract_key_argument", _recording_extract)
    block = _ToolCallBlock(_tool_call("Shell", ""))
    arguments = json.dumps({"command": "x" * (64 * 1024)})
    encoding = "utf-8"
    for offset in range(0, len(arguments), 64):
        block.append_args_part(arguments[offset : offset + 64])
    block.mark_execution_started()

    assert first_non_empty_call is not None
    scanned_after_label = extraction_calls - first_non_empty_call
    growth_scans = (len(arguments.encode(encoding=encoding)) + 1023) // 1024
    assert scanned_after_label <= growth_scans + 1


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


def test_run_agents_background_launch_keeps_live_agent_activity():
    block = _ToolCallBlock(
        _tool_call(
            "RunAgents",
            '{"summary":"scan","run_in_background":true,"agents":[{"name":"a","prompt":"p"}]}',
        )
    )
    block.set_subagent_metadata("agent-abc", "explore", "Audit the renderer")
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

    output = _plain(block.compose())

    assert "waiting Explore Audit the renderer" in output
    assert "Run Agents completed" not in output


@pytest.mark.usefixtures("_card_style_with_builtin_renderers")
def test_run_agents_background_launch_keeps_live_agent_activity_in_card_style():
    block = _ToolCallBlock(
        _tool_call(
            "RunAgents",
            '{"summary":"scan","run_in_background":true,"agents":[{"name":"a","prompt":"p"}]}',
        )
    )
    block.set_subagent_metadata("agent-abc", "explore", "Audit the renderer")
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

    output = _plain(block.compose())

    assert "waiting Explore Audit the renderer" in output
    assert "Run Agents completed" not in output


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


def test_run_agents_sanitizes_multiline_descriptions_without_breaking_same_type_activity():
    block = _ToolCallBlock(
        _tool_call(
            "RunAgents",
            '{"summary":"scan","run_in_background":false,"agents":[{"name":"a"},{"name":"b"}]}',
        )
    )
    block.set_subagent_metadata(
        "agent-alpha",
        "explore",
        "Map\trenderer\ncallbacks\x1b[31m now\x1b[0m\r",
    )
    block.set_subagent_metadata("agent-beta", "explore", "Read activity tree")
    block.append_sub_tool_call(
        _tool_call_with_id("sub-alpha", "ReadFile", '{"path":"src/renderer.py"}'),
        agent_id="agent-alpha",
    )
    block.mark_sub_execution_started("sub-alpha", agent_id="agent-alpha")

    output = _plain(block.compose(), width=52)
    lines = output.splitlines()

    assert output.count("running Explore") == 1
    assert "Map renderer callbacks now" in output
    assert "Map\trenderer" not in output
    assert "\r" not in output
    assert "\x1b" not in output
    assert "reading…" in output
    assert "thinking…" in output
    assert "callbacks now" not in lines
    assert all(len(line) <= 52 for line in lines)


def test_lsp_card_boundary_passes_nested_count_extras_to_renderer(
    _card_style_with_builtin_renderers,
):
    result = ToolReturnValue(
        is_error=False,
        output="src/foo.py:12:8\nsrc/foo.py:20:4",
        message="",
        display=[],
        extras={"operation": "findReferences", "result_count": 2, "file_count": 1},
    )
    details = _ToolCallBlock._card_result_details(result)
    payload = ToolResultPayload(
        text=_ToolCallBlock._card_result_text(result),
        is_error=result.is_error,
        details=details,
    )
    ctx = ToolRenderContext(
        args={"operation": "hover"},
        tool_call_id="tc-lsp",
        has_result=True,
    )
    renderer = get_tool_renderer("LSP")

    assert renderer is not None
    assert renderer.render_result is not None
    rendered = _plain(renderer.render_result(ctx, payload))

    assert details["extras"] == {
        "operation": "findReferences",
        "result_count": 2,
        "file_count": 1,
    }
    assert "Found 2 references" in rendered
    assert "Hover info available" not in rendered
    assert "src/foo.py:12:8" not in rendered
