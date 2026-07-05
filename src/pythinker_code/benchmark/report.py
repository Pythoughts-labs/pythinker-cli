from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from pythinker_code.benchmark.redact import redact_text


def render_run_report(summary: Mapping[str, object], artifact_root: Path) -> str:
    runtime_obj = summary.get("runtime")
    verification_obj = summary.get("verification")
    usage_obj = summary.get("usage")
    changed_files = []
    runtime = cast(dict[str, Any], runtime_obj) if isinstance(runtime_obj, dict) else {}
    verification = (
        cast(dict[str, Any], verification_obj) if isinstance(verification_obj, dict) else {}
    )
    usage = cast(dict[str, Any], usage_obj) if isinstance(usage_obj, dict) else {}
    if runtime:
        raw_changed = runtime.get("changed_files")
        if isinstance(raw_changed, list):
            changed_files = [str(item) for item in cast(list[object], raw_changed)]
    verification_status = "unknown"
    if verification:
        verification_status = str(verification.get("status", "unknown"))
    estimated_cost = "unavailable"
    if usage.get("estimated_cost_usd") is not None:
        estimated_cost = f"${float(usage['estimated_cost_usd']):.4f}"

    lines = [
        "# Pythinker Benchmark",
        "",
        f"- Run: {summary.get('run_id', '')}",
        f"- Status: {summary.get('status', '')}",
        f"- Score: {summary.get('score', '')}",
        f"- Duration: {runtime.get('duration_ms', 0)} ms",
        f"- Steps: {runtime.get('steps', 0)}",
        f"- Tool calls: {runtime.get('tool_calls', 0)}",
        f"- Changed files: {', '.join(changed_files) if changed_files else '(none)'}",
        f"- Verification: {verification_status}",
        f"- Estimated cost: {estimated_cost}",
        f"- Artifacts: {artifact_root}",
        "",
    ]
    return redact_text("\n".join(lines))


def render_show(run: Mapping[str, object], summary: Mapping[str, object] | None = None) -> str:
    lines = [
        "Pythinker Benchmark run",
        "",
        f"Run: {run.get('run_id', '')}",
        f"Status: {run.get('status', '')}",
        f"Model: {run.get('model_key', '')}",
        f"Task: {run.get('task_id') or '(suite)'}",
        f"Suite: {run.get('suite_name') or '(none)'}",
        f"Artifacts: {run.get('artifact_root', '')}",
    ]
    if summary is not None:
        runtime_obj = summary.get("runtime")
        if isinstance(runtime_obj, dict):
            runtime = cast(dict[str, Any], runtime_obj)
            lines.extend(
                [
                    f"Duration: {runtime.get('duration_ms', 0)} ms",
                    f"Steps: {runtime.get('steps', 0)}",
                    f"Tool calls: {runtime.get('tool_calls', 0)}",
                ]
            )
    return "\n".join(lines)
