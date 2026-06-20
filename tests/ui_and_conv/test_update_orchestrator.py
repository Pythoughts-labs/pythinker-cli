from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from pythinker_code.ui.shell import update
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


@pytest.mark.asyncio
async def test_update_job_records_status_and_log(monkeypatch, tmp_path):
    _isolate_update_files(monkeypatch, tmp_path)

    async def fake_do_update(*, print_output: bool, check_only: bool, output_callback=None):
        assert print_output is False
        assert check_only is True
        assert output_callback is not None
        output_callback("checked release channel")
        return update.UpdateResult.UP_TO_DATE

    monkeypatch.setattr(update, "do_update", fake_do_update)
    monkeypatch.setattr(orchestrator, "_read_target_version", lambda: "1.2.3")

    result = await orchestrator.run_update_job(print_output=False, check_only=True, source="test")

    assert result is update.UpdateResult.UP_TO_DATE
    assert not orchestrator.UPDATE_LOCK_FILE.exists()
    status = orchestrator.read_update_status()
    assert status is not None
    assert status.state is orchestrator.UpdateJobState.UP_TO_DATE
    assert status.result == "UP_TO_DATE"
    assert status.target_version == "1.2.3"
    assert "checked release channel" in "\n".join(orchestrator.read_update_log_tail())


@pytest.mark.asyncio
async def test_update_job_blocks_when_another_process_holds_lock(monkeypatch, tmp_path):
    _isolate_update_files(monkeypatch, tmp_path)
    orchestrator.UPDATE_LOCK_FILE.write_text(
        json.dumps({"pid": os.getpid(), "started_at": time.time()}),
        encoding="utf-8",
    )

    async def fail_do_update(**_kwargs):
        raise AssertionError("locked update must not call do_update")

    monkeypatch.setattr(update, "do_update", fail_do_update)

    orchestrator.write_update_status(
        orchestrator.UpdateJobStatus(
            job_id="running-job",
            state=orchestrator.UpdateJobState.RUNNING,
            started_at=time.time(),
            finished_at=None,
            current_version="1.0.0",
            target_version=None,
            result=None,
            message="still running",
            log_path=str(orchestrator.UPDATE_LOG_FILE),
            pid=os.getpid(),
        )
    )

    result = await orchestrator.run_update_job(print_output=False, source="test")

    assert result is update.UpdateResult.FAILED
    status = orchestrator.read_update_status()
    assert status is not None
    assert status.job_id == "running-job"
    assert status.state is orchestrator.UpdateJobState.RUNNING
    assert "already running" in "\n".join(orchestrator.read_update_log_tail())


@pytest.mark.asyncio
async def test_update_job_blocks_on_fresh_malformed_lock(monkeypatch, tmp_path):
    _isolate_update_files(monkeypatch, tmp_path)
    orchestrator.UPDATE_LOCK_FILE.write_text("", encoding="utf-8")

    async def fail_do_update(**_kwargs):
        raise AssertionError("fresh malformed lock must not call do_update")

    monkeypatch.setattr(update, "do_update", fail_do_update)

    result = await orchestrator.run_update_job(print_output=False, source="test")

    assert result is update.UpdateResult.FAILED
    assert orchestrator.UPDATE_LOCK_FILE.exists()
    assert orchestrator.read_update_status() is None
    assert "already running" in "\n".join(orchestrator.read_update_log_tail())


@pytest.mark.asyncio
async def test_update_job_replaces_old_malformed_lock(monkeypatch, tmp_path):
    _isolate_update_files(monkeypatch, tmp_path)
    orchestrator.UPDATE_LOCK_FILE.write_text("", encoding="utf-8")
    old_time = time.time() - orchestrator._LOCK_MALFORMED_GRACE_SECONDS - 1
    os.utime(orchestrator.UPDATE_LOCK_FILE, (old_time, old_time))

    async def fake_do_update(*, print_output: bool, check_only: bool, output_callback=None):
        return update.UpdateResult.UP_TO_DATE

    monkeypatch.setattr(update, "do_update", fake_do_update)

    result = await orchestrator.run_update_job(print_output=False, source="test")

    assert result is update.UpdateResult.UP_TO_DATE
    assert not orchestrator.UPDATE_LOCK_FILE.exists()


