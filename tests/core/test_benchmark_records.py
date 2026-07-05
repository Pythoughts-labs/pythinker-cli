from __future__ import annotations

import json
from pathlib import Path

from pythinker_code.benchmark.records import BenchmarkRecorder
from pythinker_code.benchmark.runner import BenchmarkResult, VerificationResult


def test_recorder_writes_required_artifacts_and_redacts(tmp_path: Path) -> None:
    recorder = BenchmarkRecorder(tmp_path, "bench_test")
    recorder.start_run(
        command="/benchmark start --model mock-model --task smoke-edit-readme",
        model_key="mock-model",
        provider_key="mock",
        task_id="smoke-edit-readme",
        suite_name=None,
        repeat_index=1,
    )
    recorder.record_event(
        "model_message",
        {"content": "Authorization: Bearer secret-token-1234567890"},
    )
    result = BenchmarkResult(
        run_id="bench_test",
        status="passed",
        exit_reason="verification_passed",
        final_answer="done with sk-test-secret",
        verification=VerificationResult(
            status="passed",
            type="command",
            exit_code=0,
            stdout="3 passed in 0.00s\n",
            stderr="",
        ),
        duration_ms=10,
        steps=1,
        tool_calls=0,
        changed_files=["README.md"],
        input_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        estimated_cost_usd=None,
        activity={},
        environment={},
    )

    recorder.finish_run(result)

    run_dir = tmp_path / "bench_test"
    for name in (
        "run.json",
        "trace.jsonl",
        "context.jsonl",
        "wire.jsonl",
        "final.md",
        "summary.json",
        "report.md",
    ):
        assert (run_dir / name).exists()

    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "passed"

    trace = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
    assert "secret-token" not in trace
    assert "<redacted>" in trace

    final = (run_dir / "final.md").read_text(encoding="utf-8")
    assert "sk-test-secret" not in final

    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "Pythinker Benchmark" in report
    assert "Model: mock-model" in report
    assert "Task: smoke-edit-readme" in report
    assert "3 passed in 0.00s" in report
    assert "<redacted>" in report


def test_trace_payload_is_size_capped(tmp_path: Path) -> None:
    recorder = BenchmarkRecorder(tmp_path, "bench_test")
    recorder.start_run(
        command="/benchmark start --model mock-model --task smoke-edit-readme",
        model_key="mock-model",
        provider_key="mock",
        task_id="smoke-edit-readme",
        suite_name=None,
        repeat_index=1,
    )

    recorder.record_event("model_message", {"content": "x" * 20_000})

    trace = (tmp_path / "bench_test" / "trace.jsonl").read_text(encoding="utf-8")
    assert len(trace) < 12_000
    assert "truncated" in trace


def test_context_and_wire_copy_only_tail_and_redact(tmp_path: Path) -> None:
    context = tmp_path / "context-source.jsonl"
    wire = tmp_path / "wire-source.jsonl"
    context.write_text(json.dumps({"old": "context"}) + "\n", encoding="utf-8")
    wire.write_text(json.dumps({"old": "wire"}) + "\n", encoding="utf-8")
    context_offset = context.stat().st_size
    wire_offset = wire.stat().st_size
    context.write_text(
        json.dumps({"old": "context"})
        + "\n"
        + json.dumps({"new": "Authorization: Bearer secret-token-1234567890"})
        + "\n",
        encoding="utf-8",
    )
    wire.write_text(
        json.dumps({"old": "wire"})
        + "\n"
        + json.dumps({"new": "Cookie: session=abcdef1234567890"})
        + "\n",
        encoding="utf-8",
    )

    recorder = BenchmarkRecorder(tmp_path / "runs", "bench_test")
    recorder.start_run(
        command="/benchmark start --model mock-model --task smoke-edit-readme",
        model_key="mock-model",
        provider_key="mock",
        task_id="smoke-edit-readme",
        suite_name=None,
        repeat_index=1,
    )

    recorder.copy_context_and_wire(
        context,
        wire,
        context_offset=context_offset,
        wire_offset=wire_offset,
    )

    copied_context = (tmp_path / "runs" / "bench_test" / "context.jsonl").read_text(
        encoding="utf-8"
    )
    copied_wire = (tmp_path / "runs" / "bench_test" / "wire.jsonl").read_text(encoding="utf-8")
    assert "old" not in copied_context
    assert "old" not in copied_wire
    assert "secret-token" not in copied_context
    assert "abcdef1234567890" not in copied_wire
    assert "<redacted>" in copied_context
    assert "<redacted>" in copied_wire
    for line in copied_context.splitlines() + copied_wire.splitlines():
        json.loads(line)


def test_copied_jsonl_artifacts_remain_valid_when_truncated(tmp_path: Path) -> None:
    wire = tmp_path / "wire-source.jsonl"
    wire.write_text(
        json.dumps({"message": {"type": "ToolResult", "payload": {"text": "x" * 20_000}}})
        + "\n"
        + "<truncated>\n",
        encoding="utf-8",
    )

    recorder = BenchmarkRecorder(tmp_path / "runs", "bench_test")
    recorder.start_run(
        command="/benchmark start --model mock-model --task smoke-edit-readme",
        model_key="mock-model",
        provider_key="mock",
        task_id="smoke-edit-readme",
        suite_name=None,
        repeat_index=1,
    )

    recorder.copy_context_and_wire(
        tmp_path / "missing-context.jsonl",
        wire,
    )

    copied_wire = tmp_path / "runs" / "bench_test" / "wire.jsonl"
    records = [json.loads(line) for line in copied_wire.read_text(encoding="utf-8").splitlines()]
    assert records[0]["message"]["payload"]["text"].endswith("<truncated>")
    assert records[1]["type"] == "invalid_jsonl_line"


def test_finish_run_persists_reproducibility_metadata(tmp_path: Path) -> None:
    from pythinker_code.benchmark.records import BenchmarkRecorder
    from pythinker_code.benchmark.runner import BenchmarkResult, VerificationResult

    recorder = BenchmarkRecorder(tmp_path, "bench_env")
    recorder.start_run(
        command="/benchmark start",
        model_key="mock-model",
        provider_key="mock-provider",
        task_id="core-safe-path-join",
        suite_name="pythinker-core",
        repeat_index=1,
    )
    result = BenchmarkResult(
        run_id="bench_env",
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
        duration_ms=10,
        steps=1,
        tool_calls=1,
        changed_files=["paths.py"],
        input_tokens=3,
        output_tokens=2,
        reasoning_tokens=0,
        estimated_cost_usd=None,
        activity={},
        environment={
            "git_commit": "abc123",
            "git_dirty": False,
            "python_version": "3.14.0",
            "platform": "test-platform",
            "task_timeout_seconds": 180,
            "task_max_steps": 60,
            "verification_command_sha256": "0" * 64,
        },
    )

    recorder.finish_run(result)

    summary = json.loads((tmp_path / "bench_env" / "summary.json").read_text(encoding="utf-8"))
    assert summary["environment"]["git_commit"] == "abc123"
    assert summary["environment"]["git_dirty"] is False
    assert summary["environment"]["task_max_steps"] == 60
