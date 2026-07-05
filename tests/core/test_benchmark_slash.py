from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from pythinker_core.tooling.empty import EmptyToolset

from pythinker_code.benchmark.commands import BenchmarkArgs, start_benchmark
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


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[TextPart]:
    captured: list[TextPart] = []
    monkeypatch.setattr("pythinker_code.soul.slash.wire_send", lambda msg: captured.append(msg))
    return captured


async def test_benchmark_command_registered(runtime: Runtime, tmp_path: Path) -> None:
    soul = _make_soul(runtime, tmp_path)

    names = {cmd.name for cmd in soul.available_slash_commands}

    assert "benchmark" in names
    assert soul_slash_registry.find_command("benchmark") is not None


async def test_benchmark_list_shows_bundled_suite(
    runtime: Runtime, tmp_path: Path, sent: list[TextPart]
) -> None:
    soul = _make_soul(runtime, tmp_path)

    await _run(soul, "list")

    text = "\n".join(part.text for part in sent)
    assert "Pythinker Benchmark" in text
    assert "pythinker-smoke" in text
    assert "smoke-edit-readme" in text


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
        )

    monkeypatch.setattr("pythinker_code.benchmark.commands.run_task", fake_run_task)

    await start_benchmark(
        soul,
        BenchmarkArgs(subcommand="start", output=tmp_path / "runs"),
        raw_args="start",
    )

    assert seen_suite_names == ["pythinker-smoke", "pythinker-smoke", "pythinker-smoke"]
