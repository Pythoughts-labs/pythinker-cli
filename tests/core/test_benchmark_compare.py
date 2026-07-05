from __future__ import annotations

from pythinker_code.benchmark.compare import readiness_warnings
from pythinker_code.benchmark.export import export_rows
from pythinker_code.benchmark.types import BenchmarkReportRow


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


def test_readiness_warnings_flag_single_repeat_per_model() -> None:
    warnings = readiness_warnings(
        [
            _row("model-a", "task-a", 1, 0.01),
            _row("model-b", "task-b", 2, 0.02),
        ]
    )

    assert "single repeat" in " ".join(warnings).lower()


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


def test_readiness_warnings_flag_direct_task_runs_as_local_fixture() -> None:
    row = BenchmarkReportRow(
        run={
            "model_key": "model-a",
            "task_id": "core-safe-path-join",
            "suite_name": None,
            "repeat_index": 1,
        },
        summary={
            "status": "passed",
            "usage": {"estimated_cost_usd": 0.01},
            "environment": {"git_dirty": False},
        },
    )

    warnings = readiness_warnings([row])

    assert "local fixture" in " ".join(warnings).lower()


def test_readiness_warnings_flag_smoke_suite_as_local_fixture() -> None:
    row = BenchmarkReportRow(
        run={
            "model_key": "model-a",
            "task_id": "smoke-edit-readme",
            "suite_name": "pythinker-smoke",
            "repeat_index": 1,
        },
        summary={
            "status": "passed",
            "usage": {"estimated_cost_usd": 0.01},
            "environment": {"git_dirty": False},
        },
    )

    warnings = readiness_warnings([row])

    assert "local fixture" in " ".join(warnings).lower()


def test_export_rows_flatten_runtime_usage_and_activity() -> None:
    rows = [
        BenchmarkReportRow(
            run={"run_id": "r1", "model_key": "m1", "task_id": "t1", "repeat_index": 1},
            summary={
                "status": "passed",
                "score": 1.0,
                "runtime": {"duration_ms": 10, "steps": 2, "tool_calls": 3},
                "usage": {"total_tokens": 42, "estimated_cost_usd": 0.01},
                "activity": {"added_lines": 4, "removed_lines": 1, "shell_tool_calls": 1},
            },
        )
    ]

    assert export_rows(rows) == [
        {
            "run_id": "r1",
            "model": "m1",
            "task": "t1",
            "repeat": 1,
            "status": "passed",
            "score": 1.0,
            "duration_ms": 10,
            "steps": 2,
            "tool_calls": 3,
            "total_tokens": 42,
            "estimated_cost_usd": 0.01,
            "added_lines": 4,
            "removed_lines": 1,
            "shell_tool_calls": 1,
        }
    ]
