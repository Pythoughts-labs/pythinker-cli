from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from pythinker_core.tooling.empty import EmptyToolset

from pythinker_code.benchmark.commands import (
    BenchmarkArgs,
    BenchmarkSyntaxError,
    _run_id,
    benchmark_usage,
    parse_args,
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
        _ = await result


async def _run_registered(soul: PythinkerSoul, name: str, args: str = "") -> None:
    command = soul_slash_registry.find_command(name)
    assert command is not None
    result = command.func(soul, args)
    if result is not None:
        _ = await result


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
    assert "benchmark:compare" in names
    assert "benchmark:swe" in names
    assert soul_slash_registry.find_command("benchmark") is not None
    assert soul_slash_registry.find_command("benchmark:start") is not None
    assert soul_slash_registry.find_command("benchmark:compare") is not None
    assert soul_slash_registry.find_command("benchmark:swe") is not None


def test_benchmark_usage_documents_trusted_swe_dataset_gate() -> None:
    usage = benchmark_usage()

    assert "/benchmark swe --dataset <path.jsonl> --trusted-dataset true" in usage
    assert "/benchmark:swe --dataset <path.jsonl> --trusted-dataset true" in usage


def test_parse_compare_models() -> None:
    args = parse_args("compare --models model-a,model-b --suite pythinker-core --repeat 2")

    assert args.subcommand == "compare"
    assert args.models == ["model-a", "model-b"]
    assert args.suite == "pythinker-core"
    assert args.repeat == 2


def test_parse_compare_requires_two_models() -> None:
    with pytest.raises(BenchmarkSyntaxError, match="at least two models"):
        parse_args("compare --models model-a")


def test_parse_compare_requires_two_distinct_models() -> None:
    with pytest.raises(BenchmarkSyntaxError, match="at least two distinct models"):
        parse_args("compare --models model-a,model-a")


def test_parse_export_args() -> None:
    args = parse_args("export --suite pythinker-core --format csv --output ~/benchmarks")

    assert args.subcommand == "export"
    assert args.suite == "pythinker-core"
    assert args.format == "csv"
    assert args.output == Path("~/benchmarks").expanduser()


def test_parse_export_rejects_invalid_format() -> None:
    with pytest.raises(BenchmarkSyntaxError, match="--format must be json or csv"):
        parse_args("export --format markdown")


def test_run_id_is_unique_when_clock_repeats(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDatetime:
        @classmethod
        def now(cls, tz: object) -> datetime:
            assert tz is UTC
            return datetime(2026, 7, 5, 12, 0, 0, 1, tzinfo=UTC)

    monkeypatch.setattr("pythinker_code.benchmark.commands.datetime", FixedDatetime)

    assert _run_id("same-task") != _run_id("same-task")


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


async def test_benchmark_export_reads_artifact_root(
    runtime: Runtime, tmp_path: Path, sent: list[TextPart]
) -> None:
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

    await _run(_make_soul(runtime, tmp_path), f"export --suite pythinker-core --output {tmp_path}")

    exported = json.loads("\n".join(part.text for part in sent))
    assert exported == [
        {
            "run_id": "bench_1",
            "model": "model-a",
            "task": "task-one",
            "repeat": 1,
            "status": "passed",
            "score": 0.0,
            "duration_ms": 1000,
            "steps": 4,
            "tool_calls": 2,
            "total_tokens": 100,
            "estimated_cost_usd": None,
            "added_lines": 0,
            "removed_lines": 0,
            "shell_tool_calls": 0,
        }
    ]


async def test_benchmark_compare_executes_each_requested_model(
    runtime: Runtime,
    tmp_path: Path,
    sent: list[TextPart],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.config.models["model-a"] = LLMModel(
        provider="mock", model="mock", max_context_size=100_000
    )
    runtime.config.models["model-b"] = LLMModel(
        provider="mock", model="mock", max_context_size=100_000
    )
    call_order: list[str] = []

    async def fake_run_task(*, model_key: str, **_: object) -> BenchmarkResult:
        call_order.append(model_key)
        return BenchmarkResult(
            run_id="stub",
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
    await _run(
        _make_soul(runtime, tmp_path),
        f"compare --models model-a,model-b --task smoke-edit-readme --output {tmp_path / 'runs'}",
    )
    text = "\n".join(part.text for part in sent)

    assert "Pythinker Benchmark finished." in text
    assert call_order == ["model-a", "model-b"]


async def test_benchmark_compare_invalid_model_prevalidation_prevents_run(
    runtime: Runtime, tmp_path: Path, sent: list[TextPart], monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _make_soul(runtime, tmp_path)
    runtime.config.models["model-a"] = LLMModel(
        provider="mock", model="mock", max_context_size=100_000
    )
    call_count = 0

    async def fake_run_task(**_: object) -> BenchmarkResult:
        nonlocal call_count
        call_count += 1
        return BenchmarkResult(
            run_id="stub",
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
    await _run(
        soul,
        f"compare --models model-a,unknown-model --task smoke-edit-readme "
        f"--output {tmp_path / 'runs'}",
    )

    text = "\n".join(part.text for part in sent)
    assert "Unknown benchmark model: unknown-model" in text
    assert call_count == 0


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


def test_benchmark_report_filters_run_ids_for_compare(tmp_path: Path) -> None:
    _write_run_summary(
        tmp_path,
        run_id="bench_compare",
        model="model-a",
        task="task-main",
        status="passed",
        duration_ms=1000,
        steps=4,
        tool_calls=1,
        total_tokens=100,
    )
    _write_run_summary(
        tmp_path,
        run_id="bench_other",
        model="model-z",
        task="task-other",
        status="failed_verification",
        duration_ms=500,
        steps=2,
        tool_calls=1,
        total_tokens=50,
    )

    report = render_benchmark_report(tmp_path, suite="pythinker-core", run_ids=["bench_compare"])

    assert "Runs: 1" in report
    assert "task-main" in report
    assert "task-other" not in report


def test_benchmark_report_run_id_filter_skips_runs_without_run_id(tmp_path: Path) -> None:
    _write_run_summary(
        tmp_path,
        run_id="bench_compare",
        model="model-a",
        task="task-main",
        status="passed",
        duration_ms=1000,
        steps=4,
        tool_calls=1,
        total_tokens=100,
    )
    legacy_dir = tmp_path / "legacy_run"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "run.json").write_text(
        (
            "{"
            '"suite_name": "pythinker-core", '
            '"task_id": "legacy-task", '
            '"model_key": "legacy-model", '
            '"status": "passed", '
            '"created_at": "2026-07-05T00:00:09+00:00"'
            "}\n"
        ),
        encoding="utf-8",
    )

    report = render_benchmark_report(tmp_path, suite="pythinker-core", run_ids=["bench_compare"])

    assert "Runs: 1" in report
    assert "task-main" in report
    assert "legacy-task" not in report


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
