from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from pythinker_core.message import Message
from pythinker_core.tooling.empty import EmptyToolset

from pythinker_code.benchmark.records import BenchmarkRecorder
from pythinker_code.benchmark.runner import run_task
from pythinker_code.benchmark.tasks import BenchmarkLimits, load_task
from pythinker_code.config import LLMModel, LLMProvider
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul


def _make_soul(runtime: Runtime, tmp_path: Path) -> PythinkerSoul:
    runtime.config.providers["mock"] = LLMProvider(
        type="pythinker", base_url="", api_key=SecretStr("")
    )
    runtime.config.models["mock-model"] = LLMModel(
        provider="mock", model="mock", max_context_size=100_000
    )
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    return PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))


async def test_run_task_records_passed_smoke_task(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _make_soul(runtime, tmp_path)

    async def fake_turn(message: Message):
        workspace = Path(str(soul.runtime.work_dir))
        prompt = message.extract_text(" ")
        assert "Pythinker Benchmark workspace:" in prompt
        assert str(workspace) in prompt
        wire_path = Path(str(soul.runtime.session.wire_file.path))
        wire_path.parent.mkdir(parents=True, exist_ok=True)
        with wire_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "message": {
                            "type": "ToolCall",
                            "payload": {"id": "call-1", "name": "Bash"},
                        }
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "message": {
                            "type": "ToolExecutionStarted",
                            "payload": {"tool_call_id": "call-1"},
                        }
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "message": {
                            "type": "ToolCall",
                            "payload": {"id": "call-2", "name": "StrReplaceFile"},
                        }
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "message": {
                            "type": "ToolExecutionStarted",
                            "payload": {"tool_call_id": "call-2"},
                        }
                    }
                )
                + "\n"
            )
            f.write(
                json.dumps(
                    {
                        "message": {
                            "type": "StatusUpdate",
                            "payload": {
                                "token_usage": {
                                    "input_other": 10,
                                    "input_cache_read": 20,
                                    "input_cache_creation": 5,
                                    "output": 7,
                                },
                            },
                        }
                    }
                )
                + "\n"
            )
        (workspace / "README.md").write_text(
            "# Example Project\n\nPythinker benchmark smoke test\n",
            encoding="utf-8",
        )
        return type("Outcome", (), {"step_count": 1, "final_message": message})()

    soul.turn = AsyncMock(side_effect=fake_turn)  # type: ignore[method-assign]
    recorder = BenchmarkRecorder(tmp_path / "runs", "bench_test")

    result = await run_task(
        soul=soul,
        task=load_task("smoke-edit-readme"),
        recorder=recorder,
        model_key="mock-model",
        command="/benchmark start --model mock-model --task smoke-edit-readme",
    )

    assert result.status == "passed"
    assert result.tool_calls == 2
    assert result.activity["tool_calls_by_name"] == {"Bash": 1, "StrReplaceFile": 1}
    assert result.changed_files == ["README.md"]
    assert result.input_tokens == 35
    assert result.output_tokens == 7
    assert (tmp_path / "runs" / "bench_test" / "summary.json").exists()
    summary = json.loads(
        (tmp_path / "runs" / "bench_test" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["activity"]["tool_calls_by_name"] == {"Bash": 1, "StrReplaceFile": 1}


async def test_run_task_records_failed_verification(runtime: Runtime, tmp_path: Path) -> None:
    soul = _make_soul(runtime, tmp_path)
    soul.turn = AsyncMock(  # type: ignore[method-assign]
        return_value=type("Outcome", (), {"step_count": 1, "final_message": None})()
    )
    recorder = BenchmarkRecorder(tmp_path / "runs", "bench_test")

    result = await run_task(
        soul=soul,
        task=load_task("smoke-edit-readme"),
        recorder=recorder,
        model_key="mock-model",
        command="/benchmark start --model mock-model --task smoke-edit-readme",
    )

    assert result.status == "failed_verification"
    assert result.verification.exit_code != 0


async def test_run_task_changed_files_ignore_verification_artifacts(
    runtime: Runtime, tmp_path: Path
) -> None:
    soul = _make_soul(runtime, tmp_path)

    async def fake_turn(message: Message):
        workspace = Path(str(soul.runtime.work_dir))
        (workspace / "strings.py").write_text(
            "def slugify(text):\n    return text.strip().lower().replace(' ', '-')\n",
            encoding="utf-8",
        )
        return type("Outcome", (), {"step_count": 1, "final_message": message})()

    soul.turn = AsyncMock(side_effect=fake_turn)  # type: ignore[method-assign]
    recorder = BenchmarkRecorder(tmp_path / "runs", "bench_test")

    result = await run_task(
        soul=soul,
        task=load_task("smoke-add-small-function"),
        recorder=recorder,
        model_key="mock-model",
        command="/benchmark start --task smoke-add-small-function",
    )

    assert result.status == "passed"
    assert result.changed_files == ["strings.py"]


async def test_run_task_applies_and_restores_task_step_limit(
    runtime: Runtime, tmp_path: Path
) -> None:
    soul = _make_soul(runtime, tmp_path)
    original_limit = soul._loop_control.max_steps_per_turn  # pyright: ignore[reportPrivateUsage]
    seen_limits: list[int] = []

    async def fake_turn(message: Message):
        seen_limits.append(
            soul._loop_control.max_steps_per_turn  # pyright: ignore[reportPrivateUsage]
        )
        workspace = Path(str(soul.runtime.work_dir))
        (workspace / "strings.py").write_text(
            "def slugify(text):\n    return text.strip().lower().replace(' ', '-')\n",
            encoding="utf-8",
        )
        return type("Outcome", (), {"step_count": 1, "final_message": message})()

    soul.turn = AsyncMock(side_effect=fake_turn)  # type: ignore[method-assign]
    task = load_task("smoke-add-small-function")
    task = dataclasses.replace(task, limits=BenchmarkLimits(timeout_seconds=120, max_steps=2))
    recorder = BenchmarkRecorder(tmp_path / "runs", "bench_test")

    result = await run_task(
        soul=soul,
        task=task,
        recorder=recorder,
        model_key="mock-model",
        command="/benchmark start --task smoke-add-small-function",
    )

    assert result.status == "passed"
    assert seen_limits == [2]
    assert soul._loop_control.max_steps_per_turn == original_limit  # pyright: ignore[reportPrivateUsage]


async def test_run_task_records_timeout_override_for_environment(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _make_soul(runtime, tmp_path)
    soul.turn = AsyncMock(  # type: ignore[method-assign]
        return_value=type("Outcome", (), {"step_count": 1, "final_message": None})()
    )
    recorder = BenchmarkRecorder(tmp_path / "runs", "bench_test")

    monkeypatch.setattr(
        "pythinker_code.benchmark.runner.collect_benchmark_environment",
        lambda *_, task_timeout_seconds, **__: {"task_timeout_seconds": task_timeout_seconds},
    )

    result = await run_task(
        soul=soul,
        task=load_task("smoke-edit-readme"),
        recorder=recorder,
        model_key="mock-model",
        command="/benchmark start --task smoke-edit-readme",
        timeout_seconds=5,
    )

    assert result.environment["task_timeout_seconds"] == 5


async def test_run_task_restores_runtime_when_setup_mutation_fails(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _make_soul(runtime, tmp_path)
    original_work_dir = runtime.work_dir
    original_builtin_args = runtime.builtin_args
    original_loop_limit = soul._loop_control.max_steps_per_turn  # pyright: ignore[reportPrivateUsage]
    recorder = BenchmarkRecorder(tmp_path / "runs", "bench_test")

    def fail_replace(*args: object, **kwargs: object) -> object:
        raise TypeError("boom")

    monkeypatch.setattr("pythinker_code.benchmark.runner.dataclasses.replace", fail_replace)

    result = await run_task(
        soul=soul,
        task=load_task("smoke-edit-readme"),
        recorder=recorder,
        model_key="mock-model",
        command="/benchmark start --task smoke-edit-readme",
    )

    assert result.status == "internal_benchmark_error"
    assert runtime.work_dir == original_work_dir
    assert runtime.builtin_args == original_builtin_args
    assert soul._loop_control.max_steps_per_turn == original_loop_limit  # pyright: ignore[reportPrivateUsage]


def test_run_verification_does_not_use_login_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code.benchmark import runner

    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object):
        captured.append(cmd)
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner._run_verification(load_task("smoke-edit-readme"), tmp_path)  # pyright: ignore[reportPrivateUsage]

    assert result.status == "passed"
    assert captured
    assert captured[0][1] == "-c"
