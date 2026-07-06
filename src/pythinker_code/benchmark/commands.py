from __future__ import annotations

import dataclasses
import json
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from pythinker_code.benchmark.discovery import (
    DiscoveredBenchmarkTask,
    discover_benchmark_sources,
    quiz_fixture_from_discovery,
)
from pythinker_code.benchmark.errors import (
    BenchmarkInternalError,
    BenchmarkSyntaxError,
    UnknownBenchmarkModelError,
)
from pythinker_code.benchmark.estimate import estimate_benchmark as build_estimate
from pythinker_code.benchmark.estimate import render_estimate
from pythinker_code.benchmark.export import render_export
from pythinker_code.benchmark.records import BenchmarkRecorder, load_run
from pythinker_code.benchmark.report import render_show
from pythinker_code.benchmark.runner import run_task
from pythinker_code.benchmark.suites import list_suite_names, load_suite
from pythinker_code.benchmark.swe import load_swe_instances, swe_instance_to_task
from pythinker_code.benchmark.tasks import list_task_ids, load_task
from pythinker_code.benchmark.types import BenchmarkReportRow, JsonObject
from pythinker_code.share import get_share_dir

if TYPE_CHECKING:
    from pythinker_code.soul.pythinkersoul import PythinkerSoul


DEFAULT_SUITE = "pythinker-core"
DEFAULT_SANDBOX = "current-pythinker-approval-runtime"


@dataclass(frozen=True, slots=True)
class BenchmarkArgs:
    subcommand: str
    model: str | None = None
    models: list[str] | None = None
    task: str | None = None
    suite: str | None = None
    repeat: int = 1
    max_concurrency: int = 1
    timeout_seconds: int | None = None
    judges: str = "off"
    sandbox: str = DEFAULT_SANDBOX
    format: str = "json"
    output: Path | None = None
    dataset: Path | None = None
    instance: str | None = None
    trusted_dataset: bool = False
    run_id: str | None = None
    source: str | None = None
    difficulty: str = "hard"
    limit: int = 5


