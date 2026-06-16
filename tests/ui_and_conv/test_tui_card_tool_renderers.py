"""Smoke tests for the Pythinker ported tool renderers.

These tests render each builtin through ``ToolExecutionComponent`` and assert
the human-readable output contains the expected fragments. They are not
pixel-snapshots: tweaking exact spacing/styling is fine, but the substantive
information (tool name, path, key args, result preview, expansion hint) must
remain visible.
"""

from __future__ import annotations

import pytest

from pythinker_code.tools.display import DiffDisplayBlock
from pythinker_code.ui.shell.components import (
    ToolExecutionComponent,
    compute_edit_diff_string,
    render_diff,
    render_plain,
)
from pythinker_code.ui.shell.glyphs import QUESTION_MARKER
from pythinker_code.ui.shell.tool_renderers import (
    ToolResultPayload,
    clear_tool_renderers,
    get_tool_renderer,
    register_builtin_renderers,
)
from pythinker_code.ui.shell.tool_renderers._file_diff import preview_from_diff_blocks
from pythinker_code.ui.shell.tool_renderers._render_utils import loading_marker
from pythinker_code.ui.shell.tool_renderers.generic import generic_renderer
from pythinker_code.ui.theme import tui_rich_style


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_tool_renderers()
    register_builtin_renderers()
    yield
    clear_tool_renderers()


def _render(
    tool: str,
    args: dict,
    *,
    output: str = "",
    is_error: bool = False,
    expanded: bool = False,
    width: int = 100,
    details: dict | None = None,
) -> str:
    defn = get_tool_renderer(tool)
    assert defn is not None, f"renderer not registered for {tool!r}"
    comp = ToolExecutionComponent(tool, "tc-1", definition=defn, cwd="/repo")
    comp.update_args(args)
    comp.set_args_complete()
    comp.mark_execution_started()
    comp.set_result(ToolResultPayload(text=output, is_error=is_error, details=details or {}))
    comp.set_expanded(expanded)
    return render_plain(comp.render(), width=width)


def _render_with_definition(
    tool: str,
    args: dict,
    *,
    output: str = "",
    is_error: bool = False,
    width: int = 100,
) -> str:
    comp = ToolExecutionComponent(tool, "tc-1", definition=generic_renderer(), cwd="/repo")
    comp.update_args(args)
    comp.set_args_complete()
    comp.mark_execution_started()
    comp.set_result(ToolResultPayload(text=output, is_error=is_error))
    return render_plain(comp.render(), width=width)


def _render_running(tool: str, args: dict, *, width: int = 100) -> str:
    defn = get_tool_renderer(tool)
    assert defn is not None, f"renderer not registered for {tool!r}"
    comp = ToolExecutionComponent(tool, "tc-1", definition=defn, cwd="/repo")
    comp.update_args(args)
    comp.set_args_complete()
    comp.mark_execution_started()
    return render_plain(comp.render(), width=width)


def _render_streaming(tool: str, args: dict, *, width: int = 100) -> str:
    defn = get_tool_renderer(tool)
    assert defn is not None, f"renderer not registered for {tool!r}"
    comp = ToolExecutionComponent(tool, "tc-1", definition=defn, cwd="/repo")
    comp.update_args(args)
    comp.mark_execution_started()
    return render_plain(comp.render(), width=width)


def test_loading_marker_pulses_muted_transcript_dot_then_finishes_green():
    visible = loading_marker(now=0.0)
    hidden = loading_marker(now=0.9)
    done = loading_marker(done=True)

    assert visible.plain == "⏺ "
    assert visible.style == tui_rich_style("muted")
    assert hidden.plain == "  "
    assert hidden.style == tui_rich_style("muted")
    assert done.plain == "⏺ "
    assert done.style == tui_rich_style("success")


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_read_renders_path_and_range():
    rendered = _render(
        "ReadFile",
        {"path": "/repo/src/foo.py", "line_offset": 10, "n_lines": 30},
        output="line1\nline2",
    )
    assert "⏺ Read(" in rendered
    assert "src/foo.py" in rendered
    assert ":10-39" in rendered
    assert "Read 1 file (ctrl+o to expand)" in rendered


def test_read_renders_negative_offset_as_tail():
    rendered = _render(
        "ReadFile",
        {"path": "/repo/src/foo.py", "line_offset": -100},
        output="line1",
    )
    assert "src/foo.py" in rendered
    assert ":tail 100" in rendered
    # The confusing forward-range form must not appear for tail reads.
    assert "--" not in rendered


def test_read_renders_negative_offset_with_limit():
    rendered = _render(
        "ReadFile",
        {"path": "/repo/src/foo.py", "line_offset": -100, "n_lines": 20},
        output="line1",
    )
    assert ":tail 100 · limit 20" in rendered


def test_read_result_matches_reference_summary_only():
    body = "\n".join(f"line {i}" for i in range(20))
    rendered = _render("ReadFile", {"path": "/repo/x.py"}, output=body)
    assert "Read 1 file (ctrl+o to expand)" in rendered
    assert "line 0" not in rendered
    assert "more lines" not in rendered


def test_read_error_prefers_structured_message():
    defn = get_tool_renderer("ReadFile")
    assert defn is not None
    comp = ToolExecutionComponent("ReadFile", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"path": "/repo/missing.py"})
    comp.mark_execution_started()
    comp.set_args_complete()
    comp.set_result(
        ToolResultPayload(
            text="",
            is_error=True,
            details={"message": "File does not exist: /repo/missing.py"},
        )
    )

    rendered = render_plain(comp.render(), width=100)
    assert "File not found" in rendered


