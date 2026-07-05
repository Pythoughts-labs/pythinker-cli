from __future__ import annotations

import json
from pathlib import Path

from pythinker_code.benchmark.activity import summarize_benchmark_activity
from pythinker_code.benchmark.report import render_run_report


def test_summarize_activity_counts_changed_lines_and_tools(tmp_path: Path) -> None:
    wire = tmp_path / "wire.jsonl"
    wire.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "message": {
                            "type": "ToolCall",
                            "payload": {"id": "call-1", "name": "StrReplaceFile"},
                        }
                    }
                ),
                json.dumps(
                    {
                        "message": {
                            "type": "ToolCall",
                            "payload": {"id": "call-2", "name": "Bash"},
                        }
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    before = {"app.py": "def f():\n    return 1\n"}
    after = {"app.py": "def f():\n    return 2\n", "new.py": "x = 1\n"}

    activity = summarize_benchmark_activity(before, after, wire, 0)

    assert activity["changed_files_count"] == 2
    assert activity["added_lines"] == 2
    assert activity["removed_lines"] == 1
    assert activity["tool_calls_by_name"] == {"Bash": 1, "StrReplaceFile": 1}
    assert activity["shell_tool_calls"] == 1


def test_summarize_activity_tool_calls_ignore_wire_read_errors(tmp_path: Path) -> None:
    wire_dir = tmp_path / "wire.jsonl"
    wire_dir.mkdir()

    activity = summarize_benchmark_activity({}, {}, wire_dir, 0)

    assert activity["tool_calls_by_name"] == {}
    assert activity["shell_tool_calls"] == 0


def test_render_run_report_includes_tool_call_breakdown(tmp_path: Path) -> None:
    run_report = render_run_report(
        run={
            "run_id": "run-id",
            "model_key": "mock",
            "provider_key": "provider",
            "task_id": "task",
        },
        summary={
            "activity": {
                "changed_files_count": 1,
                "added_lines": 10,
                "removed_lines": 3,
                "tool_calls_by_name": {"StrReplaceFile": 2, "Bash": 1},
                "shell_tool_calls": 2,
            }
        },
        artifact_root=tmp_path,
    )

    assert "- tool calls by name: Bash: 1, StrReplaceFile: 2" in run_report


def test_render_run_report_missing_activity_shows_unavailable(tmp_path: Path) -> None:
    run_report = render_run_report(
        run={
            "run_id": "run-id",
            "model_key": "mock",
            "provider_key": "provider",
            "task_id": "task",
        },
        summary={},
        artifact_root=tmp_path,
    )

    assert "- Activity: unavailable" in run_report
    assert "  - changed files:" not in run_report
