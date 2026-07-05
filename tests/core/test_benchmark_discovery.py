from __future__ import annotations

import json
from pathlib import Path

import pytest

from pythinker_code.benchmark.commands import (
    BenchmarkArgs,
    BenchmarkSyntaxError,
    discover_benchmark,
    parse_args,
)
from pythinker_code.benchmark.discovery import (
    SOURCE_URLS,
    DiscoveredBenchmarkTask,
    discover_benchmark_sources,
    quiz_fixture_from_discovery,
)


def test_discover_terminal_bench_from_allowlisted_source() -> None:
    html = """
    <a>train-fasttext Github model-training hard</a>
    <a>check-cert coding security medium</a>
    """

    tasks = discover_benchmark_sources(
        source="terminal-bench",
        difficulty="hard",
        limit=3,
        fetch_text=lambda url: html,
    )

    assert [task.source for task in tasks] == ["terminal-bench"]
    assert tasks[0].difficulty == "hard"
    assert "train-fasttext" in tasks[0].title
    assert tasks[0].trusted is False


def test_discover_deepswe_from_allowlisted_source() -> None:
    html = """
    <a>Add deterministic map conflict detection to Y.Map writes javascript</a>
    <a>Fix PromQL label sorting across typed and untyped values go</a>
    """

    tasks = discover_benchmark_sources(
        source="deepswe",
        difficulty="hard",
        limit=1,
        fetch_text=lambda url: html,
    )

    assert SOURCE_URLS["deepswe"] == "https://deepswe.datacurve.ai/"
    assert tasks == [
        DiscoveredBenchmarkTask(
            source="deepswe",
            title="Add deterministic map conflict detection to Y.Map writes javascript",
            difficulty="hard",
            source_url="https://deepswe.datacurve.ai/",
            trusted=False,
            notes=tasks[0].notes,
        )
    ]


def test_quiz_fixture_uses_deterministic_answer_check() -> None:
    task = discover_benchmark_sources(
        source="terminal-bench",
        difficulty="hard",
        limit=1,
        fetch_text=lambda url: "train-fasttext Github model-training hard",
    )[0]

    record = quiz_fixture_from_discovery(
        task,
        question="Which benchmark source produced this hard task?",
        expected_substrings=["terminal-bench", "hard"],
    )

    assert record["verification"] == {
        "type": "answer_contains",
        "expected_substrings": ["terminal-bench", "hard"],
    }
    assert record["trusted"] is False
    assert record["workspace"]["files"] == {}


def test_discover_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="Unsupported benchmark source"):
        discover_benchmark_sources(
            source="random-blog",
            difficulty="hard",
            limit=1,
            fetch_text=lambda url: "",
        )


def test_discover_rejects_invalid_limit() -> None:
    with pytest.raises(ValueError, match="limit must be >= 1"):
        discover_benchmark_sources(
            source="terminal-bench",
            difficulty="hard",
            limit=0,
            fetch_text=lambda url: "",
        )


def test_parse_discover_args() -> None:
    args = parse_args("discover --source terminal-bench --difficulty hard --limit 5")

    assert args.subcommand == "discover"
    assert args.source == "terminal-bench"
    assert args.difficulty == "hard"
    assert args.limit == 5


def test_parse_discover_rejects_invalid_limit() -> None:
    with pytest.raises(BenchmarkSyntaxError, match="--limit must be >= 1"):
        parse_args("discover --source terminal-bench --limit 0")


def test_discover_benchmark_writes_jsonl_only_for_jsonl_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = DiscoveredBenchmarkTask(
        source="terminal-bench",
        title="train-fasttext Github model-training hard",
        difficulty="hard",
        source_url="https://www.tbench.ai/",
        trusted=False,
        notes="review required",
    )
    monkeypatch.setattr(
        "pythinker_code.benchmark.commands.discover_benchmark_sources",
        lambda **_: [task],
    )
    jsonl_output = tmp_path / "manifest.jsonl"
    artifact_root_output = tmp_path / "benchmark-runs"

    jsonl_text = discover_benchmark(
        BenchmarkArgs(subcommand="discover", source="terminal-bench", output=jsonl_output)
    )
    artifact_root_text = discover_benchmark(
        BenchmarkArgs(
            subcommand="discover",
            source="terminal-bench",
            output=artifact_root_output,
        )
    )

    assert json.loads(jsonl_output.read_text(encoding="utf-8"))["trusted"] is False
    assert "Wrote provisional manifest" in jsonl_text
    assert not artifact_root_output.exists()
    assert "Wrote provisional manifest" not in artifact_root_text


def test_discover_benchmark_reports_no_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pythinker_code.benchmark.commands.discover_benchmark_sources",
        lambda **_: [],
    )

    text = discover_benchmark(BenchmarkArgs(subcommand="discover", source="terminal-bench"))

    assert "No benchmark tasks found for terminal-bench at difficulty hard." in text
