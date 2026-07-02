from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from pythinker_host.windows import windows_console_detach_flags

from pythinker_code.utils.logging import logger
from pythinker_code.utils.subprocess_env import get_clean_env, scrub_secret_env

from .models import TaskControl, TaskRuntime
from .store import BackgroundTaskStore


def terminate_process_tree_windows(pid: int, *, force: bool) -> None:
    args = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        args.append("/F")
    subprocess.run(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


async def run_background_task_worker(
    task_dir: Path,
    *,
    heartbeat_interval_ms: int = 5000,
    control_poll_interval_ms: int = 500,
    kill_grace_period_ms: int = 2000,
    max_output_bytes: int = 0,
) -> None:
    task_dir = task_dir.expanduser().resolve()
    task_id = task_dir.name
    store = BackgroundTaskStore(task_dir.parent)
    spec = store.read_spec(task_id)
    runtime = store.read_runtime(task_id)

    runtime.status = "starting"
    runtime.worker_pid = os.getpid()
    runtime.started_at = time.time()
    runtime.heartbeat_at = runtime.started_at
    runtime.updated_at = runtime.started_at
    store.write_runtime(task_id, runtime)

    control = store.read_control(task_id)
    if control.kill_requested_at is not None:
        runtime.status = "killed"
        runtime.interrupted = True
        runtime.finished_at = time.time()
        runtime.updated_at = runtime.finished_at
        runtime.failure_reason = control.kill_reason or "Killed before command start"
        store.write_runtime(task_id, runtime)
        return

    if spec.command is None or spec.shell_path is None or spec.cwd is None:
        runtime.status = "failed"
        runtime.finished_at = time.time()
        runtime.updated_at = runtime.finished_at
        runtime.failure_reason = "Task spec is incomplete for bash worker"
        store.write_runtime(task_id, runtime)
        return

    process: asyncio.subprocess.Process | None = None
    control_task: asyncio.Task[None] | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    input_task: asyncio.Task[None] | None = None
    stop_event = asyncio.Event()
    kill_sent_at: float | None = None
    timed_out = False
    timeout_reason: str | None = None
    output_limit_exceeded = False
    output_limit_reason: str | None = None

    async def _heartbeat_loop() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(heartbeat_interval_ms / 1000)
            current = store.read_runtime(task_id)
            if current.finished_at is not None:
                return
            current.heartbeat_at = time.time()
            current.updated_at = current.heartbeat_at
            store.write_runtime(task_id, current)

    async def _terminate_process(force: bool = False) -> None:
        nonlocal kill_sent_at
        if process is None or process.returncode is not None:
            return
        kill_sent_at = kill_sent_at or time.time()

        try:
            if os.name == "nt":
                terminate_process_tree_windows(process.pid, force=force)
                return

            target_pgid = process.pid
            if force:
                os.killpg(target_pgid, signal.SIGKILL)
            else:
                os.killpg(target_pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    output_path = store.output_path(task_id)

    async def _check_output_limit() -> None:
        """Terminate the task if its output.log grew past ``max_output_bytes``.

        Writes a single marker line and asks the process to terminate the first
        time the limit is hit; the final runtime write records the failure once
        the process has exited.
        """
        nonlocal output_limit_exceeded, output_limit_reason
        if max_output_bytes <= 0 or output_limit_exceeded:
            return
        if process is None or process.returncode is not None:
            return
        try:
            size = output_path.stat().st_size
        except OSError:
            return
        if size <= max_output_bytes:
            return

        output_limit_exceeded = True
        output_limit_reason = f"Output exceeded max_output_bytes ({max_output_bytes})"
        marker = f"\n... output limit exceeded ({size} bytes); task terminated ...\n"
        with contextlib.suppress(OSError), output_path.open("ab") as marker_file:
            marker_file.write(marker.encode("utf-8"))
        await _terminate_process(force=False)

    async def _control_loop() -> None:
        nonlocal kill_sent_at
        while not stop_event.is_set():
            await asyncio.sleep(control_poll_interval_ms / 1000)
            await _check_output_limit()
            if output_limit_exceeded and (
                kill_sent_at is not None
                and process is not None
                and process.returncode is None
                and time.time() - kill_sent_at >= kill_grace_period_ms / 1000
            ):
                await _terminate_process(force=True)
            current_control: TaskControl = store.read_control(task_id)
            if current_control.kill_requested_at is not None:
                await _terminate_process(force=current_control.force)
                if (
                    kill_sent_at is not None
                    and process is not None
                    and process.returncode is None
                    and time.time() - kill_sent_at >= kill_grace_period_ms / 1000
                ):
                    await _terminate_process(force=True)

    async def _input_loop() -> None:
        seen_event_ids: set[str] = set()
        while not stop_event.is_set():
            await asyncio.sleep(control_poll_interval_ms / 1000)
            if process is None or process.returncode is not None or process.stdin is None:
                return
            for event in store.read_input_events(task_id):
                if event.id in seen_event_ids:
                    continue
                seen_event_ids.add(event.id)
                payload = event.text + ("\n" if event.newline else "")
                try:
                    process.stdin.write(payload.encode("utf-8"))
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    return

    try:
        with output_path.open("ab") as output_file:
            spawn_kwargs: dict[str, Any] = {
                "stdin": asyncio.subprocess.PIPE,
                "stdout": output_file,
                "stderr": output_file,
                "cwd": spec.cwd,
                "env": scrub_secret_env(get_clean_env()) if spec.scrub_secrets else get_clean_env(),
            }
            if os.name == "nt":
                # CREATE_NO_WINDOW: don't share the interactive console — a child
                # touching it via the Win32 console API bypasses the redirected
                # stdio and can blank the parent TUI until terminal restart.
                spawn_kwargs["creationflags"] = windows_console_detach_flags()
            else:
                spawn_kwargs["start_new_session"] = True

            args = (
                (spec.shell_path, "-command", spec.command)
                if spec.shell_name == "Windows PowerShell"
                else (spec.shell_path, "-c", spec.command)
            )
            process = await asyncio.create_subprocess_exec(*args, **spawn_kwargs)

            runtime = store.read_runtime(task_id)
            runtime.status = "running"
            runtime.child_pid = process.pid
            runtime.child_pgid = process.pid if os.name != "nt" else None
            runtime.updated_at = time.time()
            runtime.heartbeat_at = runtime.updated_at
            store.write_runtime(task_id, runtime)

            heartbeat_task = asyncio.create_task(_heartbeat_loop())
            control_task = asyncio.create_task(_control_loop())
            input_task = asyncio.create_task(_input_loop())
            if spec.timeout_s is None:
                returncode = await process.wait()
            else:
                try:
                    returncode = await asyncio.wait_for(process.wait(), timeout=spec.timeout_s)
                except TimeoutError:
                    timed_out = True
                    timeout_reason = f"Command timed out after {spec.timeout_s}s"
                    await _terminate_process(force=False)
                    try:
                        returncode = await asyncio.wait_for(
                            process.wait(),
                            timeout=kill_grace_period_ms / 1000,
                        )
                    except TimeoutError:
                        await _terminate_process(force=True)
                        returncode = await process.wait()
    except Exception as exc:
        logger.exception("Background task worker failed")
        runtime = store.read_runtime(task_id)
        runtime.status = "failed"
        runtime.finished_at = time.time()
        runtime.updated_at = runtime.finished_at
        runtime.failure_reason = str(exc)
        store.write_runtime(task_id, runtime)
        return
    finally:
        stop_event.set()
        if process is not None and process.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                process.stdin.close()
                await process.stdin.wait_closed()
        for task in (heartbeat_task, control_task, input_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    control = store.read_control(task_id)

    def finish_runtime(runtime: TaskRuntime) -> bool:
        runtime.finished_at = time.time()
        runtime.updated_at = runtime.finished_at
        runtime.exit_code = returncode
        runtime.heartbeat_at = runtime.finished_at
        if output_limit_exceeded:
            runtime.status = "failed"
            runtime.interrupted = True
            runtime.failure_reason = output_limit_reason
        elif timed_out:
            runtime.status = "failed"
            runtime.interrupted = True
            runtime.timed_out = True
            runtime.failure_reason = timeout_reason
        elif control.kill_requested_at is not None:
            runtime.status = "killed"
            runtime.interrupted = True
            runtime.failure_reason = control.kill_reason or "Killed"
        elif returncode == 0:
            runtime.status = "completed"
            runtime.failure_reason = None
        else:
            runtime.status = "failed"
            runtime.failure_reason = f"Command failed with exit code {returncode}"
        return True

    store.update_runtime(task_id, finish_runtime)
