from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, cast

from pythinker_code.benchmark.errors import (
    MalformedBenchmarkTaskError,
    UnknownBenchmarkTaskError,
)


@dataclass(frozen=True, slots=True)
class BenchmarkWorkspace:
    files: dict[str, str]


@dataclass(frozen=True, slots=True)
class BenchmarkVerification:
    type: str
    command: str


@dataclass(frozen=True, slots=True)
class BenchmarkLimits:
    timeout_seconds: int
    max_steps: int


@dataclass(frozen=True, slots=True)
class BenchmarkTask:
    id: str
    title: str
    description: str
    prompt: str
    workspace: BenchmarkWorkspace
    verification: BenchmarkVerification
    limits: BenchmarkLimits
    tags: list[str]


def _bundled_tasks_root() -> Path:
    return Path(str(resources.files("pythinker_code.benchmark.bundled.tasks")))


def list_task_ids(task_root: Path | None = None) -> list[str]:
    root = task_root or _bundled_tasks_root()
    return sorted(path.stem for path in root.glob("*.json"))


def load_task(task_id: str, *, task_root: Path | None = None) -> BenchmarkTask:
    root = task_root or _bundled_tasks_root()
    path = root / f"{task_id}.json"
    if not path.exists():
        raise UnknownBenchmarkTaskError(f"Unknown benchmark task: {task_id}")
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MalformedBenchmarkTaskError(f"Malformed benchmark task JSON: {task_id}") from exc
    if not isinstance(raw, dict):
        raise MalformedBenchmarkTaskError(f"Benchmark task must be an object: {task_id}")
    return _parse_task(cast(dict[str, object], raw), task_id)


def _parse_task(raw: dict[str, object], task_id: str) -> BenchmarkTask:
    required = ("id", "title", "description", "prompt", "workspace", "verification", "limits")
    missing = [key for key in required if key not in raw]
    if missing:
        raise MalformedBenchmarkTaskError(
            f"Benchmark task {task_id!r} missing required fields: {', '.join(missing)}"
        )

    id_value = raw["id"]
    title = raw["title"]
    description = raw["description"]
    prompt = raw["prompt"]
    workspace = raw["workspace"]
    verification = raw["verification"]
    limits = raw["limits"]
    tags = raw.get("tags", [])

    if not (
        isinstance(id_value, str)
        and id_value
        and isinstance(title, str)
        and title
        and isinstance(prompt, str)
        and prompt
    ):
        raise MalformedBenchmarkTaskError(f"Benchmark task {task_id!r} has invalid text fields")
    if id_value != task_id:
        raise MalformedBenchmarkTaskError(
            f"Benchmark task file {task_id!r} contains mismatched id {id_value!r}"
        )
    if not isinstance(description, str):
        raise MalformedBenchmarkTaskError(f"Benchmark task {task_id!r} has invalid description")
    if not isinstance(workspace, dict):
        raise MalformedBenchmarkTaskError(f"Benchmark task {task_id!r} has invalid workspace")
    workspace_data = cast(dict[str, object], workspace)
    files = workspace_data.get("files")
    if not isinstance(files, dict) or not files:
        raise MalformedBenchmarkTaskError(f"Benchmark task {task_id!r} workspace has no files")
    parsed_files: dict[str, str] = {}
    for name, content in cast(dict[object, object], files).items():
        if (
            not isinstance(name, str)
            or not name
            or name.startswith("/")
            or ".." in Path(name).parts
        ):
            raise MalformedBenchmarkTaskError(
                f"Benchmark task {task_id!r} has unsafe workspace path"
            )
        if not isinstance(content, str):
            raise MalformedBenchmarkTaskError(
                f"Benchmark task {task_id!r} file {name!r} content must be text"
            )
        parsed_files[name] = content

    parsed_verification = _parse_verification(verification, task_id)
    parsed_limits = _parse_limits(limits, task_id)
    parsed_tags = (
        [tag for tag in cast(list[object], tags) if isinstance(tag, str)]
        if isinstance(tags, list)
        else []
    )
    return BenchmarkTask(
        id=id_value,
        title=title,
        description=description,
        prompt=prompt,
        workspace=BenchmarkWorkspace(files=parsed_files),
        verification=parsed_verification,
        limits=parsed_limits,
        tags=parsed_tags,
    )


def _parse_verification(value: object, task_id: str) -> BenchmarkVerification:
    if not isinstance(value, dict):
        raise MalformedBenchmarkTaskError(f"Benchmark task {task_id!r} has invalid verification")
    data = cast(dict[str, object], value)
    typ = data.get("type")
    command = data.get("command")
    if typ != "command" or not isinstance(command, str) or not command:
        raise MalformedBenchmarkTaskError(
            f"Benchmark task {task_id!r} verification must be a command"
        )
    return BenchmarkVerification(type="command", command=command)


def _positive_int(mapping: dict[str, Any], key: str, task_id: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or value < 1:
        raise MalformedBenchmarkTaskError(
            f"Benchmark task {task_id!r} limit {key!r} must be a positive integer"
        )
    return value


def _parse_limits(value: object, task_id: str) -> BenchmarkLimits:
    if not isinstance(value, dict):
        raise MalformedBenchmarkTaskError(f"Benchmark task {task_id!r} has invalid limits")
    data = cast(dict[str, Any], value)
    return BenchmarkLimits(
        timeout_seconds=_positive_int(data, "timeout_seconds", task_id),
        max_steps=_positive_int(data, "max_steps", task_id),
    )


def materialize_workspace(task: BenchmarkTask, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in task.workspace.files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