def test_read_directory_result_says_listed_directory():
    defn = get_tool_renderer("ReadFile")
    assert defn is not None
    comp = ToolExecutionComponent("ReadFile", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"path": "/repo/.pythinker-review/security-scan/data/project"})
    comp.mark_execution_started()
    comp.set_args_complete()
    comp.set_result(
        ToolResultPayload(
            text="├── project.json\n└── runs/",
            is_error=False,
            details={
                "message": "Directory listing for `/repo/.pythinker-review/security-scan/data/project`. Use ReadFile on a file path to read file contents.",
                "output": "├── project.json\n└── runs/",
            },
        )
    )

    rendered = render_plain(comp.render(), width=100)
    assert "Listed 1 directory" in rendered
    assert "Read 1 file" not in rendered


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def test_write_shows_path_and_content_preview():
    rendered = _render(
        "WriteFile",
        {"path": "/repo/new.py", "content": "def f():\n    return 1\n"},
        output="Successfully wrote",
    )
    assert "⏺ Write(new.py)" in rendered
    assert "Wrote 2 lines to new.py" in rendered
    assert "1 def f():" in rendered


def test_write_error_surfaced():
    rendered = _render(
        "WriteFile",
        {"path": "/repo/new.py", "content": "x"},
        output="Permission denied",
        is_error=True,
    )
    assert "Permission denied" in rendered


def test_write_existing_file_renders_diff_for_add_only_change():
    rendered = _render(
        "WriteFile",
        {"path": "/repo/report.md", "content": "intro\nnew section\n"},
        details={
            "display": [
                DiffDisplayBlock(
                    path="/repo/report.md",
                    old_text="intro",
                    new_text="intro\nnew section",
                    old_start=1,
                    new_start=1,
                )
            ]
        },
    )

    assert "Added 1 line" in rendered
    assert " 2 +new section" in rendered
    assert "Wrote 2 lines" not in rendered


def test_write_huge_new_file_shows_wrote_header_not_diff():
    content = "".join(f"line {i}\n" for i in range(12_000))
    rendered = _render(
        "WriteFile",
        {"path": "/repo/big.py", "content": content},
        details={
            "display": [
                DiffDisplayBlock(
                    path="/repo/big.py",
                    old_text="(0 lines)",
                    new_text="(12000 lines)",
                    old_start=1,
                    new_start=1,
                    is_summary=True,
                )
            ]
        },
    )
    assert "Wrote 12000 lines" in rendered
    assert "removed 1 line" not in rendered


def test_write_large_diff_can_expand_from_completed_card():
    defn = get_tool_renderer("WriteFile")
    assert defn is not None
    comp = ToolExecutionComponent("WriteFile", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"path": "/repo/report.md", "content": "new"})
    comp.set_args_complete()
    comp.mark_execution_started()
    comp.set_result(
        ToolResultPayload(
            details={
                "display": [
                    DiffDisplayBlock(
                        path="/repo/report.md",
                        old_text="\n".join(f"old {i}" for i in range(30)),
                        new_text="\n".join(f"new {i}" for i in range(30)),
                        old_start=1,
                        new_start=1,
                    )
                ]
            }
        )
    )

    collapsed = render_plain(comp.render(), width=100)
    assert comp.can_expand
    assert "ctrl+o to expand" in collapsed
    assert "new 29" not in collapsed

    comp.toggle_expanded()
    expanded = render_plain(comp.render(), width=100)
    assert "new 29" in expanded


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


def test_edit_renders_inline_diff():
    rendered = _render(
        "StrReplaceFile",
        {"path": "/repo/foo.py", "edit": {"old": "return 1", "new": "return 2"}},
    )
    assert "Update" in rendered
    assert "foo.py" in rendered
    assert "removed 1 line" in rendered
    assert "Added 1 line" in rendered
    assert "return 1" in rendered
    assert "return 2" in rendered
    assert " 1 -return 1" in rendered
    assert " 1 +return 2" in rendered


def test_edit_multi_count_in_header():
    rendered = _render(
        "StrReplaceFile",
        {
            "path": "/repo/foo.py",
            "edit": [
                {"old": "a", "new": "b"},
                {"old": "c", "new": "d"},
            ],
        },
    )
    assert "(2 edits)" in rendered


def test_edit_prefers_structured_result_diff_blocks():
    defn = get_tool_renderer("StrReplaceFile")
    assert defn is not None
    comp = ToolExecutionComponent("StrReplaceFile", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"path": "/repo/foo.py", "edit": {"old": "old", "new": "new"}})
    comp.mark_execution_started()
    comp.set_args_complete()
    comp.set_result(
        ToolResultPayload(
            text="File successfully edited.",
            details={
                "display": [
                    DiffDisplayBlock(
                        path="/repo/foo.py",
                        old_text="keep\nold",
                        new_text="keep\nnew",
                        old_start=40,
                        new_start=40,
                    )
                ]
            },
        )
    )

    rendered = render_plain(comp.render(), width=100)
    assert "removed 1 line" in rendered
    assert "Added 1 line" in rendered
    assert "41 -old" in rendered
    assert "41 +new" in rendered


