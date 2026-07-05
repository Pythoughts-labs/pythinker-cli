from __future__ import annotations

from pythinker_code.benchmark.commands import BenchmarkReportRow
from pythinker_code.benchmark.compare import readiness_warnings


def _row(model: str, task: str, repeat: int, cost: object = None) -> BenchmarkReportRow:
    return BenchmarkReportRow(
        run={
            "model_key": model,
            "task_id": task,
            "suite_name": "pythinker-core",
            "repeat_index": repeat,
        },
        summary={
            "status": "passed",
            "usage": {"estimated_cost_usd": cost},
            "environment": {"git_dirty": False},
        },
    )


def test_readiness_warnings_flag_non_publishable_single_model_run() -> None:
    warnings = readiness_warnings([_row("minimax/m3", "core-safe-path-join", 1)])

    assert "single model" in " ".join(warnings).lower()
    assert "single repeat" in " ".join(warnings).lower()
    assert "local fixture" in " ".join(warnings).lower()
    assert "cost" in " ".join(warnings).lower()


def test_readiness_warnings_allow_multi_model_repeated_costed_run() -> None:
    warnings = readiness_warnings(
        [
            _row("model-a", "task", 1, 0.01),
            _row("model-a", "task", 2, 0.01),
            _row("model-b", "task", 1, 0.02),
            _row("model-b", "task", 2, 0.02),
        ]
    )

    assert not any("single model" in warning.lower() for warning in warnings)
    assert not any("single repeat" in warning.lower() for warning in warnings)


def test_readiness_warnings_flag_missing_dirty_metadata() -> None:
    row = BenchmarkReportRow(
        run={
            "model_key": "model-a",
            "task_id": "task",
            "suite_name": "pythinker-core",
            "repeat_index": 1,
        },
        summary={
            "status": "passed",
            "usage": {"estimated_cost_usd": 0.01},
        },
    )
    warnings = readiness_warnings([row])

    assert any("dirty git worktree metadata" in warning.lower() for warning in warnings)
