from __future__ import annotations

import json
from pathlib import Path

from pythinker_code.benchmark.activity import summarize_benchmark_activity


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