def test_summary_diff_blocks_count_each_line():
    preview = preview_from_diff_blocks(
        [
            DiffDisplayBlock(
                path="/repo/large.py",
                old_text="line1\nline2\nline3",
                new_text="new1\nnew2",
                is_summary=True,
            )
        ]
    )

    assert preview is not None
    assert preview.summary_only is True
    assert preview.removed == 3
    assert preview.added == 2
    assert "- line2" in preview.diff_text
    assert "+ new2" in preview.diff_text


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


def test_grep_renders_pattern_and_path():
    rendered = _render(
        "Grep",
        {"pattern": "def\\s+", "path": "/repo/src", "glob": "*.py"},
        output="src/foo.py:10: def hello():",
    )
    assert "⏺ Search(" in rendered
    assert "/def\\s+/" in rendered
    assert "src" in rendered
    assert "*.py" in rendered
    assert "Found 1 file" in rendered


def test_grep_content_counts_paths_with_punctuation():
    rendered = _render(
        "Grep",
        {"pattern": "needle", "path": "/repo", "output_mode": "content"},
        output="src/a-b.py:10:needle\nsrc/a-b.py-11-context\nsrc/colon:name.py:3:needle",
    )
    assert "Found 3 lines across 2 files" in rendered


def test_invalid_empty_grep_call_names_missing_pattern():
    rendered = _render(
        "Grep",
        {},
        output=(
            "Error validating JSON arguments: 1 validation error for Params\n"
            "pattern\n  Field required"
        ),
        is_error=True,
    )
    assert "✘ Search(<missing pattern> in .)" in rendered
    assert "Error searching files" in rendered
    assert "Search ... in ." not in rendered


# ---------------------------------------------------------------------------
# find / glob
# ---------------------------------------------------------------------------


def test_glob_renders_pattern_and_directory():
    rendered = _render(
        "Glob",
        {"pattern": "**/*.py", "directory": "/repo/src"},
        output="src/a.py\nsrc/b.py",
    )
    assert "⏺ Find(" in rendered
    assert "**/*.py" in rendered
    assert "Found 2 files" in rendered


# ---------------------------------------------------------------------------
# bash / shell
# ---------------------------------------------------------------------------


def test_shell_renders_command_and_output_under_response_gutter():
    rendered = _render("Shell", {"command": "ls -la", "timeout": 60}, output="total 0")
    assert "⏺ Bash(ls -la)" in rendered
    assert "total 0" in rendered
    assert "⎿" in rendered


def test_shell_collapses_long_command_and_reports_output_lines():
    command = "\n".join(["echo first", "echo second", "echo third"])
    output = "\n".join(f"line {i}" for i in range(8))
    rendered = _render("Shell", {"command": command, "timeout": 60}, output=output)
    assert "echo first" in rendered
    assert "echo second" in rendered
    assert "echo third" not in rendered
    assert "… +4 lines (ctrl+o to expand)" in rendered


def test_shell_component_can_toggle_expansion_when_renderer_suppresses_generic_hint():
    defn = get_tool_renderer("Shell")
    assert defn is not None
    comp = ToolExecutionComponent("Shell", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"command": "pytest", "timeout": 60})
    comp.set_args_complete()
    comp.mark_execution_started()
    comp.set_result(ToolResultPayload(text="\n".join(f"line {i}" for i in range(8))))

    collapsed = render_plain(comp.render(), width=100)
    assert "line 3" not in collapsed
    assert comp.can_expand

    comp.toggle_expanded()
    expanded = render_plain(comp.render(), width=100)
    assert "line 3" in expanded


def test_shell_wraps_substantial_output_in_response_gutter():
    rendered = _render("Shell", {"command": "pytest"}, output="failed\nexit code 1")
    assert "⎿" in rendered
    assert "⎿    ⎿" not in rendered
    assert "failed" in rendered
    assert "exit code 1" in rendered


def test_shell_error_with_empty_output_shows_message():
    defn = get_tool_renderer("Shell")
    assert defn is not None
    comp = ToolExecutionComponent("Shell", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"command": "printf rejected > reject.txt"})
    comp.mark_execution_started()
    comp.set_args_complete()
    comp.set_result(
        ToolResultPayload(
            text="The tool call is rejected by the user.",
            is_error=True,
            details={"output": "", "message": "The tool call is rejected by the user."},
        )
    )

    rendered = render_plain(comp.render(), width=100)
    assert "The tool call is rejected by the user" in rendered
    assert "exit 1" not in rendered


def test_shell_error_uses_structured_exit_code_when_available():
    defn = get_tool_renderer("Shell")
    assert defn is not None
    comp = ToolExecutionComponent("Shell", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"command": "python -c 'raise SystemExit(2)'"})
    comp.mark_execution_started()
    comp.set_args_complete()
    comp.set_result(
        ToolResultPayload(
            text="Command failed with exit code: 2.",
            is_error=True,
            details={
                "output": "",
                "message": "Command failed with exit code: 2.",
                "extras": {"status": "failure", "exit_code": 2},
            },
        )
    )

    rendered = render_plain(comp.render(), width=100)
    assert "Command failed with exit code: 2" in rendered
    assert "exit 2" in rendered


def test_shell_uses_comment_label_for_long_script():
    command = "# build assets\n" + "\n".join(f"echo {i}" for i in range(5))
    rendered = _render("Shell", {"command": command, "timeout": 60}, output="ok")
    assert "⏺ Bash(build assets)" in rendered
    assert "echo 0" not in rendered


