from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from typing import cast

from pythinker_code.benchmark.types import BenchmarkReportRow, JsonObject

EXPORT_FIELDS = [
    "run_id",
    "model",
    "task",
    "repeat",
    "status",
    "score",
    "duration_ms",
    "steps",
    "tool_calls",
    "total_tokens",
    "estimated_cost_usd",
    "added_lines",
    "removed_lines",
    "shell_tool_calls",
]


def export_rows(rows: Sequence[BenchmarkReportRow]) -> list[dict[str, object]]:
    return [_export_row(row) for row in rows]


def render_export(rows: Sequence[BenchmarkReportRow], fmt: str) -> str:
    flat_rows = export_rows(rows)
    if fmt == "json":
        return json.dumps(flat_rows, indent=2) + "\n"
    if fmt == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        writer.writerows(flat_rows)
        return output.getvalue()
    raise ValueError(f"Unsupported benchmark export format: {fmt}")


def _export_row(row: BenchmarkReportRow) -> dict[str, object]:
    runtime = _mapping(row.summary.get("runtime"))
    usage = _mapping(row.summary.get("usage"))
    activity = _mapping(row.summary.get("activity"))
    return {
        "run_id": row.run.get("run_id", ""),
        "model": row.run.get("model_key", ""),
        "task": row.run.get("task_id", ""),
        "repeat": row.run.get("repeat_index", 1),
        "status": row.summary.get("status", row.run.get("status", "unknown")),
        "score": row.summary.get("score", 0.0),
        "duration_ms": runtime.get("duration_ms", 0),
        "steps": runtime.get("steps", 0),
        "tool_calls": runtime.get("tool_calls", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "estimated_cost_usd": usage.get("estimated_cost_usd"),
        "added_lines": activity.get("added_lines", 0),
        "removed_lines": activity.get("removed_lines", 0),
        "shell_tool_calls": activity.get("shell_tool_calls", 0),
    }


def _mapping(value: object) -> JsonObject:
    return cast(JsonObject, value) if isinstance(value, dict) else {}
