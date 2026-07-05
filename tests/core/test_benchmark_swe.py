from __future__ import annotations

import json
from pathlib import Path

from pydantic import SecretStr
from pythinker_core.tooling.empty import EmptyToolset

from pythinker_code.benchmark.commands import BenchmarkArgs, start_swe_benchmark
from pythinker_code.benchmark.swe import load_swe_instances, swe_instance_to_task
from pythinker_code.config import LLMModel, LLMProvider
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul


def _write_swe_jsonl(path: Path) -> None:
    record = {
        "instance_id": "demo__project-1",
        "repo": "demo/project",
        "base_commit": "abc123",
        "problem_statement": "Fix add_one so it returns n + 1.",
        "FAIL_TO_PASS": ["test_math.py::test_add_one"],
        "PASS_TO_PASS": ["test_math.py::test_existing_behavior"],
        "workspace": {
            "files": {
                "mathlib.py": "def add_one(n):\n    return n\n",
                "test_math.py": (
                    "from mathlib import add_one\n\n"
                    "def test_add_one():\n"
                    "    assert add_one(2) == 3\n\n"
                    "def test_existing_behavior():\n"
                    "    assert add_one(0) == 1\n"
                ),
            }
        },
        "verification": {
            "type": "command",
            "command": "python -m pytest test_math.py -q",
        },
        "limits": {"timeout_seconds": 120, "max_steps": 40},
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


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
    return PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))


def test_load_swe_jsonl_instance_converts_to_benchmark_task(tmp_path: Path) -> None:
    dataset = tmp_path / "swe.jsonl"
    _write_swe_jsonl(dataset)

    instances = load_swe_instances(dataset)
    task = swe_instance_to_task(instances[0])

    assert [instance.instance_id for instance in instances] == ["demo__project-1"]
    assert task.id == "swe-demo__project-1"
    assert "Fix add_one" in task.prompt
    assert "FAIL_TO_PASS" in task.prompt
    assert task.workspace.files["mathlib.py"].startswith("def add_one")
    assert task.verification.command == "python -m pytest test_math.py -q"


async def test_start_swe_benchmark_runs_one_instance(
    runtime: Runtime, tmp_path: Path, monkeypatch
) -> None:
    dataset = tmp_path / "swe.jsonl"
    _write_swe_jsonl(dataset)
    soul = _make_soul(runtime, tmp_path)
    seen_tasks: list[str] = []

    async def fake_run_task(**kwargs):
        seen_tasks.append(kwargs["task"].id)
        return type(
            "Result",
            (),
            {
                "status": "passed",
                "duration_ms": 1000,
                "steps": 2,
                "tool_calls": 1,
                "changed_files": ["mathlib.py"],
            },
        )()

    monkeypatch.setattr("pythinker_code.benchmark.commands.run_task", fake_run_task)

    output = await start_swe_benchmark(
        soul,
        BenchmarkArgs(
            subcommand="swe",
            output=tmp_path / "runs",
            dataset=dataset,
            instance="demo__project-1",
            repeat=2,
        ),
        raw_args=f"swe --dataset {dataset} --instance demo__project-1",
    )

    assert seen_tasks == ["swe-demo__project-1", "swe-demo__project-1"]
    assert "Pythinker Benchmark finished." in output
    assert "- Task: demo__project-1" in output
    assert "- Changed files: mathlib.py" in output
