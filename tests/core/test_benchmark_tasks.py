from __future__ import annotations

import json
from pathlib import Path

import pytest

from pythinker_code.benchmark.errors import (
    MalformedBenchmarkTaskError,
    UnknownBenchmarkSuiteError,
    UnknownBenchmarkTaskError,
)
from pythinker_code.benchmark.suites import load_suite
from pythinker_code.benchmark.tasks import load_task, materialize_workspace


def test_load_bundled_smoke_task() -> None:
    task = load_task("smoke-edit-readme")

    assert task.id == "smoke-edit-readme"
    assert "README.md" in task.workspace.files
    assert task.verification.command
    assert task.limits.timeout_seconds > 0


def test_load_bundled_smoke_suite_preserves_order() -> None:
    suite = load_suite("pythinker-smoke")

    assert suite.name == "pythinker-smoke"
    assert suite.tasks == [
        "smoke-edit-readme",
        "smoke-fix-python-test",
        "smoke-add-small-function",
    ]


def test_load_bundled_core_suite_preserves_order() -> None:
    suite = load_suite("pythinker-core")

    assert suite.name == "pythinker-core"
    assert suite.tasks == [
        "core-atomic-transfer",
        "core-dedup-order",
        "core-explicit-none-metadata",
        "core-safe-path-join",
    ]


def test_unknown_task_raises_typed_error() -> None:
    with pytest.raises(UnknownBenchmarkTaskError, match="missing-task"):
        load_task("missing-task")


def test_unknown_suite_raises_typed_error() -> None:
    with pytest.raises(UnknownBenchmarkSuiteError, match="missing-suite"):
        load_suite("missing-suite")


def test_malformed_task_rejected(tmp_path: Path) -> None:
    task_dir = tmp_path / "tasks"
    task_dir.mkdir()
    (task_dir / "bad.json").write_text(
        json.dumps({"id": "bad", "prompt": "missing required fields"}),
        encoding="utf-8",
    )

    with pytest.raises(MalformedBenchmarkTaskError):
        load_task("bad", task_root=task_dir)


def test_materialize_workspace_writes_files(tmp_path: Path) -> None:
    task = load_task("smoke-edit-readme")

    materialize_workspace(task, tmp_path)

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# Example Project\n\n"


@pytest.mark.parametrize(
    ("task_id", "test_file", "required_snippets"),
    [
        (
            "core-atomic-transfer",
            "test_ledger.py",
            [
                "test_missing_source_rolls_back",
                "ledger.transfer('missing', 'b', 1)",
                "[0, -1]",
            ],
        ),
        (
            "core-dedup-order",
            "test_seqkit.py",
            [
                "test_unique_accepts_generators",
                "unique(x for x in ['a', 'a', 'b'])",
            ],
        ),
        (
            "core-explicit-none-metadata",
            "test_metadata.py",
            [
                "test_falsey_explicit_values_are_preserved",
                "name=False",
                "version=0",
            ],
        ),
        (
            "core-safe-path-join",
            "test_paths.py",
            [
                "test_safe_join_rejects_sibling_prefix_absolute_path",
                "test_safe_join_rejects_symlink_escape",
            ],
        ),
    ],
)
def test_core_benchmark_tasks_cover_reviewed_edge_cases(
    task_id: str, test_file: str, required_snippets: list[str]
) -> None:
    task = load_task(task_id)
    test_source = task.workspace.files[test_file]

    for snippet in required_snippets:
        assert snippet in test_source
