"""Offline wiring for scenario efficiency evals (task 6.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests_ai.eval_gate import gate_report, load_eval_cases
from tests_e2e.eval_schema import ObservedMetrics, score_eval_case


def test_load_example_eval_cases() -> None:
    cases = load_eval_cases(Path("tests_ai/eval_cases.example.json"))
    assert cases[0].name == "encoding smoke"
    assert cases[0].budget.max_tool_calls == 20


def test_gate_report_scores_optional_metrics() -> None:
    cases = load_eval_cases(Path("tests_ai/eval_cases.example.json"))
    report = [
        {
            "file": "tests_ai/test_encoding_error_handling.md",
            "cases": [
                {
                    "name": "encoding smoke",
                    "pass": True,
                    "metrics": {
                        "tool_calls": 2,
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "tools_used": ["ReadFile"],
                    },
                }
            ],
        }
    ]
    verdicts = gate_report(report, cases)
    assert len(verdicts) == 1
    assert verdicts[0].passed


def test_gate_report_raises_on_unknown_case_name() -> None:
    """A report case with no matching eval case is a contract drift, not a silent skip."""
    cases = load_eval_cases(Path("tests_ai/eval_cases.example.json"))
    report = [{"file": "x.md", "cases": [{"name": "does-not-exist", "pass": True}]}]
    with pytest.raises(ValueError, match="Unknown eval case name"):
        gate_report(report, cases)


def test_score_eval_case_flags_budget_breach() -> None:
    cases = load_eval_cases(Path("tests_ai/eval_cases.example.json"))
    observed = ObservedMetrics(tool_calls=999, input_tokens=10**6)
    assert not score_eval_case(cases[0], observed).passed
