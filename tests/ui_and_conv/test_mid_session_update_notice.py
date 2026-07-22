"""Mid-session update discovery: one-time toast + periodic re-check loop."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pythinker_core.tooling.empty import EmptyToolset

import pythinker_code.ui.shell as shell_module
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul


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


def _mock_update_check(monkeypatch, targets: list[str | None], notice: str | None) -> None:
    async def refresh() -> None:
        return None

    target_values = iter(targets)
    monkeypatch.setattr(shell_module, "refresh_update_cache_if_due", refresh)
    monkeypatch.setattr(shell_module, "welcome_update_target", lambda: next(target_values))
    monkeypatch.setattr(shell_module, "pending_update_notice", lambda: notice)


@pytest.mark.asyncio
async def test_newly_discovered_update_toasts_once(
    runtime: Runtime, tmp_path: Path, monkeypatch, _toasts
):
    shell = _make_shell(runtime, tmp_path)
    shell._prompt_session = SimpleNamespace(invalidate=lambda: None)  # type: ignore[assignment]
    _mock_update_check(monkeypatch, [None, "9.9.9"], "Update available: 9.9.9")

    await shell._auto_update()

    assert len(_toasts) == 1
    assert "9.9.9" in _toasts[0][0]
    assert shell._update_toast_shown_version == "9.9.9"


@pytest.mark.asyncio
async def test_update_cached_before_refresh_does_not_toast(
    runtime: Runtime, tmp_path: Path, monkeypatch, _toasts
):
    shell = _make_shell(runtime, tmp_path)
    _mock_update_check(monkeypatch, ["9.9.9", "9.9.9"], "Update available: 9.9.9")

    await shell._auto_update()

    assert _toasts == []


@pytest.mark.asyncio
async def test_repeat_discovery_of_same_version_toasts_only_once(
    runtime: Runtime, tmp_path: Path, monkeypatch, _toasts
):
    shell = _make_shell(runtime, tmp_path)
    _mock_update_check(
        monkeypatch,
        [None, "9.9.9", None, "9.9.9"],
        "Update available: 9.9.9",
    )

    await shell._auto_update()
    await shell._auto_update()

    assert len(_toasts) == 1


@pytest.mark.asyncio
async def test_suppressed_notice_does_not_toast(
    runtime: Runtime, tmp_path: Path, monkeypatch, _toasts
):
    shell = _make_shell(runtime, tmp_path)
    _mock_update_check(monkeypatch, [None, "9.9.9"], None)

    await shell._auto_update()

    assert _toasts == []
    assert shell._update_toast_shown_version is None


def test_update_toast_dedupes_repeated_notices(runtime: Runtime, tmp_path: Path, _toasts):
    """The periodic loop re-surfaces outcomes; each notice text toasts once."""
    shell = _make_shell(runtime, tmp_path)

    shell._update_toast("Update available: 9.9.9", style="bold")
    shell._update_toast("Update available: 9.9.9", style="bold")
    shell._update_toast("Update available: 10.0.0", style="bold")

    assert [msg for msg, _ in _toasts] == [
        "Update available: 9.9.9",
        "Update available: 10.0.0",
    ]


@pytest.mark.asyncio
async def test_periodic_update_check_runs_immediately_then_at_interval(monkeypatch):
    checks: list[str] = []
    sleeps: list[float] = []

    async def check() -> None:
        checks.append("check")

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(shell_module.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await shell_module._periodic_update_check(check)

    assert checks == ["check", "check"]
    assert sleeps == [
        shell_module.AUTO_UPDATE_CHECK_INTERVAL_SECONDS,
        shell_module.AUTO_UPDATE_CHECK_INTERVAL_SECONDS,
    ]


@pytest.mark.asyncio
async def test_periodic_update_check_recovers_after_failing_check(monkeypatch):
    checks: list[str] = []
    sleeps: list[float] = []

    async def check() -> None:
        checks.append("check")
        if len(checks) == 1:
            raise RuntimeError("transient check failure")

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(shell_module.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await shell_module._periodic_update_check(check)

    # The first failure is swallowed (logged) and the loop keeps re-checking.
    assert checks == ["check", "check"]
    assert sleeps == [
        shell_module.AUTO_UPDATE_CHECK_INTERVAL_SECONDS,
        shell_module.AUTO_UPDATE_CHECK_INTERVAL_SECONDS,
    ]


@pytest.mark.asyncio
async def test_periodic_update_check_recovers_after_timed_out_check(monkeypatch):
    checks: list[str] = []
    sleeps: list[float] = []

    async def check() -> None:
        checks.append("check")
        if len(checks) == 1:
            await asyncio.Event().wait()  # hang until the watchdog fires

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    # asyncio.timeout drives off the event loop clock, not asyncio.sleep, so a
    # tiny real timeout fires while the patched sleep only records intervals.
    monkeypatch.setattr(shell_module.asyncio, "sleep", sleep)
    monkeypatch.setattr(shell_module, "AUTO_UPDATE_CHECK_ATTEMPT_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(asyncio.CancelledError):
        await shell_module._periodic_update_check(check)

    # The hung first attempt is bounded by the watchdog; the loop logs the
    # timeout and continues to the next interval instead of dying.
    assert checks == ["check", "check"]
    assert sleeps == [
        shell_module.AUTO_UPDATE_CHECK_INTERVAL_SECONDS,
        shell_module.AUTO_UPDATE_CHECK_INTERVAL_SECONDS,
    ]


def test_startup_scheduler_uses_periodic_loop_in_all_scheduling_branches(
    runtime: Runtime, tmp_path: Path, monkeypatch
):
    from pythinker_code.config import AutoUpdateMode

    shell = _make_shell(runtime, tmp_path)
    scheduled_names: list[str] = []

    def capture(coro):
        # The scheduler wraps each check in the periodic loop but deliberately
        # retains the concrete check's __name__ for task diagnostics/observers
        # (see `_start_periodic_update_check`). Assert on that documented name
        # at the scheduling seam instead of mocking the private loop wiring;
        # fully removing the seam would require exercising real update I/O.
        scheduled_names.append(coro.__name__)
        coro.close()
        return None

    monkeypatch.delenv("PYTHINKER_CLI_NO_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(shell, "_start_background_task", capture)

    with monkeypatch.context() as context:
        context.setattr(shell_module, "PythinkerSoul", type("OtherSoul", (), {}))
        shell._schedule_startup_update_task()
    monkeypatch.setattr(shell_module, "resolve_auto_update_mode", lambda cfg: AutoUpdateMode.NOTIFY)
    shell._schedule_startup_update_task()
    monkeypatch.setattr(
        shell_module, "resolve_auto_update_mode", lambda cfg: AutoUpdateMode.DOWNLOAD
    )
    shell._schedule_startup_update_task()

    assert scheduled_names == ["_auto_update", "_auto_update", "_silent_auto_update"]
