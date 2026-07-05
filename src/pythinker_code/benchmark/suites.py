from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import cast

from pythinker_code.benchmark.errors import (
    MalformedBenchmarkSuiteError,
    UnknownBenchmarkSuiteError,
)
from pythinker_code.benchmark.tasks import load_task


@dataclass(frozen=True, slots=True)
class BenchmarkSuite:
    name: str
    title: str
    description: str
    tasks: list[str]


def _bundled_suites_root() -> Path:
    return Path(str(resources.files("pythinker_code.benchmark.bundled.suites")))


def list_suite_names(suite_root: Path | None = None) -> list[str]:
    root = suite_root or _bundled_suites_root()
    return sorted(path.stem for path in root.glob("*.json"))


def load_suite(
    name: str,
    *,
    suite_root: Path | None = None,
    task_root: Path | None = None,
) -> BenchmarkSuite:
    root = suite_root or _bundled_suites_root()
    path = root / f"{name}.json"
    if not path.exists():
        raise UnknownBenchmarkSuiteError(f"Unknown benchmark suite: {name}")
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MalformedBenchmarkSuiteError(f"Malformed benchmark suite JSON: {name}") from exc
    if not isinstance(raw, dict):
        raise MalformedBenchmarkSuiteError(f"Benchmark suite must be an object: {name}")
    suite = _parse_suite(cast(dict[str, object], raw), name)
    for task_id in suite.tasks:
        load_task(task_id, task_root=task_root)
    return suite


def _parse_suite(raw: dict[str, object], name: str) -> BenchmarkSuite:
    required = ("name", "title", "description", "tasks")
    missing = [key for key in required if key not in raw]
    if missing:
        raise MalformedBenchmarkSuiteError(
            f"Benchmark suite {name!r} missing required fields: {', '.join(missing)}"
        )
    suite_name = raw["name"]
    title = raw["title"]
    description = raw["description"]
    tasks = raw["tasks"]
    if suite_name != name:
        raise MalformedBenchmarkSuiteError(
            f"Benchmark suite file {name!r} contains mismatched name {suite_name!r}"
        )
    if not isinstance(title, str) or not isinstance(description, str):
        raise MalformedBenchmarkSuiteError(f"Benchmark suite {name!r} has invalid text fields")
    if (
        not isinstance(tasks, list)
        or not tasks
        or not all(isinstance(t, str) for t in cast(list[object], tasks))
    ):
        raise MalformedBenchmarkSuiteError(
            f"Benchmark suite {name!r} must contain a non-empty task list"
        )
    return BenchmarkSuite(
        name=name, title=title, description=description, tasks=cast(list[str], tasks)
    )