def test_shell_shows_timeout_only_when_nondefault():
    short = _render("Shell", {"command": "echo x", "timeout": 60}, output="x")
    assert "timeout" not in short
    long = _render("Shell", {"command": "echo x", "timeout": 600}, output="x")
    assert "timeout 600s" in long


def test_shell_background_marker():
    rendered = _render(
        "Shell",
        {"command": "sleep 100", "run_in_background": True, "description": "watch"},
        output="started",
    )
    assert "background: watch" in rendered


def test_running_tool_headers_do_not_duplicate_status_bullets():
    cases = [
        ("Shell", {"command": "ls packages/pythinker-review/AGENTS.md"}, "Bash("),
        ("ReadFile", {"path": "/repo/src/foo.py"}, "Read("),
        ("WriteFile", {"path": "/repo/src/foo.py", "content": "x"}, "Write("),
        (
            "StrReplaceFile",
            {"path": "/repo/src/foo.py", "edit": {"old": "a", "new": "b"}},
            "Update(",
        ),
        ("Grep", {"pattern": "needle", "path": "/repo"}, "Search("),
        ("Glob", {"pattern": "**/*.py", "directory": "/repo"}, "Find("),
        ("ReadSkill", {"skill_name": "review-pr"}, "Skill("),
        ("FetchURL", {"url": "https://example.com"}, "Fetch("),
        ("SearchWeb", {"query": "python"}, "WebSearch("),
        (
            "Agent",
            {"description": "audit", "prompt": "check", "subagent_type": "explore"},
            "Agent(",
        ),
        (
            "RunAgents",
            {
                "summary": "audit",
                "agents": [{"name": "scan", "prompt": "check", "subagent_type": "explore"}],
            },
            "RunAgents(",
        ),
        ("AskUserQuestion", {"questions": [{"question": "Continue?"}]}, "Ask("),
        ("Think", {"thought": "check"}, "Think"),
        ("TaskList", {"active_only": True}, "Tasks("),
        ("TaskOutput", {"task_id": "abc"}, "TaskOutput("),
        ("TaskStop", {"task_id": "abc"}, "TaskStop("),
        ("EnterPlanMode", {}, "Plan("),
        ("ExitPlanMode", {"options": [{"label": "Continue"}]}, "Plan("),
    ]
    for tool, args, label in cases:
        rendered = _render_running(tool, args, width=64)
        assert label in rendered
        assert "⏺ ⏺" not in rendered


def test_streaming_missing_args_use_preparing_rows_not_tool_ellipsis_placeholders():
    cases = [
        ("Shell", "Bash"),
        ("ReadFile", "Read"),
        ("WriteFile", "Write"),
        ("StrReplaceFile", "Update"),
        ("Grep", "Search"),
        ("Glob", "Find"),
        ("ReadSkill", "Skill"),
        ("FetchURL", "Fetch"),
        ("SearchWeb", "WebSearch"),
        ("Think", "Think"),
        ("AskUserQuestion", "Ask"),
        ("TaskOutput", "TaskOutput"),
        ("TaskStop", "TaskStop"),
        ("RunAgents", "RunAgents"),
    ]

    for tool, label in cases:
        rendered = _render_streaming(tool, {}, width=80)
        assert f"Preparing {label}…" in rendered
        assert f"{label}(...)" not in rendered
        assert f"{label}(…)" not in rendered


def test_invalid_empty_shell_call_names_missing_command():
    rendered = _render(
        "Shell",
        {},
        output=(
            "Error validating JSON arguments: 1 validation error for Params\n"
            "command\n  Field required"
        ),
        is_error=True,
    )
    assert "✘ Bash(<missing command>)" in rendered
    assert "$ ..." not in rendered


def test_task_output_header_shows_description_not_id():
    from pythinker_code.tools.display import BackgroundTaskDisplayBlock

    defn = get_tool_renderer("TaskOutput")
    assert defn is not None
    comp = ToolExecutionComponent("TaskOutput", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"task_id": "agent-pyl4xz6a", "block": True})
    comp.set_args_complete()
    comp.mark_execution_started()
    comp.set_result(
        ToolResultPayload(
            text="status: completed",
            details={
                "display": [
                    BackgroundTaskDisplayBlock(
                        task_id="agent-pyl4xz6a",
                        kind="agent",
                        status="completed",
                        description="Shell TUI mapping",
                    )
                ]
            },
        )
    )
    rendered = render_plain(comp.render(), width=80)
    # Friendly description is the primary label; raw id kept as a dim suffix.
    assert "Shell TUI mapping" in rendered
    assert rendered.index("Shell TUI mapping") < rendered.index("agent-pyl4xz6a")


def test_task_output_header_resolves_name_while_running():
    """With a registered resolver the friendly name shows even while the task is
    still running (no result yet) — not just after a result arrives."""
    from pythinker_code.ui.shell.tool_renderers.background import set_task_label_resolver

    set_task_label_resolver(lambda tid: "src-mapper" if tid == "agent-7ofm18ub" else None)
    try:
        rendered = _render_running(
            "TaskOutput", {"task_id": "agent-7ofm18ub", "block": True}, width=80
        )
    finally:
        set_task_label_resolver(None)
    assert "src-mapper" in rendered
    assert rendered.index("src-mapper") < rendered.index("agent-7ofm18ub")


def test_generic_substantial_output_uses_single_response_gutter():
    rendered = _render_with_definition(
        "UnknownTool",
        {"path": "x"},
        output="line1\nline2",
    )
    assert "⎿  line1" in rendered
    assert "⎿    ⎿" not in rendered


