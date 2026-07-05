from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from pythinker_core.tooling.empty import EmptyToolset

from pythinker_code.benchmark.commands import (
    BenchmarkArgs,
    benchmark_usage,
    render_benchmark_report,
    start_benchmark,
)
from pythinker_code.benchmark.runner import BenchmarkResult, VerificationResult
from pythinker_code.config import LLMModel, LLMProvider
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.soul.slash import benchmark
from pythinker_code.soul.slash import registry as soul_slash_registry
from pythinker_code.wire.types import TextPart


def _make_soul(runtime: Runtime, tmp_path: Path) -> PythinkerSoul:
    runtime.config.providers["mock"] = LLMProvider(
        type="pythinker", base_url="", api_key=SecretStr("")
    )
    runtime.config.models["mock-model"] = LLMModel(
        provider="mock", model="mock", max_context_size=100_000
    )
    runtime.config.default_model = "mock-model"
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    soul._turn = AsyncMock(return_value=None)  # type: ignore[method-assign]
    return soul


async def _run(soul: PythinkerSoul, args: str) -> None:
    result = benchmark(soul, args)
    if result is not None:
        await result


async def _run_registered(soul: PythinkerSoul, name: str, args: str = "") -> None:
    command = soul_slash_registry.find_command(name)
    assert command is not None
    result = command.func(soul, args)
    if result is not None:
        await result


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[TextPart]:
    captured: list[TextPart] = []
    monkeypatch.setattr("pythinker_code.soul.slash.wire_send", lambda msg: captured.append(msg))
    return captured


async def test_benchmark_command_registered(runtime: Runtime, tmp_path: Path) -> None:
    soul = _make_soul(runtime, tmp_path)

    names = {cmd.name for cmd in soul.available_slash_commands}

    assert "benchmark" in names
    assert "benchmark:start" in names
    assert "benchmark:all" in names
    assert "benchmark:estimate" in names
    assert "benchmark:list" in names
    assert "benchmark:show" in names
    assert "benchmark:report" in names
    assert "benchmark:swe" in names
    assert soul_slash_registry.find_command("benchmark") is not None
    assert soul_slash_registry.find_command("benchmark:start") is not None
    assert soul_slash_registry.find_command("benchmark:swe") is not None


def test_benchmark_usage_documents_trusted_swe_dataset_gate() -> None:
    usage = benchmark_usage()

    assert "/benchmark swe --dataset <path.jsonl> --trusted-dataset true" in usage
    assert "/benchmark:swe --dataset <path.jsonl> --trusted-dataset true" in usage


async def test_benchmark_list_shows_bundled_suite(
    runtime: Runtime, tmp_path: Path, sent: list[TextPart]
) -> None:
    soul = _make_soul(runtime, tmp_path)

    await _run(soul, "list")

    text = "\n".join(part.text for part in sent)
    assert "Pythinker Benchmark" in text
    assert "pythinker-core" in text
    assert "pythinker-smoke" in text
    assert "smoke-edit-readme" in text


async def test_benchmark_namespaced_list_shows_bundled_suite(
    runtime: Runtime, tmp_path: Path, sent: list[TextPart]
) -> None:
    soul = _make_soul(runtime, tmp_path)

    await _run_registered(soul, "benchmark:list")

    text = "\n".join(part.text for part in sent)
    assert "Pythinker Benchmark" in text
    assert "pythinker-core" in text


async def test_benchmark_estimate_does_not_run_turn(
    runtime: Runtime, tmp_path: Path, sent: list[TextPart]
) -> None:
    soul = _make_soul(runtime, tmp_path)

    await _run(soul, "estimate --suite pythinker-smoke")

    text = "\n".join(part.text for part in sent)
    assert "Pythinker Benchmark estimate" in text
    assert "No model calls were made." in text
    turn_mock = soul._turn
    assert isinstance(turn_mock, AsyncMock)
    turn_mock.assert_not_awaited()


async def test_benchmark_start_without_model_uses_current_model(
    runtime: Runtime, tmp_path: Path, sent: list[TextPart]
) -> None:
    soul = _make_soul(runtime, tmp_path)

    await _run(soul, "estimate --task smoke-edit-readme")

    text = "\n".join(part.text for part in sent)
    assert "Model: mock-model" in text


