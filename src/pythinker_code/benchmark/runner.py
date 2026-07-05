from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pythinker_core.message import Message
from pythinker_host.path import HostPath

from pythinker_code.benchmark.records import BenchmarkRecorder
from pythinker_code.benchmark.tasks import BenchmarkTask, materialize_workspace

if TYPE_CHECKING:
    from pythinker_code.soul.pythinkersoul import PythinkerSoul


_GENERATED_DIRS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}
_GENERATED_SUFFIXES = {".pyc", ".pyo"}


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: str
    type: str
    exit_code: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    run_id: str
    status: str
    exit_reason: str
    final_answer: str
    verification: VerificationResult
    duration_ms: int
    steps: int
    tool_calls: int
    changed_files: list[str]
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    estimated_cost_usd: float | None


async def run_task(
    *,
    soul: PythinkerSoul,
    task: BenchmarkTask,
    recorder: BenchmarkRecorder,
    model_key: str,
    command: str,
    suite_name: str | None = None,
    repeat_index: int = 1,
    timeout_seconds: int | None = None,
) -> BenchmarkResult:
    runtime = soul.runtime  # type: ignore[attr-defined]
    model = runtime.config.models[model_key]
    provider_key = model.provider
    recorder.start_run(
        command=command,
        model_key=model_key,
        provider_key=provider_key,
        task_id=task.id,
        suite_name=suite_name,
        repeat_index=repeat_index,
    )
    workspace = recorder.workspace_dir
    materialize_workspace(task, workspace)
    recorder.record_event("workspace_prepared", {"workspace": str(workspace)})

    before = _snapshot_files(workspace)
    started = time.monotonic()
    context_file = Path(str(runtime.session.context_file))
    wire_file = Path(str(runtime.session.wire_file.path))
    context_offset = _file_size(context_file)
    wire_offset = _file_size(wire_file)
    steps = 0
    final_answer = ""
    status = "internal_benchmark_error"
    exit_reason = "internal_error"
    cancelled = False
    verification = VerificationResult(
        status="not_run",
        type=task.verification.type,
        exit_code=None,
        stdout="",
        stderr="",
    )
    old_override = runtime.work_dir_override
    old_builtin_args = runtime.builtin_args
    benchmark_work_dir = HostPath.unsafe_from_local_path(workspace)
    _set_work_dir_override(soul, benchmark_work_dir)
    runtime.builtin_args = dataclasses.replace(
        runtime.builtin_args,
        PYTHINKER_WORK_DIR=benchmark_work_dir,
        PYTHINKER_WORK_DIR_LS="",
        PYTHINKER_AGENTS_MD="",
    )
    try:
        recorder.record_event("user_message", {"content": task.prompt})
        try:
            outcome = await asyncio.wait_for(
                soul.turn(Message(role="user", content=task.prompt)),  # type: ignore[attr-defined]
                timeout=timeout_seconds or task.limits.timeout_seconds,
            )
        except TimeoutError:
            status = "timeout"
            exit_reason = "timeout"
            verification = VerificationResult(
                status="not_run",
                type=task.verification.type,
                exit_code=None,
                stdout="",
                stderr="",
            )
        except asyncio.CancelledError:
            status = "cancelled"
            exit_reason = "cancelled"
            cancelled = True
        except Exception as exc:
            status = "model_provider_error"
            exit_reason = str(exc)
            recorder.record_event("tool_call_failed", {"error": str(exc)})
        else:
            steps = int(getattr(outcome, "step_count", getattr(outcome, "n_steps", 0)) or 0)
            final_message = getattr(
                outcome,
                "final_message",
                getattr(outcome, "final_assistant_message", None),
            )
            if final_message is not None and hasattr(final_message, "extract_text"):
                final_answer = final_message.extract_text(" ")
            recorder.record_event("model_message", {"content": final_answer})

            verification = await asyncio.to_thread(_run_verification, task, workspace)
            recorder.record_event(
                "verification_finished",
                {
                    "status": verification.status,
                    "exit_code": verification.exit_code,
                    "stdout": verification.stdout,
                    "stderr": verification.stderr,
                },
            )
            if verification.status == "passed":
                status = "passed"
                exit_reason = "verification_passed"
            elif verification.status == "timeout":
                status = "timeout"
                exit_reason = "verification timed out"
            else:
                status = "failed_verification"
                exit_reason = f"verification command exited with code {verification.exit_code}"
    except Exception as exc:
        status = "internal_benchmark_error"
        exit_reason = str(exc)
        recorder.record_event(
            "status",
            {"status": status, "error": str(exc)},
        )
    finally:
        _set_work_dir_override(soul, old_override)
        runtime.builtin_args = old_builtin_args

    changed_files = _changed_files(workspace, before)
    tool_calls = _count_wire_tool_calls(wire_file, wire_offset)
    result = BenchmarkResult(
        run_id=recorder.run_id,
        status=status,
        exit_reason=exit_reason,
        final_answer=final_answer,
        verification=verification,
        duration_ms=int((time.monotonic() - started) * 1000),
        steps=steps,
        tool_calls=tool_calls,
        changed_files=changed_files,
        input_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        estimated_cost_usd=None,
    )
    recorder.copy_context_and_wire(
        context_file,
        wire_file,
        context_offset=context_offset,
        wire_offset=wire_offset,
    )
    recorder.finish_run(result)
    if cancelled:
        raise asyncio.CancelledError()
    return result