def test_generic_renderer_summarizes_arg_keys_without_values():
    rendered = _render_with_definition(
        "UnknownTool",
        {
            "content": "SECRET PAYLOAD",
            "newText": "new secret",
            "oldText": "old secret",
            "path": "src/config.py",
            "token": "sk-secret",
        },
    )

    assert "UnknownTool(5 args:" in rendered
    assert "content" in rendered
    assert "path" in rendered
    assert "SECRET PAYLOAD" not in rendered
    assert "new secret" not in rendered
    assert "old secret" not in rendered
    assert "src/config.py" not in rendered
    assert "sk-secret" not in rendered


# ---------------------------------------------------------------------------
# ReadSkill
# ---------------------------------------------------------------------------


def test_read_skill_renders_as_skill_with_name_only():
    rendered = _render(
        "ReadSkill",
        {"skill_name": "review-pr"},
        output="skill: review-pr\npath: /repo/skills/review-pr/SKILL.md\n\n# Review PR",
    )

    assert "⏺ Skill(review-pr)" in rendered
    assert "ReadSkill" not in rendered
    assert "arg:" not in rendered
    assert "# Review PR" in rendered


# ---------------------------------------------------------------------------
# diff component
# ---------------------------------------------------------------------------


def test_compute_edit_diff_string_basic():
    result = compute_edit_diff_string("a\nb\nc\n", "a\nB\nc\n")
    assert "-" in result.diff
    assert "+" in result.diff
    assert result.first_changed_line == 2


def test_render_diff_colorizes_added_removed():
    diff = compute_edit_diff_string("hello\n", "world\n").diff
    plain = render_plain(render_diff(diff), width=60)
    assert "hello" in plain
    assert "world" in plain


def test_render_diff_signs_match_body_foreground():
    """+/- markers and line numbers use default fg on tinted rows, not green/red."""
    from pythinker_code.ui.theme import get_diff_colors, set_active_theme, tui_rich_style

    set_active_theme("dark")
    diff = compute_edit_diff_string("old line\n", "new line\n").diff
    text = render_diff(diff)
    accent_fgs = {
        tui_rich_style("tool_diff_added").color,
        tui_rich_style("tool_diff_removed").color,
    }
    row_bgs = {get_diff_colors().add_bg.bgcolor, get_diff_colors().del_bg.bgcolor}
    for span in text.spans:
        if span.end <= span.start:
            continue
        style = span.style
        if isinstance(style, str):
            continue
        if style.color in accent_fgs:
            pytest.fail(f"diff sign/body used accent fg {style.color!r} on {text.plain[span.start:span.end]!r}")

    tinted = [
        text.plain[span.start : span.end]
        for span in text.spans
        if not isinstance(span.style, str) and span.style.bgcolor in row_bgs
    ]
    assert any(" -" in chunk or chunk.endswith("-") for chunk in tinted)
    assert any(" +" in chunk or chunk.endswith("+") for chunk in tinted)


# ---------------------------------------------------------------------------
# Agent (subagent)
# ---------------------------------------------------------------------------


def test_agent_renders_type_and_description_without_prompt_preview():
    rendered = _render(
        "Agent",
        {
            "subagent_type": "code-architect",
            "description": "design auth flow",
            "prompt": "Design the OAuth flow with PKCE\nAdditional context...",
        },
        output="Plan ready",
    )
    assert "⏺ Agent(" in rendered
    assert "code-architect" in rendered
    assert "design auth flow" in rendered
    assert "Prompt: Design the OAuth flow with PKCE" not in rendered
    assert "Plan ready" in rendered


def test_invalid_empty_agent_call_names_missing_required_fields():
    rendered = _render(
        "Agent",
        {},
        output=(
            "Error validating JSON arguments: 2 validation errors for Params\n"
            "description\n  Field required\n"
            "prompt\n  Field required"
        ),
        is_error=True,
    )
    assert "<missing description>" in rendered
    assert "<missing prompt>" in rendered


# ---------------------------------------------------------------------------
# RunAgents
# ---------------------------------------------------------------------------


def test_run_agents_renders_compact_professional_summary():
    rendered = _render(
        "RunAgents",
        {
            "summary": "Run code and security scans on current diff",
            "base_prompt": "Repository details that should not be echoed in the terminal",
            "run_in_background": False,
            "agents": [
                {
                    "name": "code_scan",
                    "prompt": "Review every changed file and return detailed findings",
                    "subagent_type": "code-reviewer",
                },
                {
                    "name": "security_scan",
                    "prompt": "Review for security issues only",
                    "subagent_type": "security-reviewer",
                },
            ],
        },
        output=(
            "tool_status: success\n"
            "orchestration_approval: requested\n"
            "orchestration_fingerprint: 9ab49da6d522\n"
            "summary: Run code and security scans on current diff\n"
            "mode: foreground\n"
            "agent_count: 2\n"
            "agents:\n"
            "- name: code_scan\n"
            "  subagent_type: code-reviewer\n"
            "  status: completed\n"
            "  result: |\n"
            "    agent_id: ab1ad32a2\n"
            "    resumed: false\n"
            "    actual_subagent_type: code-reviewer\n"
            "    status: completed\n"
            "\n"
            "    [summary]\n"
            "    No correctness findings above the configured threshold.\n"
            "- name: security_scan\n"
            "  subagent_type: security-reviewer\n"
            "  status: completed\n"
            "  result: |\n"
            "    agent_id: ac9ed41ff\n"
            "    resumed: false\n"
            "    actual_subagent_type: security-reviewer\n"
            "    status: completed\n"
            "\n"
            "    [summary]\n"
            "    No exploitable security issues were found."
        ),
        width=120,
    )
    assert "⏺ RunAgents(" in rendered
    assert "2 agents" in rendered
    assert "foreground" in rendered
    assert "code_scan" in rendered
    assert "security_scan" in rendered
    # Successful agent summaries are suppressed — only the findings table shows
    assert "No correctness findings" not in rendered
    assert "No exploitable security issues" not in rendered
    # The findings panel appears for review runs
    assert "Review Findings" in rendered
    assert "Repository details" not in rendered
    assert "Review every changed file" not in rendered
    assert "result: |" not in rendered
    assert "agent_id:" not in rendered


