from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pythinker_code.benchmark.errors import BenchmarkSyntaxError, UnknownBenchmarkModelError
from pythinker_code.benchmark.estimate import estimate_benchmark as build_estimate
from pythinker_code.benchmark.estimate import render_estimate
from pythinker_code.benchmark.records import BenchmarkRecorder, load_run
from pythinker_code.benchmark.report import render_show
from pythinker_code.benchmark.runner import run_task
from pythinker_code.benchmark.suites import list_suite_names, load_suite
from pythinker_code.benchmark.tasks import list_task_ids, load_task
from pythinker_code.share import get_share_dir

if TYPE_CHECKING:
    from pythinker_code.soul.pythinkersoul import PythinkerSoul


DEFAULT_SUITE = "pythinker-core"
DEFAULT_SANDBOX = "current-pythinker-approval-runtime"


@dataclass(frozen=True, slots=True)
class BenchmarkArgs:
    subcommand: str
    model: str | None = None
    task: str | None = None
    suite: str | None = None
    repeat: int = 1
    max_concurrency: int = 1
    timeout_seconds: int | None = None
    judges: str = "off"
    sandbox: str = DEFAULT_SANDBOX
    output: Path | None = None
    run_id: str | None = None


def benchmark_usage() -> str:
    return "\n".join(
        [
            "Usage:",
            "  /benchmark start [--model <model-key>] [--task <task-id> | --suite <suite-name>]",
            "  /benchmark estimate [--model <model-key>] [--task <task-id> | --suite <suite-name>]",
            "  /benchmark list",
            "  /benchmark show <run-id>",
            "  /benchmark report [--suite <suite-name>]",
        ]
    )


def parse_args(args: str) -> BenchmarkArgs:
    try:
        tokens = shlex.split(args)
    except ValueError as exc:
        raise BenchmarkSyntaxError(str(exc)) from exc
    if not tokens:
        raise BenchmarkSyntaxError(benchmark_usage())
    subcommand = tokens.pop(0)
    if subcommand not in {"start", "estimate", "list", "show", "report"}:
        raise BenchmarkSyntaxError(
            f"Unknown benchmark subcommand: {subcommand}\n{benchmark_usage()}"
        )
    values: dict[str, object] = {"subcommand": subcommand}
    positional: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("--"):
            positional.append(token)
            i += 1
            continue
        key = token.removeprefix("--").replace("-", "_")
        if key not in {
            "model",
            "task",
            "suite",
            "repeat",
            "max_concurrency",
            "timeout_seconds",
            "judges",
            "sandbox",
            "output",
        }:
            raise BenchmarkSyntaxError(f"Unknown benchmark flag: {token}\n{benchmark_usage()}")
        if i + 1 >= len(tokens):
            raise BenchmarkSyntaxError(f"Missing value for {token}\n{benchmark_usage()}")
        values[key] = tokens[i + 1]
        i += 2
    if subcommand == "show":
        if len(positional) != 1:
            raise BenchmarkSyntaxError(benchmark_usage())
        values["run_id"] = positional[0]
    elif positional:
        raise BenchmarkSyntaxError(f"Unexpected benchmark argument: {positional[0]}")
    return _coerce_args(values)


def _coerce_args(values: dict[str, object]) -> BenchmarkArgs:
    repeat = _positive_int(values.get("repeat", "1"), "--repeat")
    max_concurrency = _positive_int(values.get("max_concurrency", "1"), "--max-concurrency")
    timeout = values.get("timeout_seconds")
    timeout_seconds = _positive_int(timeout, "--timeout-seconds") if timeout is not None else None
    task = _optional_str(values.get("task"))
    suite = _optional_str(values.get("suite"))
    if task is not None and suite is not None:
        raise BenchmarkSyntaxError("--task and --suite are mutually exclusive")
    judges = str(values.get("judges", "off"))
    if judges != "off":
        raise BenchmarkSyntaxError("--judges only supports 'off' in v1")
    sandbox = str(values.get("sandbox", DEFAULT_SANDBOX))
    if sandbox != DEFAULT_SANDBOX:
        raise BenchmarkSyntaxError(
            "--sandbox only supports current-pythinker-approval-runtime in v1"
        )
    if max_concurrency > 1:
        raise BenchmarkSyntaxError("--max-concurrency > 1 is not supported in v1")
    output = Path(str(values["output"])).expanduser() if values.get("output") else None
    return BenchmarkArgs(
        subcommand=str(values["subcommand"]),
        model=_optional_str(values.get("model")),
        task=task,
        suite=suite,
        repeat=repeat,
        max_concurrency=max_concurrency,
        timeout_seconds=timeout_seconds,
        judges=judges,
        sandbox=sandbox,
        output=output,
        run_id=_optional_str(values.get("run_id")),
    )


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _positive_int(value: object, flag: str) -> int:
    try:
        parsed = int(str(value))
    except ValueError as exc:
        raise BenchmarkSyntaxError(f"{flag} must be an integer") from exc
    if parsed < 1:
        raise BenchmarkSyntaxError(f"{flag} must be >= 1")
    return parsed