def _run_verification(task: BenchmarkTask, workspace: Path) -> VerificationResult:
    try:
        completed = subprocess.run(
            ["/bin/bash", "-lc", task.verification.command],
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=task.limits.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return VerificationResult(
            status="timeout",
            type=task.verification.type,
            exit_code=None,
            stdout=_timeout_output(exc.stdout),
            stderr=_timeout_output(exc.stderr),
        )
    return VerificationResult(
        status="passed" if completed.returncode == 0 else "failed",
        type=task.verification.type,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(encoding="utf-8", errors="replace")
    return value


def _set_work_dir_override(soul: PythinkerSoul, work_dir: HostPath | None) -> None:
    setter = getattr(soul.agent.toolset, "set_work_dir_override", None)
    if callable(setter):
        setter(work_dir)
    else:
        soul.runtime.work_dir_override = work_dir


def _count_wire_tool_calls(wire_file: Path, offset: int) -> int:
    if not wire_file.exists():
        return 0
    tool_call_ids: set[str] = set()
    try:
        with wire_file.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            for line in f:
                try:
                    raw_record: object = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw_record, dict):
                    continue
                record = cast(dict[str, object], raw_record)
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                message_data = cast(dict[str, object], message)
                message_type = message_data.get("type")
                payload = message_data.get("payload")
                if not isinstance(payload, dict):
                    continue
                payload_data = cast(dict[str, object], payload)
                if message_type == "ToolCall":
                    tool_id = payload_data.get("id")
                elif message_type == "ToolExecutionStarted":
                    tool_id = payload_data.get("tool_call_id")
                else:
                    tool_id = None
                if isinstance(tool_id, str):
                    tool_call_ids.add(tool_id)
    except OSError:
        return 0
    return len(tool_call_ids)


def _snapshot_files(workspace: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in workspace.rglob("*"):
        if path.is_file():
            rel = path.relative_to(workspace).as_posix()
            if _is_generated_artifact(rel):
                continue
            snapshot[rel] = path.read_text(encoding="utf-8", errors="replace")
    return snapshot


def _changed_files(workspace: Path, before: dict[str, str]) -> list[str]:
    changed: list[str] = []
    after = _snapshot_files(workspace)
    for name, content in sorted(after.items()):
        if before.get(name) != content:
            changed.append(name)
    for name in sorted(set(before) - set(after)):
        changed.append(name)
    return changed


def _is_generated_artifact(path: str) -> bool:
    rel = Path(path)
    if any(part in _GENERATED_DIRS for part in rel.parts):
        return True
    return rel.suffix in _GENERATED_SUFFIXES
