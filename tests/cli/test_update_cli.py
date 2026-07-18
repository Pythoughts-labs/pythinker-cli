from __future__ import annotations

import os

from typer.testing import CliRunner

from pythinker_code.cli import cli
from pythinker_code.ui.shell import update as update_module
from pythinker_code.ui.shell import update_orchestrator as orchestrator


def _isolate_update_files(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(orchestrator, "UPDATE_STATUS_FILE", tmp_path / "update_status.json")
    monkeypatch.setattr(orchestrator, "UPDATE_LOG_FILE", tmp_path / "update.log")
    monkeypatch.setattr(orchestrator, "UPDATE_LOCK_FILE", tmp_path / "update.lock")
    monkeypatch.setattr(
        orchestrator,
        "UPDATE_LAST_SUCCESS_FILE",
        tmp_path / "update_last_success.json",
    )


def test_update_status_command_renders_recorded_status(monkeypatch, tmp_path):
    _isolate_update_files(monkeypatch, tmp_path)
    orchestrator.write_update_status(
        orchestrator.UpdateJobStatus(
            job_id="job-1",
            state=orchestrator.UpdateJobState.UPDATED,
            started_at=1.0,
            finished_at=2.0,
            current_version="1.0.0",
            target_version="1.1.0",
            result="UPDATED",
            message="Smoke check passed: pythinker, version 1.1.0",
            log_path=str(orchestrator.UPDATE_LOG_FILE),
            pid=os.getpid(),
            source="test",
        )
    )

    result = CliRunner().invoke(cli, ["update", "status"])

    assert result.exit_code == 0, result.output
    assert "State: updated" in result.output
    assert "Result: UPDATED" in result.output
    assert "Current version: 1.0.0" in result.output
    assert "Target version: 1.1.0" in result.output
    assert "Message: Smoke check passed" in result.output
    assert f"Log: {orchestrator.UPDATE_LOG_FILE}" in result.output


def test_update_log_command_respects_line_count(monkeypatch, tmp_path):
    _isolate_update_files(monkeypatch, tmp_path)
    for idx in range(4):
        orchestrator.append_update_log(f"line {idx}")

    result = CliRunner().invoke(cli, ["update", "log", "--lines", "2"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["line 2", "line 3"]


def test_update_command_exits_nonzero_when_verification_fails(monkeypatch):
    async def fake_run_update_job(*, print_output, intent, source):
        assert print_output is True
        assert intent is update_module.UpdateIntent.INSTALL_AND_EXIT
        assert source == "cli"
        return update_module.UpdateResult.VERIFICATION_FAILED

    monkeypatch.setattr(orchestrator, "run_update_job", fake_run_update_job)

    result = CliRunner().invoke(cli, ["update"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 1
