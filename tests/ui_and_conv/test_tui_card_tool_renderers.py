"""Smoke tests for the Pythinker ported tool renderers.

These tests render each builtin through ``ToolExecutionComponent`` and assert
the human-readable output contains the expected fragments. They are not
pixel-snapshots: tweaking exact spacing/styling is fine, but the substantive
information (tool name, path, key args, result preview, expansion hint) must
remain visible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from rich.text import Text

from pythinker_code.tools.display import DiffDisplayBlock
from pythinker_code.ui.shell.components import (
    ToolExecutionComponent,
    compute_edit_diff_string,
    render_diff,
    render_plain,
)
from pythinker_code.ui.shell.glyphs import QUESTION_MARKER
from pythinker_code.ui.shell.tool_renderers import (
    ToolRenderContext,
    ToolResultPayload,
    clear_tool_renderers,
    get_tool_renderer,
    register_builtin_renderers,
)
from pythinker_code.ui.shell.tool_renderers._file_diff import preview_from_diff_blocks
from pythinker_code.ui.shell.tool_renderers._render_utils import loading_marker
from pythinker_code.ui.shell.tool_renderers.generic import generic_renderer
from pythinker_code.ui.shell.tool_renderers.todo import (
    TODO_RENDERER,
    _has_cursor_todowrite_shape,
    _summarize_todo_validation_error,
    _todo_level_and_title,
)
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
# ReadMediaFile
# ---------------------------------------------------------------------------


def test_readmedia_renders_image_summary_from_message():
    raw_payload = (
        '<image path="/repo/assets/cat.png">'
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"
        "</image>"
    )
    rendered = _render(
        "ReadMediaFile",
        {"path": "/repo/assets/cat.png"},
        output=raw_payload,
        details={
            "message": (
                "Loaded image file `/repo/assets/cat.png` "
                "(image/png, 2048 bytes, original size 640x480px)."
            )
        },
    )

    assert "⏺ ReadMedia(" in rendered
    assert "assets/cat.png" in rendered
    assert "Read image" in rendered
    assert "image/png" in rendered
    assert "2.0 KB" in rendered
    assert "640x480" in rendered
    assert "data:image/png;base64" not in rendered
    assert "iVBORw0KGgo" not in rendered


def test_readmedia_renders_video_summary_from_message():
    rendered = _render(
        "ReadMediaFile",
        {"path": "/repo/assets/clip.mp4"},
        output='<video path="/repo/assets/clip.mp4">data:video/mp4;base64,AAAA</video>',
        details={
            "message": "Loaded video file `/repo/assets/clip.mp4` (video/mp4, 1048576 bytes)."
        },
    )

    assert "⏺ ReadMedia(" in rendered
    assert "assets/clip.mp4" in rendered
    assert "Read video" in rendered
    assert "video/mp4" in rendered
    assert "1.0 MB" in rendered
    assert "data:video/mp4;base64" not in rendered


def test_readmedia_unsupported_text_file_preserves_error():
    rendered = _render(
        "ReadMediaFile",
        {"path": "/repo/notes.txt"},
        output="`/repo/notes.txt` is a text file. Use ReadFile to read text files.",
        is_error=True,
    )

    assert "⏺ ReadMedia(" not in rendered
    assert "✘ ReadMedia(" in rendered
    assert "`/repo/notes.txt` is a text file. Use ReadFile to read text files." in rendered
    assert "Read text" not in rendered


def test_readmedia_collapsed_output_suppresses_wrapped_base64_payload():
    raw_payload = (
        '<image path="/repo/assets/cat.png">\n'
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB\n"
        "</image>"
    )
    rendered = _render(
        "ReadMediaFile",
        {"path": "/repo/assets/cat.png"},
        output=raw_payload,
    )

    assert "Read image" in rendered
    assert "data:image/png;base64" not in rendered
    assert "iVBORw0KGgo" not in rendered
    assert "<image" not in rendered


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
    assert " 2 + new section" in rendered
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
    assert " 1 - return 1" in rendered
    assert " 1 + return 2" in rendered


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
    assert "41 - old" in rendered
    assert "41 + new" in rendered


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
# SmartSearch
# ---------------------------------------------------------------------------


def test_smartsearch_renders_query_path_and_structured_counts():
    rendered = _render(
        "SmartSearch",
        {"query": "renderer parity", "path": "/repo/src"},
        output="## exact\nsrc/ui/card.py:12:renderer parity",
        details={
            "extras": {
                "result_count": 4,
                "file_count": 2,
                "line_count": 4,
                "returned_results": 4,
            }
        },
    )

    assert "⏺ SmartSearch(" in rendered
    assert "renderer parity" in rendered
    assert "src" in rendered
    assert "Found 4 lines across 2 files" in rendered
    assert "src/ui/card.py" not in rendered


def test_smartsearch_renders_query_without_path():
    rendered = _render(
        "SmartSearch",
        {"query": "ToolExecutionComponent"},
        output="No matches found across smart search passes.",
    )

    assert "ToolExecutionComponent" in rendered
    assert " in ." not in rendered
    assert "No matches found across smart search passes." in rendered


def test_smartsearch_short_collapsed_result_that_hides_text_is_expandable():
    defn = get_tool_renderer("SmartSearch")
    assert defn is not None
    comp = ToolExecutionComponent("SmartSearch", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"query": "renderer parity", "path": "/repo"})
    comp.set_args_complete()
    comp.mark_execution_started()
    comp.set_result(ToolResultPayload(text="src/ui/card.py:12:renderer parity"))

    collapsed = render_plain(comp.render(), width=120)

    assert "Found 1 line across 1 file" in collapsed
    assert "src/ui/card.py" not in collapsed
    assert comp.can_expand


def test_smartsearch_expanded_shows_bounded_sectioned_text():
    output = "\n".join(
        [
            "## exact",
            "src/a.py:1:renderer parity",
            "src/a.py:2:renderer parity",
            "",
            "## any term",
            "src/b.py:3:smart search renderer",
            "src/c.py:4:smart search renderer",
            "src/d.py:5:smart search renderer",
            "src/e.py:6:smart search renderer",
            "src/f.py:7:smart search renderer",
            "src/g.py:8:smart search renderer",
            "src/h.py:9:smart search renderer",
            "src/i.py:10:smart search renderer",
            "src/j.py:11:smart search renderer",
            "src/k.py:12:smart search renderer",
            "src/l.py:13:smart search renderer",
            "src/m.py:14:smart search renderer",
            "src/n.py:15:smart search renderer",
            "src/o.py:16:smart search renderer",
            "src/p.py:17:smart search renderer",
        ]
    )

    rendered = _render(
        "SmartSearch",
        {"query": "renderer parity", "path": "/repo"},
        output=output,
        expanded=True,
    )

    assert "Found 17 lines across 16 files" in rendered
    assert "## exact" in rendered
    assert "src/a.py:1:renderer parity" in rendered
    assert "## any term" in rendered
    assert "src/p.py:17:smart search renderer" not in rendered
    assert "more lines" in rendered


def test_smartsearch_error_preserves_tool_text():
    rendered = _render(
        "SmartSearch",
        {"query": "renderer parity", "path": "/outside"},
        output="`/outside` is outside the workspace.",
        is_error=True,
    )

    assert "Error searching files" in rendered
    assert "`/outside` is outside the workspace." in rendered


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
        ("ToolSearch", {"query": "read"}, "Tools("),
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


def _render_diff_text(*args: object, **kwargs: object) -> Text:
    """Flatten :func:`render_diff` grid output into ``Text`` for span assertions."""
    from rich.console import Console
    from rich.text import Text

    renderable = render_diff(*args, **kwargs)  # type: ignore[arg-type]
    console = Console(
        width=120,
        record=True,
        force_terminal=True,
        _environ={"TERM": "xterm-256color"},
    )
    segments = list(console.render(renderable, console.options.update_width(120)))
    text = Text()
    for segment in segments:
        if segment.control:
            if segment.text in {"\n", "\r\n"} and (not text.plain or not text.plain.endswith("\n")):
                text.append("\n")
            continue
        text.append(segment.text, style=segment.style or "")
    return text


def _render_diff_with_gutter(*args, width: int = 70, **kwargs) -> str:
    """Render a tool-card-shaped diff (gutter + body) at *width*."""
    from pythinker_code.ui.shell.components.render_utils import render_message_response

    return render_plain(render_message_response(render_diff(*args, **kwargs)), width=width)


def _assert_wrap_fragment_aligned(output: str, fragment: str) -> None:
    """Wrap fragments must not start a line (orphan at column 0)."""
    for line in output.splitlines():
        if fragment not in line:
            continue
        assert not line.lstrip().startswith(fragment), (
            f"wrap fragment {fragment!r} orphaned at column 0: {line!r}"
        )
        return
    raise AssertionError(f"fragment {fragment!r} not found in diff output")


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


def test_render_diff_spaces_marker_before_at_rule():
    old = "@keyframes drawer-fade-in { from { opacity: 0; } to { opacity: 1); } }\n"
    new = "@keyframes drawer-fade-in { from { opacity: 0; } to { opacity: 1; } }\n"
    diff = compute_edit_diff_string(old, new).diff
    plain = render_plain(render_diff(diff), width=120)
    assert " - @keyframes" in plain
    assert " + @keyframes" in plain
    assert "-@" not in plain
    assert "+@" not in plain


def test_render_diff_signs_match_body_foreground():
    """+/- markers and line numbers use default fg on tinted rows, not green/red."""
    from pythinker_code.ui.theme import get_diff_colors, set_active_theme, tui_rich_style

    set_active_theme("dark")
    diff = compute_edit_diff_string("old line\n", "new line\n").diff
    text = _render_diff_text(diff)
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
            pytest.fail(
                f"diff sign/body used accent fg {style.color!r} on {text.plain[span.start : span.end]!r}"
            )

    tinted = [
        text.plain[span.start : span.end]
        for span in text.spans
        if not isinstance(span.style, str) and span.style.bgcolor in row_bgs
    ]
    assert any(" -" in chunk or chunk.endswith("-") for chunk in tinted)
    assert any(" +" in chunk or chunk.endswith("+") for chunk in tinted)


def test_render_diff_syntax_highlights_python_when_path_given():
    import re

    from rich.console import Console

    from pythinker_code.utils.rich.syntax import (
        CATPPUCCIN_ADAPTIVE_THEME_NAME,
        set_active_code_theme,
    )

    set_active_code_theme(CATPPUCCIN_ADAPTIVE_THEME_NAME)
    diff = compute_edit_diff_string(
        "def old():\n    pass\n",
        "def new():\n    pass\n",
    ).diff

    def _ansi(text) -> str:
        console = Console(width=120, record=True, force_terminal=True)
        console.print(text)
        return console.export_text(styles=True)

    highlighted = _ansi(render_diff(diff, path="module.py"))
    plain = _ansi(render_diff(diff))
    style_seqs = len(re.findall(r"\x1b\[[^m]*m", highlighted))
    plain_seqs = len(re.findall(r"\x1b\[[^m]*m", plain))
    assert style_seqs > plain_seqs


def test_render_diff_without_path_stays_plain_foreground():
    from rich.console import Console

    diff = compute_edit_diff_string("def old():\n", "def new():\n").diff
    console = Console(width=120, record=True, force_terminal=True)
    console.print(render_diff(diff))
    ansi = console.export_text(styles=True)
    assert "38;2;" not in ansi


def test_render_diff_without_path_does_not_construct_highlighter(monkeypatch):
    from pythinker_code.ui.shell.components import diff as diff_component

    def _boom(_path: str):
        raise AssertionError("make_diff_highlighter must not run when path is omitted")

    monkeypatch.setattr(diff_component, "make_diff_highlighter", _boom)
    diff = compute_edit_diff_string("a\n", "b\n").diff
    render_diff(diff)


def _spans_covering(text, start: int, end: int):
    return [span for span in text.spans if span.start < end and span.end > start]


def test_render_diff_syntax_highlights_context_lines_when_path_given():
    from pythinker_code.ui.theme import get_diff_colors, set_active_theme
    from pythinker_code.utils.rich.syntax import (
        CATPPUCCIN_ADAPTIVE_THEME_NAME,
        set_active_code_theme,
    )

    set_active_theme("dark")
    set_active_code_theme(CATPPUCCIN_ADAPTIVE_THEME_NAME)
    diff = compute_edit_diff_string(
        "line one\nunchanged ctx\n",
        "line ONE\nunchanged ctx\n",
    ).diff
    text = _render_diff_text(diff, path="module.py")
    needle = "unchanged ctx"
    start = text.plain.index(needle)
    end = start + len(needle)
    row_bgs = {get_diff_colors().add_bg.bgcolor, get_diff_colors().del_bg.bgcolor}
    overlapping = _spans_covering(text, start, end)
    assert overlapping
    assert any(
        not isinstance(span.style, str) and span.style.color and span.style.bgcolor not in row_bgs
        for span in overlapping
    )


def test_render_diff_inline_pair_preserves_syntax_foreground_and_add_hl():
    from pythinker_code.ui.theme import get_diff_colors, set_active_theme
    from pythinker_code.utils.rich.syntax import (
        CATPPUCCIN_ADAPTIVE_THEME_NAME,
        set_active_code_theme,
    )

    set_active_theme("dark")
    set_active_code_theme(CATPPUCCIN_ADAPTIVE_THEME_NAME)
    old = "async def run_old(value: str) -> None:\n"
    new = "async def run_new(value: str) -> None:\n"
    diff = compute_edit_diff_string(old, new).diff
    text = _render_diff_text(diff, path="module.py")
    colors = get_diff_colors()

    minus_start = text.plain.index("run_old")
    minus_spans = _spans_covering(text, minus_start, minus_start + len("run_old"))
    assert any(
        not isinstance(span.style, str) and span.style.bgcolor == colors.del_hl.bgcolor
        for span in minus_spans
    )

    plus_start = text.plain.index("run_new")
    plus_spans = _spans_covering(text, plus_start, plus_start + len("run_new"))
    assert any(
        not isinstance(span.style, str) and span.style.bgcolor == colors.add_hl.bgcolor
        for span in plus_spans
    )

    async_start = text.plain.index("async")
    async_spans = _spans_covering(text, async_start, async_start + len("async"))
    assert any(
        not isinstance(span.style, str) and span.style.color and span.style.color != "default"
        for span in async_spans
    )


def test_render_diff_tabbed_inline_pair_maps_highlight_offsets():
    from pythinker_code.ui.theme import get_diff_colors, set_active_theme
    from pythinker_code.utils.rich.syntax import (
        CATPPUCCIN_ADAPTIVE_THEME_NAME,
        set_active_code_theme,
    )

    set_active_theme("dark")
    set_active_code_theme(CATPPUCCIN_ADAPTIVE_THEME_NAME)
    diff = compute_edit_diff_string("if\told_name:\n", "if\tnew_name:\n").diff
    text = _render_diff_text(diff, path="module.py")
    colors = get_diff_colors()

    old_start = text.plain.index("old_name")
    old_spans = _spans_covering(text, old_start, old_start + len("old_name"))
    old_highlighted = "".join(
        text.plain[span.start : span.end]
        for span in old_spans
        if not isinstance(span.style, str) and span.style.bgcolor == colors.del_hl.bgcolor
    )
    assert "old" in old_highlighted

    new_start = text.plain.index("new_name")
    new_spans = _spans_covering(text, new_start, new_start + len("new_name"))
    new_highlighted = "".join(
        text.plain[span.start : span.end]
        for span in new_spans
        if not isinstance(span.style, str) and span.style.bgcolor == colors.add_hl.bgcolor
    )
    assert "new" in new_highlighted


def test_render_diff_unknown_extension_falls_back_to_text_without_crash():
    diff = compute_edit_diff_string("alpha beta\n", "alpha delta\n").diff
    text = _render_diff_text(diff, path="Makefile")
    assert "alpha" in text.plain
    assert "beta" in text.plain
    assert "delta" in text.plain


def test_render_diff_colors_disabled_does_not_emit_background_styles(monkeypatch):
    from pythinker_code.ui.theme import get_diff_colors

    monkeypatch.setenv("NO_COLOR", "1")
    diff = compute_edit_diff_string("old line\n", "new line\n").diff
    text = _render_diff_text(diff, path="module.py")
    colors = get_diff_colors()
    diff_bgs = {
        colors.add_bg.bgcolor,
        colors.del_bg.bgcolor,
        colors.add_hl.bgcolor,
        colors.del_hl.bgcolor,
    }
    diff_bgs = {bg for bg in diff_bgs if bg is not None}
    for span in text.spans:
        if isinstance(span.style, str):
            continue
        if span.style.bgcolor is not None:
            assert span.style.bgcolor not in diff_bgs


def test_render_diff_wraps_removed_line_under_code_column():
    old = (
        '    monkeypatch.setattr(_interactive_mod, "run_in_terminal",\n'
        "        lambda *args, **kwargs: printed.extend(args) if args else None,\n"
        "    )"
    )
    new = '    monkeypatch.setattr(_live_view_mod.console, "print",\n        _record_print)\n    )'
    diff = compute_edit_diff_string(old, new, old_start=935, new_start=935).diff
    output = _render_diff_with_gutter(diff, width=70)
    _assert_wrap_fragment_aligned(output, "else None,")
    assert "935" in output
    assert "- lambda" in output.replace("\n", " ") or "-         lambda" in output


def test_render_diff_wraps_added_line_under_code_column():
    old = "    pass\n"
    new = "            # Always invalidate when the caller explicitly asked for a forced refresh\n"
    diff = compute_edit_diff_string(old, new, old_start=840, new_start=840).diff
    output = _render_diff_with_gutter(diff, width=65)
    _assert_wrap_fragment_aligned(output, "forced refresh")


def test_render_diff_wraps_context_line_under_code_column():
    long_line = (
        "where the wire is shut down before the batch window closes so the caller can dispatch"
    )
    old = f"before\n{long_line}\nafter old\n"
    new = f"before\n{long_line}\nafter new\n"
    diff = compute_edit_diff_string(old, new, old_start=10, new_start=10).diff
    output = _render_diff_with_gutter(diff, width=60)
    _assert_wrap_fragment_aligned(output, "can dispatch")


def test_render_diff_wraps_syntax_highlighted_line_under_code_column():
    long_line = (
        '    print(f"DEBUG emit_scrollback_block called type={type(block).__name__}", '
        "file=sys.stdout, flush=True)"
    )
    old = f"{long_line}\n"
    new = '    print("ok")\n'
    diff = compute_edit_diff_string(old, new, old_start=81, new_start=81).diff
    output = _render_diff_with_gutter(diff, path="module.py", width=55)
    _assert_wrap_fragment_aligned(output, "block).__name__")


def test_render_diff_wrap_continuation_repeats_sign_marker():
    old = (
        '    print(f"DEBUG emit_scrollback_block called type={type(block).__name__}", '
        "file=sys.stdout, flush=True)\n"
    )
    new = '    print("ok")\n'
    diff = compute_edit_diff_string(old, new, old_start=81, new_start=81).diff
    output = _render_diff_with_gutter(diff, width=50)
    continuation_lines = [
        line
        for line in output.splitlines()
        if ("type=" in line or "block)" in line or "file=sys" in line)
        and line.lstrip().startswith("-")
    ]
    assert len(continuation_lines) >= 2
    assert all(line.lstrip().startswith("-") for line in continuation_lines[1:])


def test_make_diff_highlighter_caches_by_lexer_and_theme():
    from pythinker_code.utils.rich.diff_render import (
        _cached_diff_highlighter,
        make_diff_highlighter,
    )
    from pythinker_code.utils.rich.syntax import (
        CATPPUCCIN_ADAPTIVE_THEME_NAME,
        set_active_code_theme,
    )

    _cached_diff_highlighter.cache_clear()
    set_active_code_theme(CATPPUCCIN_ADAPTIVE_THEME_NAME)
    first = make_diff_highlighter("a.py")
    second = make_diff_highlighter("b.py")
    third = make_diff_highlighter("nested/c.py")
    assert first is second is third


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
    assert "Run code and security scans" in rendered
    assert "code-reviewer" in rendered
    assert "security-reviewer" in rendered
    assert "2 code-reviewer agents finished" in rendered or "2 agents finished" in rendered
    assert "Done" in rendered
    # Successful agent summaries are suppressed — only the findings table shows
    assert "No correctness findings" not in rendered
    assert "No exploitable security issues" not in rendered
    # The findings panel appears for review runs
    assert "Review Findings" in rendered
    assert "Repository details" not in rendered
    assert "Review every changed file" not in rendered
    assert "result: |" not in rendered
    assert "agent_id:" not in rendered
    assert "Mode" not in rendered


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
    assert "2 background agents launched" in rendered
    assert "qa" in rendered
    assert "code-reviewer" in rendered
    assert "Initializing" not in rendered
    assert "Mode" not in rendered


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


def test_todo_content_field_renders_labels_during_streaming():
    """Cursor-style payloads use ``content``; preview must still show labels."""
    rendered = _render_running(
        "SetTodoList",
        {
            "todos": [
                {"id": "1", "content": "Install framer-motion", "status": "in_progress"},
                {"id": "2", "content": "Create component", "status": "pending"},
            ]
        },
    )
    assert "Install framer-motion" in rendered
    assert "Create component" in rendered
    assert "├─" in rendered


def test_failed_cursor_todowrite_shape_renders_error_badge_not_tree():
    """``render_call`` must skip the plan tree when ``ctx.is_error`` is true."""
    ctx = ToolRenderContext(
        args={
            "todos": [
                {"id": "1", "content": "Install framer-motion", "status": "in_progress"},
                {"id": "2", "content": "Create background paths", "status": "pending"},
            ]
        },
        tool_call_id="tc-1",
        is_error=True,
        has_result=True,
        args_complete=True,
        expanded=False,
        execution_started=True,
    )
    render_call = TODO_RENDERER.render_call
    assert render_call is not None
    result = render_call(ctx)
    assert result is not None
    rendered = render_plain(result, width=100)
    assert "update failed · 2 items" in rendered
    assert "Install framer-motion" not in rendered
    assert "Create background paths" not in rendered
    assert "├─" not in rendered


def test_todo_validation_error_renders_compact_card_without_broken_tree():
    todos = [
        {"id": "1", "content": "Install framer-motion", "status": "in_progress"},
        {"id": "2", "content": "Create component", "status": "pending"},
    ]
    rendered = _render(
        "SetTodoList",
        {"todos": todos},
        output=(
            "Error validating JSON arguments: 2 validation errors for Params\n"
            "todos.0.title\n  Field required [type=missing, input_value={'id': '1', "
            "'content': 'Install framer-motion', 'status': 'in_progress'}, "
            "input_type=dict]\n"
            "todos.1.title\n  Field required [type=missing, input_value={'id': '2', "
            "'content': 'Create component', 'status': 'pending'}, input_type=dict]"
        ),
        is_error=True,
    )
    assert "✘ todos" in rendered
    assert "update failed · 2 items" in rendered
    assert "Todo update failed: each item needs `title`" in rendered
    assert "received `content` without `title`" in rendered
    assert "⎿" in rendered
    assert "├─" not in rendered
    assert "0/2 done" not in rendered
    assert "Install framer-motion" not in rendered
    assert "Field required" not in rendered


def test_todo_validation_error_five_items_no_fake_tree():
    """Regression: GLM run showed icons-only tree for five failed content-only items."""
    todos = [
        {"id": str(i), "content": f"Step {i}", "status": "in_progress" if i == 1 else "pending"}
        for i in range(1, 6)
    ]
    rendered = _render(
        "SetTodoList",
        {"todos": todos},
        output=(
            "Error validating JSON arguments: 5 validation errors for Params\n"
            + "\n".join(f"todos.{i}.title\n  Field required" for i in range(5))
        ),
        is_error=True,
    )
    assert "update failed · 5 items" in rendered
    assert "├─" not in rendered
    assert "Step 1" not in rendered


def test_todo_malformed_item_renders_untitled_label():
    rendered = _render_running(
        "SetTodoList",
        {"todos": [{"status": "pending"}]},
    )
    assert "Untitled todo" in rendered


def test_renderer_uses_content_fallback_for_title():
    _level, title = _todo_level_and_title(
        {"content": "Install framer-motion", "status": "in_progress"}
    )
    assert title == "Install framer-motion"


def test_renderer_uses_untitled_fallback():
    _level, title = _todo_level_and_title({"status": "pending"})
    assert title == "Untitled todo"


def test_renderer_cleans_multiline_title():
    _level, title = _todo_level_and_title({"title": "line one\nline two", "status": "pending"})
    assert title == "line one line two"


def test_validation_summary_detects_cursor_shape():
    args = {"todos": [{"id": "1", "content": "Install framer-motion", "status": "pending"}]}
    text = (
        "Error validating JSON arguments: 1 validation error for Params\n"
        "todos.0.title\n  Field required"
    )
    assert _summarize_todo_validation_error(text, args) == (
        "Todo update failed: each item needs `title` (received `content` without `title`)."
    )


def test_todo_validation_error_shows_full_detail_when_expanded():
    todos = [{"content": "Task A", "status": "pending"}]
    pydantic_text = (
        "Error validating JSON arguments: 1 validation error for Params\n"
        "todos.0.title\n  Field required"
    )
    defn = get_tool_renderer("SetTodoList")
    assert defn is not None
    comp = ToolExecutionComponent("SetTodoList", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"todos": todos})
    comp.set_args_complete()
    comp.mark_execution_started()
    comp.set_result(ToolResultPayload(text=pydantic_text, is_error=True))
    comp.set_expanded(True)
    rendered = render_plain(comp.render(), width=100)
    assert "Todo update failed: each item needs `title`" in rendered
    assert "Field required" in rendered


def test_cursor_shape_detects_blank_title_with_content():
    args = {"todos": [{"title": "", "content": "Install framer-motion", "status": "pending"}]}
    assert _has_cursor_todowrite_shape(args) is True


def test_renderer_blank_title_uses_content_fallback():
    _level, title = _todo_level_and_title(
        {"title": "", "content": "Install framer-motion", "status": "pending"}
    )
    assert title == "Install framer-motion"


def test_renderer_indent_ignores_ansi_before_leading_spaces():
    raw = "\x1b[31m  \x1b[0mNested task"
    _level, title = _todo_level_and_title({"title": raw, "status": "pending"})
    assert _level == 1
    assert title == "Nested task"


def test_malformed_todos_list_renders_invalid_when_args_complete():
    rendered = _render_running(
        "SetTodoList",
        {"todos": ["bad", None, 123, {"title": "ok", "status": "pending"}]},
    )
    assert "<invalid>" in rendered
    assert "ok" not in rendered


def test_malformed_todos_streaming_skips_non_dict_items():
    rendered = _render_streaming(
        "SetTodoList",
        {"todos": ["bad", {"title": "Visible", "status": "pending"}]},
    )
    assert "<invalid>" not in rendered
    assert "Visible" in rendered


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


def test_task_output_renders_summary_not_raw_metadata():
    rendered = _render(
        "TaskOutput",
        {"task_id": "agent-plucky-comet", "block": True, "timeout": 600},
        output=(
            "tool_status: success\n"
            "retrieval_status: success\n"
            "task_id: agent-plucky-comet\n"
            "kind: agent\n"
            "status: completed\n"
            "description: Python subagents scan\n"
            "subagent_type: explore\n"
            "interrupted: false\n"
            "timed_out: false\n"
            "terminal_reason: completed\n"
            "output_path: /Users/panda/.pythinker/sessions/s1/output.md\n"
            "output_size_bytes: 82841\n"
            "output_preview_bytes: 32768\n"
            "output_truncated: true\n"
            "offset: 0\n"
            "next_offset: 32768\n"
            "eof: false\n"
            "\n"
            "[output]\n"
            "Scan complete with findings."
        ),
        width=120,
    )
    assert "Read output" in rendered
    assert "to expand" in rendered
    assert "tool_status:" not in rendered
    assert "retrieval_status:" not in rendered
    assert "output_path:" not in rendered
    assert "Scan complete with findings." not in rendered


def test_task_output_expanded_shows_description_and_body():
    rendered = _render(
        "TaskOutput",
        {"task_id": "agent-plucky-comet"},
        output=(
            "tool_status: success\n"
            "task_id: agent-plucky-comet\n"
            "status: completed\n"
            "description: Python subagents scan\n"
            "output_size_bytes: 100\n"
            "output_truncated: false\n"
            "\n"
            "[output]\n"
            "body"
        ),
        expanded=True,
        width=120,
    )
    assert "Python subagents scan (1 lines)" in rendered
    assert "body" in rendered
    assert "tool_status" not in rendered


def test_task_output_renders_id_and_block_flag():
    rendered = _render(
        "TaskOutput",
        {"task_id": "abc-123", "block": True, "timeout": 60},
        output="logs...",
    )
    assert "⏺ TaskOutput(" in rendered
    assert "abc-123" in rendered
    assert "block" in rendered


def test_taskinput_renders_id_and_redacted_input_preview():
    rendered = _render(
        "TaskInput",
        {
            "task_id": "bash-abc123",
            "text": "OPENAI_API_KEY=sk-secret\nrun migration",
            "newline": False,
        },
    )

    assert "⏺ TaskInput(" in rendered
    assert "bash-abc123" in rendered
    assert "[redacted: input looks secret-like]" in rendered
    assert "sk-secret" not in rendered


def test_taskinput_result_uses_extras_status():
    rendered = _render(
        "TaskInput",
        {"task_id": "bash-abc123", "text": "continue", "newline": True},
        output=(
            "tool_status: success\n"
            "task_id: bash-abc123\n"
            "input_event_id: i123\n"
            "newline: true\n"
            "input: continue"
        ),
        details={"extras": {"status": "success"}},
    )

    assert "Input queued" in rendered
    assert "success" in rendered
    assert "tool_status:" not in rendered
    assert "input_event_id:" not in rendered


def test_taskinput_collapsed_metadata_is_expandable_and_expanded_shows_details():
    defn = get_tool_renderer("TaskInput")
    assert defn is not None
    output = (
        "tool_status: success\n"
        "task_id: bash-abc123\n"
        "input_event_id: i123\n"
        "newline: true\n"
        "input: continue"
    )
    comp = ToolExecutionComponent("TaskInput", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"task_id": "bash-abc123", "text": "continue", "newline": True})
    comp.set_args_complete()
    comp.mark_execution_started()
    comp.set_result(ToolResultPayload(text=output, details={"extras": {"status": "success"}}))

    collapsed = render_plain(comp.render(), width=120)

    assert "Input queued" in collapsed
    assert "input_event_id:" not in collapsed
    assert "tool_status:" not in collapsed
    assert comp.can_expand

    comp.set_expanded(True)
    expanded = render_plain(comp.render(), width=120)

    assert "input_event_id: i123" in expanded
    assert "tool_status: success" in expanded
    assert "input: continue" in expanded


def test_taskhandoff_renders_id():
    rendered = _render(
        "TaskHandoff",
        {"task_id": "agent-abc123"},
        output="tool_status: success\ntask_id: agent-abc123\nstatus: running",
    )

    assert "⏺ TaskHandoff(" in rendered
    assert "agent-abc123" in rendered


def test_taskhandoff_result_parses_metadata_and_status():
    rendered = _render(
        "TaskHandoff",
        {"task_id": "agent-abc123"},
        output=(
            "tool_status: success\n"
            "task_id: agent-abc123\n"
            "kind: agent\n"
            "status: completed\n"
            "description: Python subagents scan\n"
            "command: [not a shell task]\n"
            "cwd: /repo\n"
            "output_path: /tmp/pythinker/tasks/agent-abc123.output\n"
            "stop_hint: Use TaskStop with this task_id to request cancellation.\n"
            "reattach_warning: Live terminal reattachment is not available for this task."
        ),
        details={"extras": {"status": "success"}},
        width=120,
    )

    assert "Handoff details" in rendered
    assert "success" in rendered
    assert "completed" in rendered
    assert "Python subagents scan" in rendered
    assert "agent-abc123.output" in rendered
    assert "tool_status:" not in rendered
    assert "output_path:" not in rendered


def test_taskhandoff_metadata_description_sanitizes_ansi():
    rendered = _render(
        "TaskHandoff",
        {"task_id": "agent-abc123"},
        output=(
            "tool_status: success\n"
            "task_id: agent-abc123\n"
            "status: completed\n"
            "description: Python subagents scan\x1b[31m\n"
            "output_path: /tmp/pythinker/tasks/agent-abc123.output"
        ),
        details={"extras": {"status": "success"}},
        width=120,
    )

    assert "\x1b" not in rendered
    assert "Python subagents scan" in rendered


def test_taskhandoff_collapsed_metadata_is_expandable_and_expanded_shows_details():
    defn = get_tool_renderer("TaskHandoff")
    assert defn is not None
    output = (
        "tool_status: success\n"
        "task_id: agent-abc123\n"
        "kind: agent\n"
        "status: completed\n"
        "description: Python subagents scan\n"
        "command: [not a shell task]\n"
        "cwd: /repo\n"
        "output_path: /tmp/pythinker/tasks/agent-abc123.output\n"
        "stop_hint: Use TaskStop with this task_id to request cancellation.\n"
        "reattach_warning: Live terminal reattachment is not available for this task."
    )
    comp = ToolExecutionComponent("TaskHandoff", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"task_id": "agent-abc123"})
    comp.set_args_complete()
    comp.mark_execution_started()
    comp.set_result(ToolResultPayload(text=output, details={"extras": {"status": "success"}}))

    collapsed = render_plain(comp.render(), width=120)

    assert "Handoff details" in collapsed
    assert "tool_status:" not in collapsed
    assert "output_path:" not in collapsed
    assert "stop_hint:" not in collapsed
    assert comp.can_expand

    comp.set_expanded(True)
    expanded = render_plain(comp.render(), width=120)

    assert "tool_status: success" in expanded
    assert "output_path: /tmp/pythinker/tasks/agent-abc123.output" in expanded
    assert "stop_hint: Use TaskStop with this task_id to request cancellation." in expanded


def test_task_stop_renders_id():
    rendered = _render("TaskStop", {"task_id": "abc-123", "reason": "user requested"})
    assert "⏺ TaskStop(" in rendered
    assert "abc-123" in rendered


# ---------------------------------------------------------------------------
# ToolSearch
# ---------------------------------------------------------------------------


def test_tool_search_renders_compact_summary_not_catalog():
    catalog = "\n".join(
        [
            "- Agent - Start a subagent instance to work on a focused task.",
            "- RunAgents - Launch a bounded group of focused child agents.",
            "- ReadFile - Read text content from a file.",
            "- Grep - A powerful search tool based on ripgrep.",
            "- Shell - Execute a bash command.",
        ]
    )
    rendered = _render("ToolSearch", {"query": "read", "max_results": 8}, output=catalog)
    assert "⏺ Tools(read)" in rendered
    assert "5 tools discovered" in rendered
    assert "Agent" in rendered
    assert "Grep" in rendered
    assert "Start a subagent" not in rendered
    assert "powerful search tool" not in rendered


def test_tool_search_expanded_shows_names_only():
    catalog = "\n".join(
        [
            "- Agent - Start a subagent instance.",
            "- Grep - A powerful search tool based on ripgrep.",
        ]
    )
    rendered = _render(
        "ToolSearch",
        {"query": "agent"},
        output=catalog,
        expanded=True,
    )
    assert "Tools discovered: Agent, Grep" in rendered
    assert "Start a subagent" not in rendered
    assert "powerful search tool" not in rendered


def test_tool_search_no_match_renders_message():
    rendered = _render(
        "ToolSearch",
        {"query": "browser"},
        output="No visible tools matched `browser`.",
    )
    assert "No visible tools matched" in rendered
    assert "Start a subagent" not in rendered


def test_tool_search_streaming_uses_searching_header():
    rendered = _render_streaming("ToolSearch", {})
    assert "Searching Tools…" in rendered
    assert "max_results" not in rendered


# ---------------------------------------------------------------------------
# Memory / Recall / Scratchpad
# ---------------------------------------------------------------------------


def test_memory_add_renders_compact_call_and_result_summary():
    rendered = _render(
        "Memory",
        {
            "action": "add",
            "target": "memory",
            "content": "Renderer cards now cover Memory without dumping note bodies.",
        },
        output="Added memory entry to MEMORY.md.",
    )

    assert "⏺ Memory(add to project memory)" in rendered
    assert "Added project memory" in rendered
    assert "Renderer cards now cover Memory" not in rendered


def test_memory_action_call_sanitizes_ansi():
    rendered = _render(
        "Memory",
        {
            "action": "add\x1b[31m",
            "target": "memory",
            "content": "Renderer cards now cover Memory without dumping note bodies.",
        },
        output="Added memory entry to MEMORY.md.",
    )

    assert "\x1b" not in rendered


def test_memory_list_expanded_shows_bounded_status_text():
    status = "\n".join(f"{index}. Memory fact {index}" for index in range(1, 20))
    rendered = _render(
        "Memory",
        {"action": "list", "target": "memory"},
        output=status,
        expanded=True,
    )

    assert "⏺ Memory(list project memory)" in rendered
    assert "Listed project memory" in rendered
    assert "19 entries" in rendered
    assert "1. Memory fact 1" in rendered
    assert "15. Memory fact 15" in rendered
    assert "19. Memory fact 19" not in rendered
    assert "more lines" in rendered


def test_memory_short_collapsed_list_with_expand_hint_is_expandable():
    defn = get_tool_renderer("Memory")
    assert defn is not None
    comp = ToolExecutionComponent("Memory", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"action": "list", "target": "memory"})
    comp.set_args_complete()
    comp.mark_execution_started()
    comp.set_result(ToolResultPayload(text="1. Short memory fact"))

    rendered = render_plain(comp.render(), width=120)

    assert "ctrl+o expand" in rendered
    assert comp.can_expand


def test_memory_denial_text_is_preserved_verbatim():
    denial = (
        "Not saved to memory — you declined. This looks like it belongs in a "
        "project file; edit that file directly so the change actually takes effect."
    )
    rendered = _render(
        "Memory",
        {"action": "add", "target": "user", "content": "Always run the renderer gate."},
        output=denial,
        width=200,
    )

    assert "⏺ Memory(add to user memory)" in rendered
    assert denial in rendered


def test_recall_search_renders_summary_without_raw_session_list():
    output = "\n".join(
        [
            "Prior sessions in this workspace (most relevant first):",
            "",
            "- session_id: sess-abc",
            "  title: Renderer parity investigation",
            "- session_id: sess-def",
            "  title: TUI streaming smoothness",
            "",
            'Read one with Recall(mode="read", session_id="...").',
        ]
    )
    rendered = _render(
        "Recall",
        {"mode": "search", "query": "renderer parity"},
        output=output,
        details={"message": "Found 2 prior session(s)."},
    )

    assert '⏺ Recall(search "renderer parity")' in rendered
    assert "Found 2 prior sessions" in rendered
    assert "session_id:" not in rendered
    assert "Renderer parity investigation" not in rendered


def test_recall_short_collapsed_search_with_expand_hint_is_expandable():
    defn = get_tool_renderer("Recall")
    assert defn is not None
    comp = ToolExecutionComponent("Recall", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"mode": "search", "query": "renderer"})
    comp.set_args_complete()
    comp.mark_execution_started()
    comp.set_result(
        ToolResultPayload(
            text="\n".join(
                [
                    "Prior sessions in this workspace (most relevant first):",
                    "",
                    "- session_id: sess-abc",
                    "  title: Renderer parity investigation",
                    "",
                    'Read one with Recall(mode="read", session_id="...").',
                ]
            ),
            details={"message": "Found 1 prior session(s)."},
        )
    )

    rendered = render_plain(comp.render(), width=120)

    assert "ctrl+o expand" in rendered
    assert comp.can_expand


def test_recall_read_expanded_shows_bounded_transcript_text():
    transcript = "\n".join(f"[assistant] message {index}" for index in range(1, 25))
    rendered = _render(
        "Recall",
        {"mode": "read", "session_id": "sess-abc", "message_offset": 5, "max_messages": 20},
        output=transcript,
        details={"message": "Read session sess-abc."},
        expanded=True,
    )

    assert "⏺ Recall(read sess-abc · offset 5 · limit 20)" in rendered
    assert "Read session sess-abc" in rendered
    assert "[assistant] message 1" in rendered
    assert "[assistant] message 15" in rendered
    assert "[assistant] message 24" not in rendered
    assert "more lines" in rendered


def test_recall_read_session_id_sanitizes_ansi():
    rendered = _render(
        "Recall",
        {"mode": "read", "session_id": "sess-abc\x1b[31m"},
        output="[assistant] short recalled message",
        details={"message": "Read session sess-abc."},
    )

    assert "\x1b" not in rendered


def test_recall_short_collapsed_read_with_expand_hint_is_expandable():
    defn = get_tool_renderer("Recall")
    assert defn is not None
    comp = ToolExecutionComponent("Recall", "tc-1", definition=defn, cwd="/repo")
    comp.update_args({"mode": "read", "session_id": "sess-abc"})
    comp.set_args_complete()
    comp.mark_execution_started()
    comp.set_result(
        ToolResultPayload(
            text="[assistant] short recalled message",
            details={"message": "Read session sess-abc."},
        )
    )

    rendered = render_plain(comp.render(), width=120)

    assert "ctrl+o expand" in rendered
    assert comp.can_expand


def test_scratchpad_add_renders_kind_summary_without_note_body():
    rendered = _render(
        "Scratchpad",
        {
            "action": "add",
            "kind": "decision",
            "content": "Use bounded expanded text for memory-family cards.",
        },
        output="Note recorded (decision).",
    )

    assert "⏺ Scratchpad(decision)" in rendered
    assert "Recorded decision note" in rendered
    assert "Use bounded expanded text" not in rendered


def test_scratchpad_kind_call_sanitizes_ansi():
    rendered = _render(
        "Scratchpad",
        {
            "action": "add",
            "kind": "decision\x1b[31m",
            "content": "Use bounded expanded text for memory-family cards.",
        },
        output="Note recorded (decision).",
    )

    assert "\x1b" not in rendered


def test_scratchpad_error_text_is_preserved_verbatim():
    error = "Scratchpad is only available to the root agent."
    rendered = _render(
        "Scratchpad",
        {"action": "add", "kind": "note", "content": "temporary observation"},
        output=error,
        is_error=True,
    )

    assert "✘ Scratchpad(note)" in rendered
    assert error in rendered


# ---------------------------------------------------------------------------
# LSP / MCP resources / Worktree
# ---------------------------------------------------------------------------


def test_lsp_renders_operation_location_and_result_summary():
    rendered = _render(
        "LSP",
        {
            "operation": "findReferences",
            "file_path": "/repo/src/foo.py",
            "line": 12,
            "character": 8,
        },
        output="src/foo.py:12:8\nsrc/bar.py:4:1",
        details={
            "output": "src/foo.py:12:8\nsrc/bar.py:4:1",
            "message": "",
            "display": [],
            "extras": {
                "result_count": 2,
                "file_count": 2,
                "operation": "findReferences",
            },
        },
    )

    assert "⏺ LSP(" in rendered
    assert 'operation: "findReferences"' in rendered
    assert 'file: "src/foo.py"' in rendered
    assert "position: 12:8" in rendered
    assert "Found 2 references across 2 files" in rendered
    assert "src/foo.py:12:8" not in rendered


def test_lsp_short_collapsed_result_that_hides_text_is_expandable():
    defn = get_tool_renderer("LSP")
    assert defn is not None
    comp = ToolExecutionComponent("LSP", "tc-1", definition=defn, cwd="/repo")
    comp.update_args(
        {
            "operation": "findReferences",
            "file_path": "/repo/src/foo.py",
            "line": 12,
            "character": 8,
        }
    )
    comp.set_args_complete()
    comp.mark_execution_started()
    comp.set_result(
        ToolResultPayload(
            text="src/foo.py:12:8",
            details={
                "extras": {
                    "result_count": 1,
                    "file_count": 1,
                    "operation": "findReferences",
                },
            },
        )
    )

    collapsed = render_plain(comp.render(), width=120)

    assert "Found 1 reference" in collapsed
    assert "src/foo.py:12:8" not in collapsed
    assert comp.can_expand


def test_lsp_zero_result_counts_preserve_guidance_text():
    rendered = _render(
        "LSP",
        {
            "operation": "findReferences",
            "file_path": "/repo/src/foo.py",
            "line": 12,
            "character": 8,
        },
        output="No references found at this position.\nTry checking the symbol location.",
        details={
            "output": "No references found at this position.\nTry checking the symbol location.",
            "message": "",
            "display": [],
            "extras": {
                "result_count": 0,
                "file_count": 0,
                "operation": "findReferences",
            },
        },
    )

    assert "No references found at this position." in rendered
    assert "Try checking the symbol location." in rendered
    assert "Found 0" not in rendered


def test_lsp_fallback_result_sanitizes_ansi_text():
    rendered = _render(
        "LSP",
        {"operation": "hover", "file_path": "/repo/src/foo.py", "line": 3, "character": 4},
        output="Hover info available\x1b[31m",
    )

    assert "\x1b" not in rendered
    assert "Hover info available" in rendered


def test_lsp_result_without_counts_renders_fallback_text():
    rendered = _render(
        "LSP",
        {"operation": "hover", "file_path": "/repo/src/foo.py", "line": 3, "character": 4},
        output="Hover info available\n```python\nvalue: int\n```",
    )

    assert "⏺ LSP(" in rendered
    assert 'operation: "hover"' in rendered
    assert "Hover info available" in rendered


def test_mcp_resource_renderers_use_expected_labels():
    listed = _render(
        "ListMcpResources",
        {"server": "context7"},
        output='[{"uri":"docs://react","name":"React docs"}]',
    )
    read = _render(
        "ReadMcpResource",
        {"server": "context7", "uri": "docs://react"},
        output='{"contents":[{"text":"React docs"}]}',
    )

    assert '⏺ MCPResources(List MCP resources from server "context7")' in listed
    assert "React docs" in listed
    assert '⏺ MCPResource(Read resource "docs://react" from server "context7")' in read
    assert "React docs" in read


def test_worktree_renderers_parse_tool_output_metadata():
    entered = _render(
        "EnterWorktree",
        {"name": "fix-ui"},
        output="\n".join(
            [
                "session_worktree: entered",
                "worktree_path: /tmp/pythinker-worktree",
                "original_work_dir: /repo",
                "cleanup: retained until you remove it explicitly",
            ]
        ),
    )
    exited = _render(
        "ExitWorktree",
        {},
        output="\n".join(
            [
                "session_worktree: exited",
                "worktree_path: /tmp/pythinker-worktree",
                "restored_work_dir: /repo",
                "retained: true",
            ]
        ),
    )

    assert "⏺ Worktree(Creating worktree…)" in entered
    assert "Switched to worktree" in entered
    assert "/tmp/pythinker-worktree" in entered
    assert "⏺ Worktree(Exiting worktree…)" in exited
    assert "Kept worktree" in exited
    assert "Returned to /repo" in exited


def test_worktree_enter_error_does_not_render_success_label():
    # C01: a failed EnterWorktree must surface the error, never a success switch.
    rendered = _render(
        "EnterWorktree",
        {"name": "fix-ui"},
        output="failed to create worktree",
        is_error=True,
    )
    assert "Switched to worktree" not in rendered
    assert "failed to create worktree" in rendered


def test_worktree_exit_error_does_not_render_success_label():
    # C01: a failed ExitWorktree must surface the error, never keep/remove.
    rendered = _render(
        "ExitWorktree",
        {},
        output="failed to restore working directory",
        is_error=True,
    )
    assert "Kept worktree" not in rendered
    assert "Removed worktree" not in rendered
    assert "failed to restore working directory" in rendered


# ---------------------------------------------------------------------------
# Plan tools
# ---------------------------------------------------------------------------


def test_enter_plan_mode_renders():
    rendered = _render("EnterPlanMode", {})
    assert "⏺ Plan(entering)" in rendered


def test_exit_plan_mode_renders_options():
    rendered_running = _render_running(
        "ExitPlanMode",
        {
            "options": [
                {"label": "Refactor first"},
                {"label": "Add tests first"},
            ]
        },
    )
    # The running marker blinks (blink_visible() is time-dependent); assert stable content only.
    assert "Plan(awaiting approval)" in rendered_running
    assert "Refactor first" in rendered_running
    assert "Add tests first" in rendered_running

    rendered_done = _render(
        "ExitPlanMode",
        {
            "options": [
                {"label": "Refactor first"},
                {"label": "Add tests first"},
            ]
        },
        output="Plan approved",
    )
    assert "⏺ Plan(exiting)" in rendered_done


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


def test_successful_non_review_agent_shows_done_subline_not_prose_dump():
    """Completed non-review agents show a compact Done sub-line, not raw summary prose."""
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
    assert "Done" in rendered
    assert "Refactored the auth module" not in rendered
    assert "Review Findings" not in rendered