async def dispatch_benchmark(soul: PythinkerSoul, args: str) -> str:
    parsed = parse_args(args)
    if parsed.subcommand == "list":
        return list_benchmarks()
    if parsed.subcommand == "estimate":
        return estimate_benchmark(soul, parsed)
    if parsed.subcommand == "show":
        assert parsed.run_id is not None
        return show_benchmark(parsed.run_id, parsed.output)
    if parsed.subcommand == "report":
        return render_benchmark_report(parsed.output, parsed.suite)
    if parsed.subcommand == "start":
        return await start_benchmark(soul, parsed, raw_args=args)
    raise BenchmarkSyntaxError(benchmark_usage())


def list_benchmarks() -> str:
    suites = ", ".join(list_suite_names())
    tasks = ", ".join(list_task_ids())
    return f"Pythinker Benchmark\n\nSuites: {suites}\nTasks: {tasks}"


def estimate_benchmark(soul: PythinkerSoul, args: BenchmarkArgs) -> str:
    model_key = _resolve_model_key(soul, args.model)
    estimate = build_estimate(
        model_key=model_key,
        task_id=args.task,
        suite_name=args.suite or (None if args.task else DEFAULT_SUITE),
        repeat=args.repeat,
    )
    return render_estimate(estimate)


async def start_benchmark(soul: PythinkerSoul, args: BenchmarkArgs, *, raw_args: str) -> str:
    model_key = _resolve_model_key(soul, args.model)
    root = args.output or get_share_dir() / "benchmarks"
    run_summaries: list[str] = []
    suite_name = args.suite or (None if args.task else DEFAULT_SUITE)
    task_ids = [args.task] if args.task else load_suite(suite_name or DEFAULT_SUITE).tasks
    for repeat_index in range(1, args.repeat + 1):
        for task_id in task_ids:
            assert task_id is not None
            task = load_task(task_id)
            run_id = _run_id(task.id)
            recorder = BenchmarkRecorder(root, run_id)
            result = await run_task(
                soul=soul,
                task=task,
                recorder=recorder,
                model_key=model_key,
                command=f"/benchmark {raw_args}",
                suite_name=suite_name,
                repeat_index=repeat_index,
                timeout_seconds=args.timeout_seconds,
            )
            run_summaries.append(
                "\n".join(
                    [
                        "Pythinker Benchmark finished.",
                        "",
                        f"- Run: {run_id}",
                        f"- Status: {result.status}",
                        f"- Duration: {result.duration_ms / 1000:.1f}s",
                        f"- Steps: {result.steps}",
                        f"- Tool calls: {result.tool_calls}",
                        "- Changed files: "
                        + (", ".join(result.changed_files) if result.changed_files else "(none)"),
                        "- Estimated cost: unavailable",
                        f"- Report: {recorder.run_dir / 'report.md'}",
                    ]
                )
            )
    return "\n\n".join(run_summaries)


def show_benchmark(run_id: str, output: Path | None = None) -> str:
    root = output or get_share_dir() / "benchmarks"
    run, summary = load_run(root, run_id)
    return render_show(run, summary)


def render_benchmark_report(output: Path | None = None, suite: str | None = None) -> str:
    root = output or get_share_dir() / "benchmarks"
    if not root.exists():
        return "Pythinker Benchmark\n\nNo benchmark runs found."
    rows: list[dict[str, object]] = []
    for path in sorted(root.glob("*/run.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        if suite is None or run.get("suite_name") == suite:
            rows.append(run)
    if not rows:
        return "Pythinker Benchmark\n\nNo matching benchmark runs found."
    lines = ["Pythinker Benchmark report", ""]
    for row in rows:
        lines.append(f"- {row.get('run_id')}: {row.get('status')} ({row.get('model_key')})")
    return "\n".join(lines)


def _validate_model(soul: PythinkerSoul, model_key: str) -> None:
    if model_key not in soul.runtime.config.models:
        raise UnknownBenchmarkModelError(f"Unknown benchmark model: {model_key}")


def _resolve_model_key(soul: PythinkerSoul, requested_model: str | None) -> str:
    if requested_model is not None:
        _validate_model(soul, requested_model)
        return requested_model
    config = soul.runtime.config
    active_model = soul.runtime.llm.model_config if soul.runtime.llm else None
    if active_model is not None:
        for model_key, model_config in config.models.items():
            if model_config == active_model:
                return model_key
    if config.default_model:
        _validate_model(soul, config.default_model)
        return config.default_model
    raise UnknownBenchmarkModelError(
        "No active benchmark model. Select a Pythinker model or pass --model <model-key>."
    )


def _run_id(task_id: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    return f"bench_{stamp}_{task_id.replace('-', '_')}"
