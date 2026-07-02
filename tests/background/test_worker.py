from __future__ import annotations

import asyncio
import time

import pytest

from pythinker_code.background import (
    BackgroundTaskStore,
    TaskControl,
    TaskSpec,
    run_background_task_worker,
)
from pythinker_code.background.worker import terminate_process_tree_windows


@pytest.mark.asyncio
async def test_worker_completes_successfully(runtime):
    store = BackgroundTaskStore(runtime.session.context_file.parent / "tasks")
    spec = TaskSpec(
        id="b3333333",
        kind="bash",
        session_id=runtime.session.id,
        description="echo hello",
        tool_call_id="tool-4",
        command="echo hello",
        shell_name="bash",
        shell_path="/bin/bash",
        cwd=str(runtime.session.work_dir),
        timeout_s=60,
    )
    store.create_task(spec)

    await run_background_task_worker(store.task_dir(spec.id), heartbeat_interval_ms=50)

    view = store.merged_view(spec.id)
    assert view.runtime.status == "completed"
    assert view.runtime.exit_code == 0
    assert "hello" in store.output_path(spec.id).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_worker_respects_kill_control(runtime):
    store = BackgroundTaskStore(runtime.session.context_file.parent / "tasks")
    spec = TaskSpec(
        id="b4444444",
        kind="bash",
        session_id=runtime.session.id,
        description="sleep task",
        tool_call_id="tool-5",
        command="sleep 5",
        shell_name="bash",
        shell_path="/bin/bash",
        cwd=str(runtime.session.work_dir),
        timeout_s=60,
    )
    store.create_task(spec)

    worker_task = asyncio.create_task(
        run_background_task_worker(
            store.task_dir(spec.id),
            heartbeat_interval_ms=50,
            control_poll_interval_ms=20,
            kill_grace_period_ms=50,
        )
    )
    await asyncio.sleep(0.2)
    store.write_control(
        spec.id,
        TaskControl(
            kill_requested_at=time.time(),
            kill_reason="stop test",
            force=False,
        ),
    )
    await worker_task

    view = store.merged_view(spec.id)
    assert view.runtime.status == "killed"
    assert view.runtime.interrupted is True
    assert view.runtime.failure_reason == "stop test"


@pytest.mark.asyncio
async def test_worker_marks_timeout_as_failed(runtime):
    store = BackgroundTaskStore(runtime.session.context_file.parent / "tasks")
    spec = TaskSpec(
        id="b5554444",
        kind="bash",
        session_id=runtime.session.id,
        description="timeout task",
        tool_call_id="tool-6",
        command="sleep 2",
        shell_name="bash",
        shell_path="/bin/bash",
        cwd=str(runtime.session.work_dir),
        timeout_s=1,
    )
    store.create_task(spec)

    await run_background_task_worker(
        store.task_dir(spec.id),
        heartbeat_interval_ms=50,
        control_poll_interval_ms=20,
        kill_grace_period_ms=50,
    )

    view = store.merged_view(spec.id)
    assert view.runtime.status == "failed"
    assert view.runtime.interrupted is True
    assert view.runtime.timed_out is True
    assert view.runtime.failure_reason == "Command timed out after 1s"


@pytest.mark.asyncio
async def test_worker_bounds_output_log_growth(runtime):
    store = BackgroundTaskStore(runtime.session.context_file.parent / "tasks")
    spec = TaskSpec(
        id="b6664444",
        kind="bash",
        session_id=runtime.session.id,
        description="flood task",
        tool_call_id="tool-7",
        command="for i in $(seq 1 1000000); do echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; done",
        shell_name="bash",
        shell_path="/bin/bash",
        cwd=str(runtime.session.work_dir),
        timeout_s=60,
    )
    store.create_task(spec)

    cap = 2000
    await run_background_task_worker(
        store.task_dir(spec.id),
        heartbeat_interval_ms=50,
        control_poll_interval_ms=20,
        kill_grace_period_ms=50,
        max_output_bytes=cap,
    )

    view = store.merged_view(spec.id)
    assert view.runtime.status == "failed"
    assert view.runtime.interrupted is True
    assert view.runtime.failure_reason is not None
    assert "max_output_bytes" in view.runtime.failure_reason

    output_path = store.output_path(spec.id)
    text = output_path.read_text(encoding="utf-8")
    assert "output limit exceeded" in text
    # The cap is enforced by a poll loop, not per-write, so the file overshoots
    # the cap by whatever the child flushes between polls (and that window
    # stretches under CPU contention). The guarantee is that growth is
    # *bounded* far below the ~30 MB this command writes uncapped; a few MB of
    # overshoot still proves the limiter fired and terminated the task.
    assert output_path.stat().st_size <= 5 * 1024 * 1024


def test_terminate_process_tree_windows_uses_taskkill_tree(monkeypatch):
    calls: list[list[str]] = []

    def _run(args, **kwargs):
        calls.append(args)
        return None

    monkeypatch.setattr("pythinker_code.background.worker.subprocess.run", _run)

    terminate_process_tree_windows(1234, force=False)
    terminate_process_tree_windows(1234, force=True)

    assert calls == [
        ["taskkill", "/PID", "1234", "/T"],
        ["taskkill", "/PID", "1234", "/T", "/F"],
    ]


@pytest.mark.asyncio
async def test_worker_spawn_detaches_windows_console(runtime, monkeypatch):
    """The spawned child must not attach to the interactive console on Windows.

    A child sharing the TUI's console can corrupt it via the Win32 console
    API even with redirected stdio; CREATE_NO_WINDOW detaches it.
    """
    import os as real_os
    from types import SimpleNamespace

    from pythinker_code.background import worker as worker_module

    store = BackgroundTaskStore(runtime.session.context_file.parent / "tasks")
    spec = TaskSpec(
        id="b5555555",
        kind="bash",
        session_id=runtime.session.id,
        description="echo hello",
        tool_call_id="tool-9",
        command="echo hello",
        shell_name="bash",
        shell_path="/bin/bash",
        cwd=str(runtime.session.work_dir),
        timeout_s=60,
    )
    store.create_task(spec)

    captured: dict[str, object] = {}

    async def _capture_and_abort(*args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("spawn intercepted by test")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _capture_and_abort)
    # Swap the module's `os` reference rather than mutating the global
    # `os.name`, which corrupts pathlib/pytest path handling mid-run.
    monkeypatch.setattr(worker_module, "os", SimpleNamespace(name="nt", getpid=real_os.getpid))

    await run_background_task_worker(store.task_dir(spec.id), heartbeat_interval_ms=50)

    flags = captured["creationflags"]
    assert isinstance(flags, int)
    assert flags & 0x08000000  # CREATE_NO_WINDOW
    assert "start_new_session" not in captured
    # The intercepted spawn surfaced as an explicit failure, not silent success.
    assert store.merged_view(spec.id).runtime.status == "failed"