def benchmark_usage() -> str:
    return "\n".join(
        [
            "Usage:",
            "  /benchmark start [--model <model-key>] [--task <task-id> | --suite <suite-name>]",
            "  /benchmark:start [--model <model-key>] [--task <task-id> | --suite <suite-name>]",
            "  /benchmark:all [--model <model-key>]",
            "  /benchmark estimate [--model <model-key>] [--task <task-id> | --suite <suite-name>]",
            "  /benchmark:estimate [--model <model-key>] [--task <task-id> | --suite <suite-name>]",
            "  /benchmark compare --models <model-a,model-b> [--task <task-id> | "
            "--suite <suite-name>] [--repeat <n>]",
            "  /benchmark:compare --models <model-a,model-b> [--task <task-id> | "
            "--suite <suite-name>] [--repeat <n>]",
            "  /benchmark list",
            "  /benchmark:list",
            "  /benchmark show <run-id>",
            "  /benchmark:show <run-id>",
            "  /benchmark report [--suite <suite-name>]",
            "  /benchmark:report [--suite <suite-name>]",
            "  /benchmark export [--suite <suite-name>] [--format json|csv] [--output <path>]",
            "  /benchmark discover --source <allowlisted> --difficulty hard --limit 5 "
            "[--output <path.jsonl>]",
            "  /benchmark swe --dataset <path.jsonl> --trusted-dataset true "
            "[--instance <instance-id>]",
            "  /benchmark:swe --dataset <path.jsonl> --trusted-dataset true "
            "[--instance <instance-id>]",
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
    if subcommand not in {
        "start",
        "estimate",
        "list",
        "show",
        "report",
        "export",
        "swe",
        "compare",
        "discover",
    }:
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
            "models",
            "task",
            "suite",
            "repeat",
            "max_concurrency",
            "timeout_seconds",
            "judges",
            "sandbox",
            "format",
            "output",
            "dataset",
            "instance",
            "trusted_dataset",
            "source",
            "difficulty",
            "limit",
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
    models_value = values.get("models")
    models = _model_list(models_value) if models_value is not None else None
    if str(values["subcommand"]) == "compare" and (models is None or len(models) < 2):
        raise BenchmarkSyntaxError("--models must include at least two models")
    if str(values["subcommand"]) == "compare" and models is not None and len(set(models)) < 2:
        raise BenchmarkSyntaxError("--models must include at least two distinct models")
    repeat = _positive_int(values.get("repeat", "1"), "--repeat")
    limit = _positive_int(values.get("limit", "5"), "--limit")
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
    export_format = str(values.get("format", "json")).lower()
    if export_format not in {"json", "csv"}:
        raise BenchmarkSyntaxError("--format must be json or csv")
    if max_concurrency > 1:
        raise BenchmarkSyntaxError("--max-concurrency > 1 is not supported in v1")
    output = Path(str(values["output"])).expanduser() if values.get("output") else None
    dataset = Path(str(values["dataset"])).expanduser() if values.get("dataset") else None
    trusted_dataset = _bool_flag(values.get("trusted_dataset", False), "--trusted-dataset")
    return BenchmarkArgs(
        subcommand=str(values["subcommand"]),
        model=_optional_str(values.get("model")),
        task=task,
        suite=suite,
        repeat=repeat,
        models=models,
        max_concurrency=max_concurrency,
        timeout_seconds=timeout_seconds,
        judges=judges,
        sandbox=sandbox,
        format=export_format,
        output=output,
        dataset=dataset,
        instance=_optional_str(values.get("instance")),
        trusted_dataset=trusted_dataset,
        run_id=_optional_str(values.get("run_id")),
        source=_optional_str(values.get("source")),
        difficulty=str(values.get("difficulty", "hard")),
        limit=limit,
    )


def _model_list(value: object) -> list[str]:
    return [part.strip() for part in str(value).split(",") if part.strip()]


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


def _bool_flag(value: object, flag: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise BenchmarkSyntaxError(f"{flag} must be true or false")


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
    if parsed.subcommand == "export":
        return export_benchmark(parsed.output, parsed.suite, parsed.format)
    if parsed.subcommand == "discover":
        return discover_benchmark(parsed)
    if parsed.subcommand == "swe":
        return await start_swe_benchmark(soul, parsed, raw_args=args)
    if parsed.subcommand == "start":
        return await start_benchmark(soul, parsed, raw_args=args)
    if parsed.subcommand == "compare":
        return await compare_benchmark(soul, parsed, raw_args=args)
    raise BenchmarkSyntaxError(benchmark_usage())


async def compare_benchmark(soul: PythinkerSoul, args: BenchmarkArgs, *, raw_args: str) -> str:
    assert args.models is not None
    for model_key in args.models:
        _validate_model(soul, model_key)
    summaries: list[str] = []
    compare_run_ids: list[str] = []
    for model_key in args.models:
        model_args = dataclasses.replace(args, model=model_key, models=None, subcommand="start")
        summaries.append(
            await start_benchmark(
                soul,
                model_args,
                raw_args=raw_args,
                run_ids=compare_run_ids,
            )
        )
    suite = args.suite or (None if args.task else DEFAULT_SUITE)
    report = render_benchmark_report(args.output, suite, run_ids=compare_run_ids)
    return "\n\n".join([*summaries, report])


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


def discover_benchmark(args: BenchmarkArgs) -> str:
    if args.source is None:
        raise BenchmarkSyntaxError("--source is required for /benchmark discover")
    try:
        tasks = discover_benchmark_sources(
            source=args.source,
            difficulty=args.difficulty,
            limit=args.limit,
        )
    except ValueError as exc:
        raise BenchmarkSyntaxError(str(exc)) from exc
    lines = ["Pythinker Benchmark discovery", ""]
    if tasks:
        lines.extend(f"- {task.source}: {task.title} ({task.difficulty})" for task in tasks)
    else:
        lines.append(f"No benchmark tasks found for {args.source} at difficulty {args.difficulty}.")
    if args.output is not None and args.output.suffix == ".jsonl":
        args.output.write_text(
            "".join(
                json.dumps(_quiz_fixture_record(task), sort_keys=True) + "\n" for task in tasks
            ),
            encoding="utf-8",
        )
        lines.append(f"\nWrote provisional manifest: {args.output}")
    return "\n".join(lines)


def _quiz_fixture_record(task: DiscoveredBenchmarkTask) -> dict[str, object]:
    return quiz_fixture_from_discovery(
        task,
        question=(
            "Review this discovered benchmark candidate and identify its source "
            "and declared difficulty before converting it into a runnable local fixture."
        ),
        expected_substrings=[task.source, task.difficulty],
    )


async def start_benchmark(
    soul: PythinkerSoul,
    args: BenchmarkArgs,
    *,
    raw_args: str,
    run_ids: list[str] | None = None,
) -> str:
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
            if run_ids is not None:
                run_ids.append(run_id)
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


async def start_swe_benchmark(soul: PythinkerSoul, args: BenchmarkArgs, *, raw_args: str) -> str:
    if args.dataset is None:
        raise BenchmarkSyntaxError("--dataset is required for /benchmark:swe")
    if not args.trusted_dataset:
        raise BenchmarkSyntaxError(
            "/benchmark:swe runs verification commands from the dataset. "
            "Only run trusted local fixture datasets; pass --trusted-dataset true to continue."
        )
    model_key = _resolve_model_key(soul, args.model)
    root = args.output or get_share_dir() / "benchmarks"
    instances = load_swe_instances(args.dataset)
    if args.instance is not None:
        instances = [instance for instance in instances if instance.instance_id == args.instance]
    if not instances:
        raise BenchmarkSyntaxError(f"Unknown SWE benchmark instance: {args.instance}")
    run_summaries: list[str] = []
    suite_name = f"swe:{args.dataset.stem}"
    for repeat_index in range(1, args.repeat + 1):
        for instance in instances:
            task = swe_instance_to_task(instance)
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
                        f"- Task: {instance.instance_id}",
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


def render_benchmark_report(
    output: Path | None = None, suite: str | None = None, run_ids: list[str] | None = None
) -> str:
    from pythinker_code.benchmark.compare import readiness_warnings

    root = output or get_share_dir() / "benchmarks"
    if not root.exists():
        return "Pythinker Benchmark\n\nNo benchmark runs found."
    rows = _load_report_rows(root, suite=suite, run_ids=run_ids)
    if not rows:
        return "Pythinker Benchmark\n\nNo matching benchmark runs found."
    passed = sum(1 for row in rows if _row_status(row) == "passed")
    lines = [
        "Pythinker Benchmark report",
        "",
        f"Suite: {suite or 'all'}",
        f"Runs: {len(rows)}",
        f"Passed: {passed}/{len(rows)} ({_percent(passed, len(rows))})",
    ]
    warnings = readiness_warnings(rows)
    if warnings:
        lines.extend(["", "Publishability warnings:"])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.extend(["", "Models:"])
    for model, model_rows in _group_rows(rows, "model_key").items():
        model_passed = sum(1 for row in model_rows if _row_status(row) == "passed")
        lines.append(
            "- "
            f"{model}: {model_passed}/{len(model_rows)} passed "
            f"({_percent(model_passed, len(model_rows))}), "
            f"avg {_avg_duration(model_rows)}, "
            f"avg steps {_avg_number(model_rows, 'steps')}, "
            f"avg tools {_avg_number(model_rows, 'tool_calls')}, "
            f"avg tokens {_avg_tokens(model_rows)}"
        )
    lines.extend(["", "Tasks:"])
    for task, task_rows in _group_rows(rows, "task_id").items():
        task_passed = sum(1 for row in task_rows if _row_status(row) == "passed")
        lines.append(
            f"- {task}: {task_passed}/{len(task_rows)} passed "
            f"({_percent(task_passed, len(task_rows))})"
        )
    lines.extend(["", "Recent runs:"])
    for row in sorted(rows, key=lambda item: str(item.run.get("created_at", "")))[-10:]:
        run = row.run
        lines.append(
            "- "
            f"{run.get('run_id')}: {_row_status(row)} "
            f"({run.get('model_key')}, {run.get('task_id')})"
        )
    return "\n".join(lines)


def export_benchmark(
    output: Path | None = None, suite: str | None = None, fmt: str = "json"
) -> str:
    if fmt not in {"json", "csv"}:
        raise BenchmarkSyntaxError("--format must be json or csv")
    root = output or get_share_dir() / "benchmarks"
    rows = _load_report_rows(root, suite=suite, run_ids=None)
    return render_export(rows, fmt)


def _load_report_rows(
    root: Path, *, suite: str | None, run_ids: list[str] | None
) -> list[BenchmarkReportRow]:
    rows: list[BenchmarkReportRow] = []
    if not root.exists():
        return rows
    for path in sorted(root.glob("*/run.json")):
        run = _read_json_object(path)
        run_id = run.get("run_id")
        if run_ids is not None and run_id not in run_ids:
            continue
        if suite is None or run.get("suite_name") == suite:
            summary_path = path.parent / "summary.json"
            summary = _read_json_object(summary_path) if summary_path.exists() else {}
            rows.append(BenchmarkReportRow(run=run, summary=summary))
    return rows


def _read_json_object(path: Path) -> JsonObject:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise BenchmarkInternalError(f"Malformed benchmark artifact: {path}")
    return cast(JsonObject, data)


def _row_status(row: BenchmarkReportRow) -> str:
    if row.summary.get("status"):
        return str(row.summary["status"])
    return str(row.run.get("status", "unknown"))


def _group_rows(
    rows: list[BenchmarkReportRow], run_key: str
) -> dict[str, list[BenchmarkReportRow]]:
    grouped: dict[str, list[BenchmarkReportRow]] = {}
    for row in rows:
        key = str(row.run.get(run_key) or "unknown")
        grouped.setdefault(key, []).append(row)
    return dict(sorted(grouped.items()))


def _percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.0%"
    return f"{(numerator / denominator) * 100:.1f}%"


def _runtime(row: BenchmarkReportRow) -> JsonObject:
    runtime = row.summary.get("runtime")
    return cast(JsonObject, runtime) if isinstance(runtime, dict) else {}


def _usage(row: BenchmarkReportRow) -> JsonObject:
    usage = row.summary.get("usage")
    return cast(JsonObject, usage) if isinstance(usage, dict) else {}


def _avg_number(rows: list[BenchmarkReportRow], key: str) -> str:
    values = [value for row in rows if isinstance((value := _runtime(row).get(key)), int)]
    if not values:
        return "0.0"
    return f"{sum(values) / len(values):.1f}"


def _avg_duration(rows: list[BenchmarkReportRow]) -> str:
    values = [value for row in rows if isinstance((value := _runtime(row).get("duration_ms")), int)]
    if not values:
        return "0.0s"
    return f"{(sum(values) / len(values)) / 1000:.1f}s"


def _avg_tokens(rows: list[BenchmarkReportRow]) -> str:
    values = [value for row in rows if isinstance((value := _usage(row).get("total_tokens")), int)]
    if not values:
        return "0"
    return f"{sum(values) / len(values):.0f}"


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
    suffix = uuid4().hex[:8]
    return f"bench_{stamp}_{suffix}_{task_id.replace('-', '_')}"