def test_run_agents_rows_align_columns_and_drop_redundant_name():
    rendered = _render(
        "RunAgents",
        {
            "summary": "Parallel deep review",
            "run_in_background": True,
            "agents": [
                {"name": "code-reviewer", "subagent_type": "code-reviewer"},
                {"name": "qa", "subagent_type": "qa"},
            ],
        },
        output=(
            "tool_status: success\n"
            "mode: background\n"
            "agent_count: 2\n"
            "agents:\n"
            "- name: code-reviewer\n"
            "  subagent_type: code-reviewer\n"
            "  status: running\n"
            "  task_id: agent-aaaa\n"
            "- name: qa\n"
            "  subagent_type: qa\n"
            "  status: running\n"
            "  task_id: agent-bbbb\n"
        ),
        width=120,
    )
    tree_lines = [line for line in rendered.splitlines() if line.lstrip().startswith(("├─", "└─"))]
    assert len(tree_lines) == 2
    # Variable-width subagent labels are padded so the status column aligns.
    status_cols = {line.index("running") for line in tree_lines}
    assert len(status_cols) == 1, tree_lines
    # And the trailing task_id column aligns too.
    task_cols = {line.index("agent-") for line in tree_lines}
    assert len(task_cols) == 1, tree_lines
    # A name identical to the subagent_type is not echoed twice in its tree row.
    code_reviewer_line = next(line for line in tree_lines if "code-reviewer" in line)
    assert code_reviewer_line.count("code-reviewer") == 1


# ---------------------------------------------------------------------------
# AskUserQuestion
# ---------------------------------------------------------------------------


def test_ask_user_renders_question_and_options():
    rendered = _render(
        "AskUserQuestion",
        {
            "questions": [
                {
                    "question": "Which auth method?",
                    "options": [
                        {"label": "OAuth"},
                        {"label": "API key"},
                    ],
                }
            ]
        },
    )
    assert "⏺ Ask(1 question)" in rendered
    assert f"⏺ Ask(1 question)\n\n{QUESTION_MARKER} Which auth method?" in rendered
    assert "OAuth" in rendered
    assert "API key" in rendered


def test_ask_user_streaming_empty_questions_waits_instead_of_showing_invalid():
    rendered = _render_streaming("AskUserQuestion", {"questions": []})

    assert "Preparing Ask…" in rendered
    assert "<invalid>" not in rendered


def test_ask_user_renders_single_question_object_without_invalid_badge():
    rendered = _render(
        "AskUserQuestion",
        {
            "questions": {
                "question": "How should independent analyses run?",
                "options": [
                    {"label": "Run concurrently (Recommended)"},
                    {"label": "Run sequentially"},
                ],
            }
        },
    )

    assert "⏺ Ask(1 question)" in rendered
    assert "<invalid>" not in rendered
    assert "How should independent analyses run?" in rendered
    assert "Run concurrently (Recommended)" in rendered


# ---------------------------------------------------------------------------
# Think
# ---------------------------------------------------------------------------


def test_think_renders_thought_body():
    rendered = _render("Think", {"thought": "First, check the file layout.\nThen draft a fix."})
    assert "⏺ Think" in rendered
    assert "First, check the file layout." in rendered


# ---------------------------------------------------------------------------
# SetTodoList
# ---------------------------------------------------------------------------


def test_todo_renders_status_icons_and_counts():
    rendered = _render(
        "SetTodoList",
        {
            "todos": [
                {"title": "Write spec", "status": "done"},
                {"title": "Implement", "status": "in_progress"},
                {"title": "Test", "status": "pending"},
            ]
        },
    )
    assert "todos" in rendered
    assert "1/3 done" in rendered
    assert "1 active" in rendered
    assert "1 pending" in rendered
    assert "├─" in rendered
    assert "└─" in rendered
    assert "Write spec" in rendered
    assert "Implement" in rendered
    assert "Test" in rendered


def test_todo_infers_nested_items_from_leading_spaces():
    rendered = _render(
        "SetTodoList",
        {
            "todos": [
                {"title": "Parent", "status": "in_progress"},
                {"title": "  Child", "status": "pending"},
            ]
        },
    )
    assert "├─" in rendered
    assert "  └─" in rendered
    assert "Child" in rendered


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------


def test_fetch_renders_url():
    rendered = _render("FetchURL", {"url": "https://example.com/page"}, output="<html>...")
    assert "⏺ Fetch(" in rendered
    assert "example.com" in rendered
    assert "Received 9 bytes" in rendered