@pytest.mark.asyncio
async def test_update_job_replaces_stale_lock(monkeypatch, tmp_path):
    _isolate_update_files(monkeypatch, tmp_path)
    orchestrator.UPDATE_LOCK_FILE.write_text(
        json.dumps({"pid": 123456789, "started_at": time.time()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(orchestrator, "_pid_exists", lambda _pid: False)

    async def fake_do_update(*, print_output: bool, check_only: bool, output_callback=None):
        return update.UpdateResult.UP_TO_DATE

    monkeypatch.setattr(update, "do_update", fake_do_update)

    result = await orchestrator.run_update_job(print_output=False, source="test")

    assert result is update.UpdateResult.UP_TO_DATE
    assert not orchestrator.UPDATE_LOCK_FILE.exists()
    status = orchestrator.read_update_status()
    assert status is not None
    assert status.state is orchestrator.UpdateJobState.UP_TO_DATE


@pytest.mark.asyncio
async def test_update_job_skips_success_marker_when_post_install_smoke_check_fails(
    monkeypatch, tmp_path
):
    _isolate_update_files(monkeypatch, tmp_path)

    async def fake_do_update(*, print_output: bool, check_only: bool, output_callback=None):
        return update.UpdateResult.UPDATED

    monkeypatch.setattr(update, "do_update", fake_do_update)
    monkeypatch.setattr(
        orchestrator,
        "run_post_install_smoke_check",
        lambda: (False, "Smoke check failed: broken"),
    )

    result = await orchestrator.run_update_job(print_output=False, source="test")

    assert result is update.UpdateResult.UPDATED
    status = orchestrator.read_update_status()
    assert status is not None
    assert status.state is orchestrator.UpdateJobState.UPDATED
    assert status.result == "UPDATED"
    assert "smoke check did not pass" in (status.message or "").lower()
    assert not orchestrator.UPDATE_LAST_SUCCESS_FILE.exists()


@pytest.mark.asyncio
async def test_run_update_prompt_routes_check_through_runner(monkeypatch):
    calls: list[bool] = []

    async def fail_do_update(**_kwargs):
        raise AssertionError("orchestrated /update check must not call do_update directly")

    async def fake_runner(*, print_output: bool, check_only: bool):
        assert print_output is True
        calls.append(check_only)
        return update.UpdateResult.UP_TO_DATE

    monkeypatch.setattr(update, "do_update", fail_do_update)

    result = await update.run_update_prompt(update_runner=fake_runner)

    assert result is update.UpdateResult.UP_TO_DATE
    assert calls == [True]


def test_update_log_tail_returns_recent_lines(monkeypatch, tmp_path):
    _isolate_update_files(monkeypatch, tmp_path)

    for idx in range(5):
        orchestrator.append_update_log(f"line {idx}")

    assert orchestrator.read_update_log_tail(2) == ["line 3", "line 4"]


def test_update_log_redacts_sensitive_output_and_is_owner_only(monkeypatch, tmp_path):
    _isolate_update_files(monkeypatch, tmp_path)

    orchestrator.append_update_log(
        "Authorization: Bearer secret-token-12345 "
        "PIP_INDEX_URL=https://user:pass@example.test/simple"
    )

    log_text = orchestrator.UPDATE_LOG_FILE.read_text(encoding="utf-8")
    assert "secret-token-12345" not in log_text
    assert "user:pass" not in log_text
    assert "<redacted" in log_text
    if os.name != "nt":
        assert orchestrator.UPDATE_LOG_FILE.stat().st_mode & 0o777 == 0o600


def test_status_write_is_owner_only_and_does_not_use_shared_temp(monkeypatch, tmp_path):
    _isolate_update_files(monkeypatch, tmp_path)

    orchestrator.write_update_status(
        orchestrator.UpdateJobStatus(
            job_id="job-1",
            state=orchestrator.UpdateJobState.RUNNING,
            started_at=1.0,
            finished_at=None,
            current_version="1.0.0",
            target_version=None,
            result=None,
            message=None,
            log_path=str(orchestrator.UPDATE_LOG_FILE),
            pid=os.getpid(),
        )
    )

    assert orchestrator.UPDATE_STATUS_FILE.exists()
    assert not (tmp_path / "update_status.json.tmp").exists()
    if os.name != "nt":
        assert orchestrator.UPDATE_STATUS_FILE.stat().st_mode & 0o777 == 0o600


def test_pid_exists_is_conservative_on_windows_without_signaling(monkeypatch):
    monkeypatch.setattr(orchestrator.os, "name", "nt")

    def fail_kill(_pid: int, _signal: int) -> None:
        raise AssertionError("Windows PID checks must not call os.kill")

    monkeypatch.setattr(orchestrator.os, "kill", fail_kill)

    assert orchestrator._pid_exists(os.getpid() + 1) is True


def test_python_smoke_check_uses_safe_import_path(monkeypatch):
    monkeypatch.setattr(orchestrator, "is_native_build", lambda: False)
    monkeypatch.setattr(orchestrator.sys, "executable", "/tmp/venv/bin/python")

    assert orchestrator._smoke_check_command() == [
        "/tmp/venv/bin/python",
        "-P",
        "-m",
        "pythinker_code",
        "--version",
    ]


def test_native_smoke_check_does_not_use_python_module_import(monkeypatch):
    monkeypatch.setattr(orchestrator, "is_native_build", lambda: True)
    monkeypatch.setattr(orchestrator.sys, "executable", "/opt/pythinker/pythinker")

    assert orchestrator._smoke_check_command() == ["/opt/pythinker/pythinker", "--version"]


def test_native_smoke_check_targets_staged_binary_when_present(monkeypatch, tmp_path):
    # A native update is staged beside the running exe; the smoke check must
    # validate the staged binary, not the still-running old one.
    current = tmp_path / "pythinker"
    current.write_text("old", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "is_native_build", lambda: True)
    monkeypatch.setattr(orchestrator.sys, "executable", str(current))

    from pythinker_code.ui.shell.update import staged_native_path

    staged = staged_native_path()
    staged.write_text("new", encoding="utf-8")

    assert orchestrator._smoke_check_command() == [str(staged), "--version"]


@pytest.mark.asyncio
async def test_do_update_mirrors_messages_to_output_callback(monkeypatch, tmp_path):
    messages: list[str] = []

    async def fake_get_latest(session):
        return "999.0.0"

    async def fake_unavailable(session, latest_version: str, upgrade_command: list[str]):
        return None

    async def fake_native_update(latest_version: str) -> update.UpdateResult:
        return update.UpdateResult.UPDATED

    monkeypatch.setattr(update, "LATEST_VERSION_FILE", tmp_path / "latest.txt")
    monkeypatch.setattr(update, "_get_latest_version", fake_get_latest)
    monkeypatch.setattr(update, "_update_candidate_unavailable_reason", fake_unavailable)
    monkeypatch.setattr(update, "_detect_upgrade_command", lambda: [update.NATIVE_INSTALLER_MARKER])
    monkeypatch.setattr(update, "_maybe_run_native_update", fake_native_update)

    result = await update.do_update(print_output=False, output_callback=messages.append)

    assert result is update.UpdateResult.UPDATED
    assert any("Checking for updates" in message for message in messages)
    assert any("Updating pythinker-code" in message for message in messages)


def test_smoke_check_reports_success(monkeypatch):
    monkeypatch.setattr(orchestrator, "_smoke_check_command", lambda: ["pythinker", "--version"])
    monkeypatch.setenv("PYTHONPATH", "/tmp/untrusted")

    def fake_run(command, **kwargs):
        assert command == ["pythinker", "--version"]
        assert kwargs["timeout"] == orchestrator._SMOKE_CHECK_TIMEOUT_SECONDS
        assert kwargs["env"]["PYTHONSAFEPATH"] == "1"
        assert "PYTHONPATH" not in kwargs["env"]
        assert kwargs["cwd"] == orchestrator._smoke_check_cwd()
        return SimpleNamespace(returncode=0, stdout="pythinker, version 1.2.3\n", stderr="")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)

    ok, message = orchestrator.run_post_install_smoke_check()

    assert ok is True
    assert "1.2.3" in message


def test_smoke_check_reports_failure(monkeypatch):
    monkeypatch.setattr(orchestrator, "_smoke_check_command", lambda: ["pythinker", "--version"])

    def fake_run(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="broken\n")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)

    ok, message = orchestrator.run_post_install_smoke_check()

    assert ok is False
    assert "broken" in message
