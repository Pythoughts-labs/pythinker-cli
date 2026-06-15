"""Bridge AI audit reports to offline eval budgets (obs-eval-4 live slice)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from tests_ai.eval_schema import EvalCase, EvalVerdict, ObservedMetrics, score_eval_case

_EVAL_CASES = TypeAdapter(list[EvalCase])


def load_eval_cases(path: Path) -> list[EvalCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _EVAL_CASES.validate_python(payload)


def observed_from_report_case(case: dict[str, object]) -> ObservedMetrics:
    """Read optional efficiency metrics agents may attach to a report case."""
    raw = case.get("metrics")
    if not isinstance(raw, dict):
        return ObservedMetrics()
    tools_used = raw.get("tools_used")
    tools_tuple: tuple[str, ...] = ()
    if isinstance(tools_used, list):
        tools_tuple = tuple(str(item) for item in tools_used)
    return ObservedMetrics(
        tool_calls=int(raw.get("tool_calls", 0) or 0),
        input_tokens=int(raw.get("input_tokens", 0) or 0),
        output_tokens=int(raw.get("output_tokens", 0) or 0),
        tool_errors=int(raw.get("tool_errors", 0) or 0),
        step_count=int(raw.get("step_count", 0) or 0),
        tools_used=tools_tuple,
    )


def gate_report(report: list[dict[str, object]], cases: list[EvalCase]) -> list[EvalVerdict]:
    """Score report cases that include a matching ``name`` and optional ``metrics`` block."""
    cases_by_name = {case.name: case for case in cases}
    verdicts: list[EvalVerdict] = []
    for entry in report:
        for case in entry.get("cases", []):
            if not isinstance(case, dict):
                continue
            name = str(case.get("name") or "")
            eval_case = cases_by_name.get(name)
            if eval_case is None:
                continue
            verdicts.append(score_eval_case(eval_case, observed_from_report_case(case)))
    return verdicts
