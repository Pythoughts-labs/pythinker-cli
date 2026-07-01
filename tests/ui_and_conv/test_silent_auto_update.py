"""Silent auto-update: background install + result surfacing at startup."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pythinker_core.tooling.empty import EmptyToolset

import pythinker_code.ui.shell as shell_module
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.ui.shell.update import UpdateResult


def _make_shell(runtime: Runtime, tmp_path: Path) -> shell_module.Shell:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    return shell_module.Shell(soul)


@pytest.fixture
def _toasts(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(shell_module, "toast", lambda msg, **kw: captured.append((msg, kw)))
    return captured


@pytest.mark.asyncio
async def test_silent_update_success_refreshes_persistent_notice_not_toast(
    runtime: Runtime, tmp_path: Path, monkeypatch, _toasts
):
    shell = _make_shell(runtime, tmp_path)
    invalidated: list[bool] = []
    shell._prompt_session = SimpleNamespace(invalidate=lambda: invalidated.append(True))  # type: ignore[assignment]
    shell._update_notice_cache = (time.monotonic(), "stale")
    monkeypatch.setattr(shell_module, "_should_auto_check_for_updates", lambda: True)
    monkeypatch.setattr(shell_module, "_mark_auto_update_check_attempt", lambda: None)
    monkeypatch.setattr(shell_module, "_detect_upgrade_command", lambda: ["pip"])

    async def fake_job(**kw):
        assert not kw["print_output"]
        assert kw["source"] == "startup-auto"
        return UpdateResult.UPDATED

    monkeypatch.setattr(shell_module, "run_update_job", fake_job)
    monkeypatch.setattr(
        shell_module,
        "read_update_status",
        lambda: SimpleNamespace(message="updated", target_version="0.43.0"),
    )

    await shell._silent_auto_update()

    assert _toasts == []
    assert invalidated == [True]
    expected_cache = (0.0, None)
    assert shell._update_notice_cache == expected_cache


@pytest.mark.asyncio
async def test_silent_update_smoke_fail_toasts_verification_failed(
    runtime: Runtime, tmp_path: Path, monkeypatch, _toasts
):
    shell = _make_shell(runtime, tmp_path)
    monkeypatch.setattr(shell_module, "_should_auto_check_for_updates", lambda: True)
    monkeypatch.setattr(shell_module, "_mark_auto_update_check_attempt", lambda: None)
    monkeypatch.setattr(shell_module, "_detect_upgrade_command", lambda: ["pip"])

    async def fake_job(**kw):
        return UpdateResult.UPDATED

    monkeypatch.setattr(shell_module, "run_update_job", fake_job)
    monkeypatch.setattr(
        shell_module,
        "read_update_status",
        lambda: SimpleNamespace(
            message="Updated, but smoke check did not pass: boom",
            target_version="0.43.0",
        ),
    )

    await shell._silent_auto_update()

    assert any("verification failed" in m for m, _ in _toasts)
    assert not any("Restart Pythinker to apply" in m for m, _ in _toasts)


@pytest.mark.asyncio
async def test_silent_update_failed_is_silent(
    runtime: Runtime, tmp_path: Path, monkeypatch, _toasts
):
    shell = _make_shell(runtime, tmp_path)
    monkeypatch.setattr(shell_module, "_should_auto_check_for_updates", lambda: True)
    monkeypatch.setattr(shell_module, "_mark_auto_update_check_attempt", lambda: None)
    monkeypatch.setattr(shell_module, "_detect_upgrade_command", lambda: ["pip"])

    async def fake_job(**kw):
        return UpdateResult.FAILED

    monkeypatch.setattr(shell_module, "run_update_job", fake_job)
    await shell._silent_auto_update()
    assert _toasts == []


@pytest.mark.parametrize(
    ("result", "expected_marks"),
    [
        (UpdateResult.FAILED, 0),
        (UpdateResult.UP_TO_DATE, 1),
        (UpdateResult.UPDATED, 1),
    ],
)
@pytest.mark.asyncio
async def test_silent_update_marks_throttle_only_after_non_failed_run(
    runtime: Runtime, tmp_path: Path, monkeypatch, _toasts, result, expected_marks
):
    """A FAILED run (e.g. a transient startup network blip) must not burn the
    throttle window: the mark fires only after a completed, non-FAILED job."""
    shell = _make_shell(runtime, tmp_path)
    marks = 0

    def _spy_mark() -> None:
        nonlocal marks
        marks += 1

    monkeypatch.setattr(shell_module, "_should_auto_check_for_updates", lambda: True)
    monkeypatch.setattr(shell_module, "_mark_auto_update_check_attempt", _spy_mark)
    monkeypatch.setattr(shell_module, "_detect_upgrade_command", lambda: ["pip"])
    monkeypatch.setattr(
        shell_module,
        "read_update_status",
        lambda: SimpleNamespace(message="updated", target_version="0.43.0"),
    )

    async def fake_job(**kw):
        return result

    monkeypatch.setattr(shell_module, "run_update_job", fake_job)

    await shell._silent_auto_update()

    assert marks == expected_marks


@pytest.mark.asyncio
async def test_silent_update_managed_channel_toasts_channel_hint(
    runtime: Runtime, tmp_path: Path, monkeypatch, _toasts
):
    shell = _make_shell(runtime, tmp_path)
    monkeypatch.setattr(shell_module, "_should_auto_check_for_updates", lambda: True)
    monkeypatch.setattr(shell_module, "_mark_auto_update_check_attempt", lambda: None)
    monkeypatch.setattr(
        shell_module,
        "_detect_upgrade_command",
        lambda: [shell_module.MANAGED_CHANNEL_MARKER, "Nix"],
    )

    async def fake_job(**kw):
        return UpdateResult.UPDATE_AVAILABLE

    monkeypatch.setattr(shell_module, "run_update_job", fake_job)
    monkeypatch.setattr(shell_module, "welcome_update_target", lambda: "0.43.0")
    monkeypatch.setattr(
        shell_module,
        "format_managed_channel_notice",
        lambda cur, latest: f"managed Nix {cur} -> {latest}",
    )

    await shell._silent_auto_update()
    assert any("managed Nix" in m for m, _ in _toasts)


@pytest.mark.asyncio
async def test_silent_update_respects_throttle(
    runtime: Runtime, tmp_path: Path, monkeypatch, _toasts
):
    shell = _make_shell(runtime, tmp_path)
    monkeypatch.setattr(shell_module, "_should_auto_check_for_updates", lambda: False)
    called = False

    async def fake_job(**kw):
        nonlocal called
        called = True
        return UpdateResult.UPDATED

    monkeypatch.setattr(shell_module, "run_update_job", fake_job)
    await shell._silent_auto_update()
    assert not called
    assert _toasts == []


def _scheduling_shell(runtime, tmp_path, monkeypatch):
    shell = _make_shell(runtime, tmp_path)
    scheduled: list[str] = []

    def fake_start(coro):
        # Identify which coroutine was scheduled, then close it to avoid a
        # "coroutine was never awaited" warning.
        scheduled.append(coro.__name__ if hasattr(coro, "__name__") else repr(coro))
        coro.close()
        return None

    monkeypatch.setattr(shell, "_start_background_task", fake_start)
    return shell, scheduled


def test_dispatch_enabled_schedules_silent(runtime, tmp_path, monkeypatch):
    shell, scheduled = _scheduling_shell(runtime, tmp_path, monkeypatch)
    monkeypatch.delenv("PYTHINKER_CLI_NO_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(shell_module, "auto_update_enabled", lambda cfg: True)

    shell._schedule_startup_update_task()
    assert scheduled == ["_silent_auto_update"]


def test_dispatch_config_disabled_schedules_toast_only(runtime, tmp_path, monkeypatch):
    shell, scheduled = _scheduling_shell(runtime, tmp_path, monkeypatch)
    monkeypatch.delenv("PYTHINKER_CLI_NO_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(shell_module, "auto_update_enabled", lambda cfg: False)

    shell._schedule_startup_update_task()
    assert scheduled == ["_auto_update"]


def test_dispatch_env_killswitch_schedules_nothing(runtime, tmp_path, monkeypatch):
    shell, scheduled = _scheduling_shell(runtime, tmp_path, monkeypatch)
    monkeypatch.setenv("PYTHINKER_CLI_NO_AUTO_UPDATE", "1")

    shell._schedule_startup_update_task()
    assert scheduled == []


def test_background_task_systemexit_does_not_crash(runtime, tmp_path, monkeypatch):
    """_cleanup swallows SystemExit from t.result() and logs instead of crashing.

    Python 3.14 propagates SystemExit out of asyncio.run() when a task raises it,
    so we test the done-callback in isolation: intercept the callback registered by
    _start_background_task and invoke it with a mock task whose .result() raises
    SystemExit.
    """
    shell = _make_shell(runtime, tmp_path)
    logged: list[str] = []
    monkeypatch.setattr(shell_module.logger, "info", lambda msg, *a, **k: logged.append(msg))

    # A thin stand-in for asyncio.Task that captures the done-callback.
    registered: list = []

    class _CapturingTask:
        def add_done_callback(self, fn):
            registered.append(fn)

    capturing_task = _CapturingTask()

    def fake_create_task(coro, **kw):
        coro.close()  # avoid "coroutine never awaited" warning
        return capturing_task  # type: ignore[return-value]

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    async def noop():
        pass

    shell._start_background_task(noop())

    assert len(registered) == 1, "expected one done-callback to be registered"
    cleanup = registered[0]

    # capturing_task only needs add_done_callback() to grab the real _cleanup
    # closure; fake_task is the argument _cleanup actually operates on, so it
    # needs the .cancelled() / .result() interface that _cleanup calls.
    fake_task: MagicMock = MagicMock(spec=asyncio.Task)
    fake_task.cancelled.return_value = False
    fake_task.result.side_effect = SystemExit(0)
    # Pre-populate the set so _cleanup's discard(t) has something to remove,
    # mirroring what _start_background_task does in production.
    shell._background_tasks.add(fake_task)

    # Invoke _cleanup with the fake task — must NOT raise.
    cleanup(fake_task)

    # Cleanup removed the task from the set and logged the process-exit message.
    assert fake_task not in shell._background_tasks
    assert any("process exit" in m for m in logged)


def test_auto_update_override_reason_env_killswitch(monkeypatch):
    from pythinker_code import update_policy

    monkeypatch.setattr(update_policy, "auto_update_disabled", lambda: True)
    monkeypatch.setattr(update_policy, "is_running_from_source_checkout", lambda: False)
    assert update_policy.auto_update_override_reason() == "disabled by PYTHINKER_CLI_NO_AUTO_UPDATE"


def test_auto_update_override_reason_source_checkout(monkeypatch):
    from pythinker_code import update_policy

    monkeypatch.setattr(update_policy, "auto_update_disabled", lambda: False)
    monkeypatch.setattr(update_policy, "is_running_from_source_checkout", lambda: True)
    assert update_policy.auto_update_override_reason() == "disabled for source checkouts"


def test_auto_update_override_reason_none_when_config_decides(monkeypatch):
    from pythinker_code import update_policy

    monkeypatch.setattr(update_policy, "auto_update_disabled", lambda: False)
    monkeypatch.setattr(update_policy, "is_running_from_source_checkout", lambda: False)
    assert update_policy.auto_update_override_reason() is None


def _updated_status(target: str, *, message: str = "updated", pid: int | None = None):
    from pythinker_code.ui.shell.update_orchestrator import UpdateJobState

    return SimpleNamespace(
        state=UpdateJobState.UPDATED,
        target_version=target,
        message=message,
        pid=os.getpid() if pid is None else pid,
    )


def test_update_notice_available_points_to_slash_update(runtime, tmp_path, monkeypatch):
    shell = _make_shell(runtime, tmp_path)
    monkeypatch.setattr(shell_module, "welcome_update_target", lambda: "9.9.9")
    monkeypatch.setattr(shell_module, "read_update_status", lambda: None)
    monkeypatch.setattr(shell_module, "ascii_glyphs_enabled", lambda: False)
    assert shell._compute_update_notice() == "↑ Update available — v9.9.9 · /update"


def test_update_notice_installed_this_session_says_restart(runtime, tmp_path, monkeypatch):
    """Critical: after a silent install the cache still reports a newer version,
    but the line must say restart-to-apply, not /update (it would contradict the
    install toast and tell the user to re-run an update that already landed)."""
    shell = _make_shell(runtime, tmp_path)
    monkeypatch.setattr(shell_module, "welcome_update_target", lambda: "9.9.9")
    monkeypatch.setattr(shell_module, "ascii_glyphs_enabled", lambda: False)
    monkeypatch.setattr(shell_module, "read_update_status", lambda: _updated_status("9.9.9"))
    text = shell._compute_update_notice()
    assert text is not None and "Restart" in text and "9.9.9" in text
    assert "/update" not in text


def test_update_notice_installed_but_smoke_failed_falls_back(runtime, tmp_path, monkeypatch):
    shell = _make_shell(runtime, tmp_path)
    monkeypatch.setattr(shell_module, "welcome_update_target", lambda: "9.9.9")
    monkeypatch.setattr(shell_module, "ascii_glyphs_enabled", lambda: False)
    status = _updated_status("9.9.9", message=shell_module.SMOKE_CHECK_FAILED_PREFIX + "boom")
    monkeypatch.setattr(shell_module, "read_update_status", lambda: status)
    # A failed-verification install must not claim restart-to-apply.
    assert shell._compute_update_notice() == "↑ Update available — v9.9.9 · /update"


def test_update_notice_previous_process_success_falls_back(runtime, tmp_path, monkeypatch):
    shell = _make_shell(runtime, tmp_path)
    monkeypatch.setattr(shell_module, "welcome_update_target", lambda: "9.9.9")
    monkeypatch.setattr(shell_module, "ascii_glyphs_enabled", lambda: False)
    status = _updated_status("9.9.9", pid=os.getpid() + 1)
    monkeypatch.setattr(shell_module, "read_update_status", lambda: status)
    assert shell._compute_update_notice() == "↑ Update available — v9.9.9 · /update"


def test_update_notice_none_when_up_to_date(runtime, tmp_path, monkeypatch):
    shell = _make_shell(runtime, tmp_path)
    monkeypatch.setattr(shell_module, "welcome_update_target", lambda: None)
    assert shell._compute_update_notice() is None


def test_update_notice_text_uses_ttl_cache(runtime, tmp_path, monkeypatch):
    shell = _make_shell(runtime, tmp_path)
    shell._update_notice_cache = (time.monotonic(), "cached")

    def _boom() -> str:
        raise AssertionError("welcome_update_target should not be called within TTL")

    monkeypatch.setattr(shell_module, "welcome_update_target", _boom)
    assert shell._update_notice_text() == "cached"
