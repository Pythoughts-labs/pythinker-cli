from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pythinker_code.benchmark.redact import dumps_redacted, redact_text
from pythinker_code.benchmark.report import render_run_report


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class BenchmarkRecorder:
    def __init__(self, root: Path, run_id: str) -> None:
        self.root = root
        self.run_id = run_id
        self.run_dir = root / run_id
        self.workspace_dir = self.run_dir / "workspace"
        self.trace_path = self.run_dir / "trace.jsonl"
        self.run_path = self.run_dir / "run.json"
        self._step = 0
        self._run: dict[str, Any] = {}

    def start_run(
        self,
        *,
        command: str,
        model_key: str,
        provider_key: str,
        task_id: str | None,
        suite_name: str | None,
        repeat_index: int,
    ) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        for name in ("trace.jsonl", "context.jsonl", "wire.jsonl", "final.md", "summary.json"):
            (self.run_dir / name).touch()
        self._run = {
            "schema_version": 1,
            "run_id": self.run_id,
            "command": command,
            "created_at": utc_now(),
            "started_at": utc_now(),
            "finished_at": None,
            "status": "running",
            "exit_reason": None,
            "model_key": model_key,
            "provider_key": provider_key,
            "task_id": task_id,
            "suite_name": suite_name,
            "repeat_index": repeat_index,
            "artifact_root": str(self.run_dir),
        }
        self._write_run()
        self.record_event("run_started", {"run_id": self.run_id, "task_id": task_id})

    def record_event(self, event_type: str, data: dict[str, Any]) -> None:
        self._step += 1
        event = {
            "ts": utc_now(),
            "type": event_type,
            "step": self._step,
            **data,
        }
        with self.trace_path.open("a", encoding="utf-8") as f:
            f.write(dumps_redacted(event) + "\n")

    def copy_context_and_wire(
        self,
        context_file: Path,
        wire_file: Path,
        *,
        context_offset: int = 0,
        wire_offset: int = 0,
    ) -> None:
        self._copy_jsonl_tail_redacted(context_file, self.run_dir / "context.jsonl", context_offset)
        self._copy_jsonl_tail_redacted(wire_file, self.run_dir / "wire.jsonl", wire_offset)

    def finish_run(self, result: Any) -> None:
        self.record_event(
            "run_finished",
            {"status": result.status, "exit_reason": result.exit_reason},
        )
        summary = {
            "schema_version": 1,
            "run_id": result.run_id,
            "status": result.status,
            "score": 1.0 if result.status == "passed" else 0.0,
            "verification": {
                "status": result.verification.status,
                "type": result.verification.type,
                "exit_code": result.verification.exit_code,
                "stdout": result.verification.stdout,
                "stderr": result.verification.stderr,
            },
            "usage": {
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "reasoning_tokens": result.reasoning_tokens,
                "total_tokens": (
                    result.input_tokens + result.output_tokens + result.reasoning_tokens
                ),
                "estimated_cost_usd": result.estimated_cost_usd,
            },
            "runtime": {
                "duration_ms": result.duration_ms,
                "steps": result.steps,
                "tool_calls": result.tool_calls,
                "changed_files": result.changed_files,
            },
        }
        (self.run_dir / "summary.json").write_text(
            dumps_redacted(summary, indent=2) + "\n", encoding="utf-8"
        )
        (self.run_dir / "final.md").write_text(redact_text(result.final_answer), encoding="utf-8")
        (self.run_dir / "report.md").write_text(
            render_run_report(
                self._run,
                summary,
                self.run_dir,
                final_answer=result.final_answer,
            ),
            encoding="utf-8",
        )
        self._run["status"] = result.status
        self._run["exit_reason"] = result.exit_reason
        self._run["finished_at"] = utc_now()
        self._write_run()

    def _write_run(self) -> None:
        self.run_path.write_text(dumps_redacted(self._run, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def _copy_jsonl_tail_redacted(source: Path, dest: Path, offset: int) -> None:
        if not source.exists():
            dest.touch()
            return
        with source.open("r", encoding="utf-8", errors="replace") as src:
            src.seek(offset)
            lines = src.readlines()
        with dest.open("w", encoding="utf-8") as out:
            for line_number, line in enumerate(lines, start=1):
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                try:
                    record: object = json.loads(stripped)
                except json.JSONDecodeError:
                    record = {
                        "schema_version": 1,
                        "type": "invalid_jsonl_line",
                        "line": line_number,
                        "content": redact_text(stripped),
                    }
                out.write(dumps_redacted(record) + "\n")


def load_run(root: Path, run_id: str) -> tuple[dict[str, object], dict[str, object] | None]:
    run_dir = root / run_id
    run = cast(dict[str, object], json.loads((run_dir / "run.json").read_text(encoding="utf-8")))
    summary_path = run_dir / "summary.json"
    summary = (
        cast(dict[str, object], json.loads(summary_path.read_text(encoding="utf-8")))
        if summary_path.exists()
        else None
    )
    return run, summary
