from __future__ import annotations

import difflib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast


def summarize_benchmark_activity(
    before: Mapping[str, str],
    after: Mapping[str, str],
    wire_file: Path,
    wire_offset: int,
) -> dict[str, object]:
    added, removed = _changed_line_counts(before, after)
    tool_calls_by_name = _tool_calls_by_name(wire_file, wire_offset)
    return {
        "changed_files_count": len(_changed_file_names(before, after)),
        "added_lines": added,
        "removed_lines": removed,
        "tool_calls_by_name": dict(sorted(tool_calls_by_name.items())),
        "shell_tool_calls": tool_calls_by_name.get("Bash", 0) + tool_calls_by_name.get("Shell", 0),
    }


def _changed_file_names(before: Mapping[str, str], after: Mapping[str, str]) -> set[str]:
    names = set(before) | set(after)
    return {name for name in names if before.get(name) != after.get(name)}


def _changed_line_counts(before: Mapping[str, str], after: Mapping[str, str]) -> tuple[int, int]:
    added = 0
    removed = 0
    for name in sorted(_changed_file_names(before, after)):
        diff = difflib.ndiff(
            before.get(name, "").splitlines(),
            after.get(name, "").splitlines(),
        )
        for line in diff:
            if line.startswith("+ "):
                added += 1
            elif line.startswith("- "):
                removed += 1
    return added, removed


def _tool_calls_by_name(wire_file: Path, offset: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not wire_file.exists():
        return counts
    try:
        with wire_file.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for line in f:
                try:
                    raw: object = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict):
                    continue
                message = cast(dict[str, object], raw).get("message")
                if not isinstance(message, dict):
                    continue
                message_data = cast(dict[str, object], message)
                if message_data.get("type") != "ToolCall":
                    continue
                payload = message_data.get("payload")
                if not isinstance(payload, dict):
                    continue
                name = cast(dict[str, object], payload).get("name")
                if isinstance(name, str) and name:
                    counts[name] = counts.get(name, 0) + 1
    except OSError:
        return {}
    return counts
