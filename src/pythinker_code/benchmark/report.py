from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from pythinker_code.benchmark.redact import redact_text


def render_run_report(
    run: Mapping[str, object],
    summary: Mapping[str, object],
    artifact_root: Path,
    *,
    final_answer: str = "",
) -> str:
    runtime_obj = summary.get("runtime")
    verification_obj = summary.get("verification")
    usage_obj = summary.get("usage")
    activity_obj = summary.get("activity")
    changed_files = []
    runtime = cast(dict[str, Any], runtime_obj) if isinstance(runtime_obj, dict) else {}
    activity = cast(dict[str, Any], activity_obj) if isinstance(activity_obj, dict) else {}
    verification = (
        cast(dict[str, Any], verification_obj) if isinstance(verification_obj, dict) else {}
    )
    usage = cast(dict[str, Any], usage_obj) if isinstance(usage_obj, dict) else {}
    if runtime:
        raw_changed = runtime.get("changed_files")
        if isinstance(raw_changed, list):
            changed_files = [str(item) for item in cast(list[object], raw_changed)]
    activity_lines = [
        f"  - changed files: {activity.get('changed_files_count', 0)}",
        f"  - added lines: {activity.get('added_lines', 0)}",
        f"  - removed lines: {activity.get('removed_lines', 0)}",
        f"  - shell tool calls: {activity.get('shell_tool_calls', 0)}",
    ]
    verification_status = "unknown"
    if verification:
        verification_status = str(verification.get("status", "unknown"))
    estimated_cost = "unavailable"
    if usage.get("estimated_cost_usd") is not None:
        estimated_cost = f"${float(usage['estimated_cost_usd']):.4f}"
    verification_output = _verification_output(verification)
    final_excerpt = _excerpt(final_answer)

    lines = [
        "# Pythinker Benchmark",
        "",
        f"- Run: {summary.get('run_id', '')}",
        f"- Status: {summary.get('status', '')}",
        f"- Model: {run.get('model_key', '')}",
        f"- Provider: {run.get('provider_key', '')}",
        f"- Task: {run.get('task_id') or '(suite)'}",
        f"- Suite: {run.get('suite_name') or '(none)'}",
        f"- Score: {summary.get('score', '')}",
        f"- Duration: {runtime.get('duration_ms', 0)} ms",
        f"- Steps: {runtime.get('steps', 0)}",
        f"- Tool calls: {runtime.get('tool_calls', 0)}",
        f"- Changed files: {', '.join(changed_files) if changed_files else '(none)'}",
        "- Activity:",
        *activity_lines,
        f"- Verification: {verification_status}",
        f"- Estimated cost: {estimated_cost}",
        f"- Artifacts: {artifact_root}",
        "",
    ]
    if verification_output:
        lines.extend(["## Verification Output", "", "```text", verification_output, "```", ""])
    if final_excerpt:
        lines.extend(["## Final Answer", "", "```text", final_excerpt, "```", ""])
    return redact_text("\n".join(lines))


def _verification_output(verification: Mapping[str, Any]) -> str:
    chunks: list[str] = []
    stdout = _excerpt(str(verification.get("stdout", "")))
    stderr = _excerpt(str(verification.get("stderr", "")))
    if stdout:
        chunks.append(f"stdout:\n{stdout}")
    if stderr:
        chunks.append(f"stderr:\n{stderr}")
    return "\n\n".join(chunks)


def _excerpt(value: str, limit: int = 2_000) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated]"


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