async def test_benchmark_start_rejects_concurrency_gt_one(
    runtime: Runtime, tmp_path: Path, sent: list[TextPart]
) -> None:
    soul = _make_soul(runtime, tmp_path)

    await _run(soul, "start --model mock-model --suite pythinker-smoke --max-concurrency 2")

    assert any("--max-concurrency > 1 is not supported" in part.text for part in sent)


async def test_benchmark_start_default_suite_records_suite_name(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _make_soul(runtime, tmp_path)
    seen_suite_names: list[str | None] = []

    async def fake_run_task(**kwargs):
        seen_suite_names.append(kwargs["suite_name"])
        return BenchmarkResult(
            run_id=kwargs["recorder"].run_id,
            status="passed",
            exit_reason="verification_passed",
            final_answer="done",
            verification=VerificationResult(
                status="passed",
                type="command",
                exit_code=0,
                stdout="",
                stderr="",
            ),
            duration_ms=1,
            steps=1,
            tool_calls=0,
            changed_files=[],
            input_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
            estimated_cost_usd=None,
            activity={},
            environment={},
        )

    monkeypatch.setattr("pythinker_code.benchmark.commands.run_task", fake_run_task)

    output = await start_benchmark(
        soul,
        BenchmarkArgs(subcommand="start", output=tmp_path / "runs"),
        raw_args="start",
    )

    assert "- Run:" in output
    assert "\nRun:" not in output
    assert "- Report:" in output
    assert seen_suite_names == [
        "pythinker-core",
        "pythinker-core",
        "pythinker-core",
        "pythinker-core",
    ]


def test_benchmark_report_aggregates_by_model_and_task(tmp_path: Path) -> None:
    _write_run_summary(
        tmp_path,
        run_id="bench_1",
        model="model-a",
        task="task-one",
        status="passed",
        duration_ms=1000,
        steps=4,
        tool_calls=2,
        total_tokens=100,
    )
    _write_run_summary(
        tmp_path,
        run_id="bench_2",
        model="model-a",
        task="task-two",
        status="failed_verification",
        duration_ms=3000,
        steps=6,
        tool_calls=4,
        total_tokens=300,
    )
    _write_run_summary(
        tmp_path,
        run_id="bench_3",
        model="model-b",
        task="task-one",
        status="passed",
        duration_ms=2000,
        steps=2,
        tool_calls=1,
        total_tokens=50,
    )

    report = render_benchmark_report(tmp_path, suite="pythinker-core")

    assert "Runs: 3" in report
    assert "Passed: 2/3 (66.7%)" in report
    assert "- model-a: 1/2 passed (50.0%)" in report
    assert "- model-b: 1/1 passed (100.0%)" in report
    assert "- task-one: 2/2 passed (100.0%)" in report
    assert "- task-two: 0/1 passed (0.0%)" in report


def test_benchmark_report_includes_publishability_warnings(tmp_path: Path) -> None:
    _write_run_summary(
        tmp_path,
        run_id="bench_1",
        model="model-a",
        task="task-one",
        status="passed",
        duration_ms=1000,
        steps=4,
        tool_calls=2,
        total_tokens=100,
    )

    report = render_benchmark_report(tmp_path, suite="pythinker-core")

    assert "Publishability warnings:" in report
    assert "- Single model only: do not describe this as a model comparison." in report
    assert report.index("Publishability warnings:") < report.index("Models:")


def _write_run_summary(
    root: Path,
    *,
    run_id: str,
    model: str,
    task: str,
    status: str,
    duration_ms: int,
    steps: int,
    tool_calls: int,
    total_tokens: int,
) -> None:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        (
            "{"
            f'"run_id": "{run_id}", '
            '"suite_name": "pythinker-core", '
            f'"task_id": "{task}", '
            f'"model_key": "{model}", '
            f'"status": "{status}", '
            f'"created_at": "2026-07-05T00:00:0{run_id[-1]}+00:00"'
            "}\n"
        ),
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text(
        (
            "{"
            f'"status": "{status}", '
            f'"runtime": {{"duration_ms": {duration_ms}, "steps": {steps}, '
            f'"tool_calls": {tool_calls}}}, '
            f'"usage": {{"total_tokens": {total_tokens}}}'
            "}\n"
        ),
        encoding="utf-8",
    )
