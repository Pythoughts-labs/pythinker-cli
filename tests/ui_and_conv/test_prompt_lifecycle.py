"""Prompt-session task and process lifetime regression tests."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from pythinker_code.ui.shell.prompt import CustomPromptSession
from pythinker_code.ui.shell.prompting.lifecycle import PromptLifecycle
from pythinker_code.ui.shell.statusline import StatusLineCommandRunner


@pytest.mark.asyncio
async def test_aclose_cancels_status_refresh_during_sleep() -> None:
    session = object.__new__(CustomPromptSession)
    session._lifecycle = PromptLifecycle()
    session._status_refresh_task = None
    session._statusline_runner = None
    session._fast_refresh_provider = None
    session._app_for_repaint = lambda: None
    session._active_prompt_delegate = lambda: None
    session._has_background_tasks = lambda: False

    session._start()
    task = cast(asyncio.Task[None], session._status_refresh_task)
    await asyncio.sleep(0)
    await session.aclose()

    assert task.done()
    assert session._status_refresh_task is None


@pytest.mark.asyncio
async def test_lifecycle_closes_placeholder_tasks_and_resources_in_reverse_order() -> None:
    lifecycle = PromptLifecycle()
    tasks_started = asyncio.Event()
    closed: list[str] = []

    async def placeholder() -> None:
        tasks_started.set()
        await asyncio.Event().wait()

    async def close_named(name: str) -> None:
        closed.append(name)

    tasks = [lifecycle.create_task(placeholder()) for _ in range(3)]
    lifecycle.register_closer("first", lambda: close_named("first"))
    lifecycle.register_closer("second", lambda: close_named("second"))
    await tasks_started.wait()

    await lifecycle.aclose()

    assert all(task.done() for task in tasks)
    assert closed == ["second", "first"]


@pytest.mark.asyncio
async def test_prompt_startup_cancelled_before_delegate_attach() -> None:
    lifecycle = PromptLifecycle()
    startup_reached_wait = asyncio.Event()
    delegate: list[object] = []

    async def startup() -> None:
        startup_reached_wait.set()
        await asyncio.Event().wait()
        delegate.append(object())

    task = lifecycle.create_task(startup())
    await startup_reached_wait.wait()
    await lifecycle.aclose()

    assert task.done()
    assert delegate == []


@pytest.mark.asyncio
async def test_repeated_aclose_is_idempotent_and_refuses_new_work() -> None:
    lifecycle = PromptLifecycle()
    close_calls = 0

    async def close_resource() -> None:
        nonlocal close_calls
        close_calls += 1

    lifecycle.register_closer("resource", close_resource)
    await lifecycle.aclose()
    await lifecycle.aclose()

    assert close_calls == 1

    async def refused() -> None:
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="closed"):
        lifecycle.create_task(refused())


class _BlockingStdout:
    def __init__(self) -> None:
        self.read_started = asyncio.Event()
        self._reads = 0

    async def read(self, _limit: int) -> bytes:
        self._reads += 1
        if self._reads == 1:
            self.read_started.set()
            await asyncio.Event().wait()
        return b""


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = _BlockingStdout()
        self.returncode: int | None = None
        self.kill_calls = 0
        self.wait_calls = 0

    def kill(self) -> None:
        if self.returncode is not None:
            raise ProcessLookupError
        self.kill_calls += 1
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_calls += 1
        assert self.returncode is not None
        return self.returncode


@pytest.mark.asyncio
async def test_aclose_cancels_statusline_read_and_reaps_child_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess()

    async def create_process(*_args: Any, **_kwargs: Any) -> asyncio.subprocess.Process:
        return cast(asyncio.subprocess.Process, process)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    lifecycle = PromptLifecycle()
    runner = StatusLineCommandRunner(command="status-command", timeout_ms=1000)
    lifecycle.register_closer("statusline command runner", runner.stop)
    runner.start()
    await process.stdout.read_started.wait()

    await asyncio.wait_for(lifecycle.aclose(), timeout=1)

    assert runner.is_running is False
    assert process.kill_calls <= 1
    assert process.wait_calls <= 1


@pytest.mark.asyncio
async def test_prompt_once_disables_prompt_toolkit_exception_handler() -> None:
    prompt_session = object.__new__(CustomPromptSession)
    prompt_session._running_prompt_delegate = None
    prompt_session._tip_rotation_index = 0
    captured: dict[str, Any] = {}

    class _Session:
        async def prompt_async(self, **kwargs: Any) -> str:
            captured.update(kwargs)
            return "hello"

    prompt_session._session = cast(Any, _Session())
    cast(Any, prompt_session)._build_user_input = lambda command: command

    assert await prompt_session._prompt_once(append_history=False) == "hello"
    assert captured["set_exception_handler"] is False
