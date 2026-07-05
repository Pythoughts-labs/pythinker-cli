from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pythinker_code.benchmark.errors import MalformedBenchmarkTaskError
from pythinker_code.benchmark.tasks import (
    BenchmarkLimits,
    BenchmarkTask,
    BenchmarkVerification,
    BenchmarkWorkspace,
)


@dataclass(frozen=True, slots=True)
class SweBenchmarkInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    workspace_files: dict[str, str]
    verification_command: str
    timeout_seconds: int
    max_steps: int


def load_swe_instances(path: Path) -> list[SweBenchmarkInstance]:
    if not path.exists():
        raise MalformedBenchmarkTaskError(f"SWE benchmark dataset does not exist: {path}")
    instances: list[SweBenchmarkInstance] = []
    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw: object = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise MalformedBenchmarkTaskError(
                    f"Malformed SWE benchmark JSONL at line {line_number}: {path}"
                ) from exc
            if not isinstance(raw, dict):
                raise MalformedBenchmarkTaskError(
                    f"SWE benchmark line {line_number} must be an object: {path}"
                )
            instances.append(_parse_swe_instance(cast(dict[str, object], raw), line_number, path))
    if not instances:
        raise MalformedBenchmarkTaskError(f"SWE benchmark dataset is empty: {path}")
    return instances


def swe_instance_to_task(instance: SweBenchmarkInstance) -> BenchmarkTask:
    return BenchmarkTask(
        id=f"swe-{instance.instance_id}",
        title=instance.instance_id,
        description=f"SWE-style task from {instance.repo} at {instance.base_commit}",
        prompt=_prompt(instance),
        workspace=BenchmarkWorkspace(files=instance.workspace_files),
        verification=BenchmarkVerification(type="command", command=instance.verification_command),
        limits=BenchmarkLimits(
            timeout_seconds=instance.timeout_seconds,
            max_steps=instance.max_steps,
        ),
        tags=["swe", "offline", "deterministic"],
    )


def _parse_swe_instance(
    raw: dict[str, object], line_number: int, path: Path
) -> SweBenchmarkInstance:
    instance_id = _required_str(raw, "instance_id", line_number, path)
    repo = _required_str(raw, "repo", line_number, path)
    base_commit = _required_str(raw, "base_commit", line_number, path)
    problem_statement = _required_str(raw, "problem_statement", line_number, path)
    workspace_files = _workspace_files(raw, line_number, path)
    verification_command = _verification_command(raw, line_number, path)
    limits = raw.get("limits")
    limits_data = cast(dict[str, object], limits) if isinstance(limits, dict) else {}
    return SweBenchmarkInstance(
        instance_id=instance_id,
        repo=repo,
        base_commit=base_commit,
        problem_statement=problem_statement,
        fail_to_pass=_string_list(raw.get("FAIL_TO_PASS")),
        pass_to_pass=_string_list(raw.get("PASS_TO_PASS")),
        workspace_files=workspace_files,
        verification_command=verification_command,
        timeout_seconds=_positive_int(limits_data.get("timeout_seconds"), default=900),
        max_steps=_positive_int(limits_data.get("max_steps"), default=80),
    )


def _required_str(raw: dict[str, object], key: str, line_number: int, path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise MalformedBenchmarkTaskError(
            f"SWE benchmark line {line_number} missing string field {key!r}: {path}"
        )
    return value


def _workspace_files(raw: dict[str, object], line_number: int, path: Path) -> dict[str, str]:
    workspace = raw.get("workspace")
    if not isinstance(workspace, dict):
        raise MalformedBenchmarkTaskError(
            f"SWE benchmark line {line_number} needs local workspace.files: {path}"
        )
    files = cast(dict[str, object], workspace).get("files")
    if not isinstance(files, dict) or not files:
        raise MalformedBenchmarkTaskError(
            f"SWE benchmark line {line_number} workspace.files must be non-empty: {path}"
        )
    parsed: dict[str, str] = {}
    for name, content in cast(dict[object, object], files).items():
        if (
            not isinstance(name, str)
            or not name
            or name.startswith("/")
            or ".." in Path(name).parts
        ):
            raise MalformedBenchmarkTaskError(
                f"SWE benchmark line {line_number} has unsafe workspace path: {path}"
            )
        if not isinstance(content, str):
            raise MalformedBenchmarkTaskError(
                f"SWE benchmark line {line_number} file {name!r} must contain text: {path}"
            )
        parsed[name] = content
    return parsed


def _verification_command(raw: dict[str, object], line_number: int, path: Path) -> str:
    verification = raw.get("verification")
    if not isinstance(verification, dict):
        raise MalformedBenchmarkTaskError(
            f"SWE benchmark line {line_number} needs local verification command: {path}"
        )
    command = cast(dict[str, object], verification).get("command")
    if cast(dict[str, object], verification).get("type") != "command" or not isinstance(
        command, str
    ):
        raise MalformedBenchmarkTaskError(
            f"SWE benchmark line {line_number} verification must be a command: {path}"
        )
    return command


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in cast(list[object], value) if isinstance(item, str)]


def _positive_int(value: object, *, default: int) -> int:
    return value if isinstance(value, int) and value > 0 else default


def _prompt(instance: SweBenchmarkInstance) -> str:
    fail_to_pass = "\n".join(f"- {test}" for test in instance.fail_to_pass) or "- (none)"
    pass_to_pass = "\n".join(f"- {test}" for test in instance.pass_to_pass) or "- (none)"
    return "\n".join(
        [
            f"Resolve SWE-style instance {instance.instance_id}.",
            "",
            f"Repository: {instance.repo}",
            f"Base commit: {instance.base_commit}",
            "",
            "Problem statement:",
            instance.problem_statement,
            "",
            "FAIL_TO_PASS tests:",
            fail_to_pass,
            "",
            "PASS_TO_PASS tests:",
            pass_to_pass,
        ]
    )