def test_search_renders_query_and_extras():
    rendered = _render(
        "SearchWeb",
        {"query": "python typing", "limit": 10, "include_content": True},
        output="result 1",
    )
    assert "⏺ WebSearch(" in rendered
    assert "python typing" in rendered
    assert "limit 10" in rendered
    assert "with content" in rendered
    assert "Found 1 result" in rendered


def test_search_counts_structured_result_blocks():
    rendered = _render(
        "SearchWeb",
        {"query": "python"},
        output=(
            "Title: One\nDate: \nURL: https://example.com/1\nSummary: A\n\n"
            "---\n\n"
            "Title: Two\nDate: \nURL: https://example.com/2\nSummary: B\n\n"
        ),
    )
    assert "Found 2 results" in rendered


def test_search_shows_allowlist_filtered_indicator():
    rendered = _render(
        "SearchWeb",
        {"query": "python"},
        output="Title: One\nDate: \nURL: https://example.com/1\nSummary: A\n\n",
        details={"extras": {"allowlist_filtered": 2}},
    )
    assert "Found 1 result" in rendered
    assert "2 filtered to allowlist" in rendered


def test_search_no_allowlist_indicator_when_not_filtered():
    rendered = _render(
        "SearchWeb",
        {"query": "python"},
        output="Title: One\nDate: \nURL: https://example.com/1\nSummary: A\n\n",
    )
    assert "filtered to allowlist" not in rendered


def test_search_all_results_filtered_reports_zero():
    # When every result is dropped by the allowlist, SearchWeb emits prose plus a
    # structured returned_results=0 signal; the renderer must prefer that count
    # instead of misreading the one-line prose as a single result.
    rendered = _render(
        "SearchWeb",
        {"query": "python"},
        output=(
            "All 2 search result(s) were outside the configured web allowlist "
            "and have been omitted."
        ),
        details={"extras": {"allowlist_filtered": 2, "returned_results": 0}},
    )
    assert "Found 0 results" in rendered
    assert "2 filtered to allowlist" in rendered


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------


def test_task_list_renders_active_flag():
    rendered = _render("TaskList", {"active_only": True}, output="task-1: running")
    assert "⏺ Tasks(active)" in rendered


def test_task_output_renders_id_and_block_flag():
    rendered = _render(
        "TaskOutput",
        {"task_id": "abc-123", "block": True, "timeout": 60},
        output="logs...",
    )
    assert "⏺ TaskOutput(" in rendered
    assert "abc-123" in rendered
    assert "block" in rendered


def test_task_stop_renders_id():
    rendered = _render("TaskStop", {"task_id": "abc-123", "reason": "user requested"})
    assert "⏺ TaskStop(" in rendered
    assert "abc-123" in rendered


# ---------------------------------------------------------------------------
# Plan tools
# ---------------------------------------------------------------------------


def test_enter_plan_mode_renders():
    rendered = _render("EnterPlanMode", {})
    assert "⏺ Plan(entering)" in rendered


def test_exit_plan_mode_renders_options():
    rendered = _render(
        "ExitPlanMode",
        {
            "options": [
                {"label": "Refactor first"},
                {"label": "Add tests first"},
            ]
        },
    )
    assert "⏺ Plan(exiting)" in rendered
    assert "Refactor first" in rendered
    assert "Add tests first" in rendered


# ---------------------------------------------------------------------------
# spacing
# ---------------------------------------------------------------------------


def test_card_renders_compact_without_outer_padding():
    """Compact tool cards should start at the title and avoid extra outer padding."""
    rendered = _render("Glob", {"pattern": "*.py", "directory": "/repo"}, output="foo.py")
    lines = [line.strip() for line in rendered.splitlines()]
    assert lines[0] == "⏺ Find(*.py in /repo)"
    assert lines[-1] == "⎿  Found 1 file ctrl+o expand"


def test_card_places_result_immediately_under_response_gutter():
    """Tool output should follow the header immediately under the response gutter."""
    rendered = _render("Glob", {"pattern": "*.py", "directory": "/repo"}, output="foo.py\nbar.py")
    lines = [line.strip() for line in rendered.splitlines()]
    header_idx = next(
        (i for i, line in enumerate(lines) if "Find" in line and "*.py" in line), None
    )
    assert header_idx is not None, "header line not found in rendered output"
    assert lines[header_idx + 1].startswith("⎿  Found 2 files"), (
        f"expected response gutter after header at index {header_idx}, "
        f"got {lines[header_idx + 1]!r}"
    )


# ---------------------------------------------------------------------------
# RunAgents — findings table aggregation
# ---------------------------------------------------------------------------


def _run_agents_review_output(agents_yaml: str) -> str:
    return (
        "tool_status: success\n"
        "mode: foreground\n"
        f"agent_count: {agents_yaml.count('- name:')}\n"
        "agents:\n" + agents_yaml
    )


def test_findings_table_explicit_bracket_counts():
    """[HIGH] / [MEDIUM] bullets are parsed and appear in the findings table."""
    output = _run_agents_review_output(
        "- name: code_scan\n"
        "  subagent_type: code-reviewer\n"
        "  status: completed\n"
        "  result: |\n"
        "    status: completed\n"
        "\n"
        "    ## Findings\n"
        "    - [HIGH] Missing input validation\n"
        "    - [HIGH] SQL injection risk\n"
        "    - [MEDIUM] Weak error handling\n"
    )
    rendered = _render("RunAgents", {"summary": "review"}, output=output, width=120)
    assert "Review Findings" in rendered
    assert "High" in rendered
    assert "Medium" in rendered
    # Footer should say parsed 1/1
    assert "Parsed 1/1" in rendered


def test_findings_table_severity_section_bullets():
    """Plain bullets inside a ### High Severity subsection are counted as high."""
    output = _run_agents_review_output(
        "- name: sec_scan\n"
        "  subagent_type: security-reviewer\n"
        "  status: completed\n"
        "  result: |\n"
        "    status: completed\n"
        "\n"
        "    ## Findings\n"
        "    ### High Severity\n"
        "    - Token reuse vulnerability\n"
        "    - Missing TLS enforcement\n"
        "    ### Low Severity\n"
        "    - Unused debug flag\n"
    )
    rendered = _render("RunAgents", {"summary": "security review"}, output=output, width=120)
    assert "Review Findings" in rendered
    assert "High" in rendered
    assert "Low" in rendered
    assert "Parsed 1/1" in rendered


def test_findings_table_unstructured_prose_renders_as_unparsed():
    """A reviewer with only prose (no structured markers) shows the panel with unparsed count."""
    output = _run_agents_review_output(
        "- name: code_scan\n"
        "  subagent_type: code-reviewer\n"
        "  status: completed\n"
        "  result: |\n"
        "    status: completed\n"
        "\n"
        "    No correctness findings above the configured threshold.\n"
    )
    rendered = _render("RunAgents", {"summary": "review"}, output=output, width=120)
    # No structured markers → unparsed prose, not zero-count parsed findings
    assert "Review Findings" in rendered
    assert "unparsed prose" in rendered


def test_findings_table_ambiguous_prose_not_counted():
    """Mid-sentence severity words ('not a high-risk change') must not be counted."""
    output = _run_agents_review_output(
        "- name: code_scan\n"
        "  subagent_type: code-reviewer\n"
        "  status: completed\n"
        "  result: |\n"
        "    status: completed\n"
        "\n"
        "    This is not a high-risk change.\n"
        "    The overall risk is medium at most.\n"
        "    No critical vulnerabilities detected in this diff.\n"
    )
    rendered = _render("RunAgents", {"summary": "review"}, output=output, width=120)
    assert "Review Findings" in rendered
    # Whole report is unparsed prose — no structured findings found
    assert "unparsed prose" in rendered


def test_findings_table_not_rendered_for_non_review_run():
    """Non-review RunAgents output must not include the findings panel."""
    output = _run_agents_review_output(
        "- name: implementer\n"
        "  subagent_type: implementer\n"
        "  status: completed\n"
        "  result: |\n"
        "    status: completed\n"
        "\n"
        "    Implementation complete.\n"
    )
    rendered = _render("RunAgents", {"summary": "implement"}, output=output, width=120)
    assert "Review Findings" not in rendered


def test_findings_table_reported_by_shows_agent_names():
    """The 'Reported by' column lists the agent name for each severity."""
    output = _run_agents_review_output(
        "- name: auth_review\n"
        "  subagent_type: code-reviewer\n"
        "  status: completed\n"
        "  result: |\n"
        "    status: completed\n"
        "\n"
        "    ## Findings\n"
        "    - [CRITICAL] Auth bypass\n"
        "- name: api_review\n"
        "  subagent_type: security-reviewer\n"
        "  status: completed\n"
        "  result: |\n"
        "    status: completed\n"
        "\n"
        "    ## Findings\n"
        "    - [HIGH] SSRF vulnerability\n"
        "    - [HIGH] Missing CSRF\n"
    )
    rendered = _render("RunAgents", {"summary": "dual review"}, output=output, width=120)
    assert "Review Findings" in rendered
    assert "auth_review" in rendered
    assert "api_review" in rendered
    assert "Parsed 2/2" in rendered


def test_findings_table_unparsed_count_and_parsed_ratio():
    """Agents with no structured markers are counted as unparsed in footer + Unknown row."""
    output = _run_agents_review_output(
        "- name: code_scan\n"
        "  subagent_type: code-reviewer\n"
        "  status: completed\n"
        "  result: |\n"
        "    status: completed\n"
        "\n"
        "    ## Findings\n"
        "    - [MEDIUM] Missing validation\n"
        "- name: prose_reviewer\n"
        "  subagent_type: code-reviewer\n"
        "  status: completed\n"
        "  result: |\n"
        "    status: completed\n"
        "\n"
        "    The code looks fine with no major concerns.\n"
    )
    rendered = _render("RunAgents", {"summary": "review"}, output=output, width=120)
    assert "Review Findings" in rendered
    assert "Parsed 1/2" in rendered
    assert "1 report kept as unparsed prose" in rendered
    assert "Unknown" in rendered


def test_successful_non_review_agent_shows_summary_preview():
    """Completed non-review agents with a summary_preview must show it as a preview line."""
    output = _run_agents_review_output(
        "- name: implementer\n"
        "  subagent_type: implementer\n"
        "  status: completed\n"
        "  result: |\n"
        "    status: completed\n"
        "\n"
        "    [summary]\n"
        "    Refactored the auth module and added unit tests.\n"
    )
    rendered = _render("RunAgents", {"summary": "implement feature"}, output=output, width=120)
    # The summary preview must appear for non-review successful agents.
    assert "Refactored the auth module" in rendered
    # The findings panel must NOT appear (no review agents).
    assert "Review Findings" not in rendered
