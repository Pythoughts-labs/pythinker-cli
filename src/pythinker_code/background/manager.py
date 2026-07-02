from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pythinker_host.local import local_host

from pythinker_code.config import BackgroundConfig
from pythinker_code.notifications import NotificationEvent, NotificationManager
from pythinker_code.session import Session
from pythinker_code.utils.logging import logger

if TYPE_CHECKING:
    from pythinker_code.soul.agent import Runtime
    from pythinker_code.subagents.models import SubagentStatus

from .ids import generate_task_id
from .models import (
    TaskInputEvent,
    TaskOutputChunk,
    TaskRuntime,
    TaskSpec,
    TaskStatus,
    TaskView,
    is_terminal_status,
)
from .store import BackgroundTaskStore

AgentTaskOutcome = Literal["completed", "failed", "timed_out", "killed"]


def _subagent_status_for_task_status(status: TaskStatus) -> SubagentStatus | None:
    """Map an authoritative agent ``TaskStatus`` to the subagent instance status
    it implies, for crash-recovery reconciliation.

    Returns ``None`` for statuses that imply no reconciliation. ``recoverable``
    and ``completed`` park the instance at ``idle`` (resumable / done-clean);
    ``killed`` and the failure family (``failed``/``lost``) map straight through.
    """
    if status in ("completed", "recoverable"):
        return "idle"
    if status == "killed":
        return "killed"
    if status in ("failed", "lost"):
        return "failed"
    return None


class BackgroundTaskManager:
    def __init__(
        self,
        session: Session,
        config: BackgroundConfig,
        *,
        notifications: NotificationManager,
        owner_role: str = "root",
    ) -> None:
        self._session = session
        self._config = config
        self._notifications = notifications
        self._owner_role = owner_role
        self._store = BackgroundTaskStore(session.context_file.parent / "tasks")
        self._runtime: Runtime | None = None
        self._live_agent_tasks: dict[str, asyncio.Task[None]] = {}
        self._current_turn_task_ids: set[str] = set()
        self._completion_event: asyncio.Event = asyncio.Event()
        self._nonblocking_polls: dict[str, int] = {}
        """Consecutive non-blocking TaskOutput polls per still-running task."""
        self._blocking_timeouts: dict[str, int] = {}
        """Consecutive timed-out blocking TaskOutput waits per still-running task."""

    @property
    def completion_event(self) -> asyncio.Event:
        """Event set when a new terminal notification is published.

        Not set immediately when a task becomes terminal — only after
        ``reconcile()`` / ``publish_terminal_notifications()`` runs.
        Deduplicated notifications do not trigger a repeat signal.
        """
        return self._completion_event

    @property
    def store(self) -> BackgroundTaskStore:
        return self._store

    @property
    def role(self) -> str:
        return self._owner_role

    def copy_for_role(self, role: str) -> BackgroundTaskManager:
        manager = BackgroundTaskManager(
            self._session,
            self._config,
            notifications=self._notifications,
            owner_role=role,
        )
        manager._runtime = self._runtime
        # Share the live-task registry: orphan detection is "non-terminal and
        # not in _live_agent_tasks", so a copy with an empty dict would let a
        # reconcile through it finalize the root's actively running agents as
        # recoverable — enabling a corrupting double-resume.
        manager._live_agent_tasks = self._live_agent_tasks
        manager._current_turn_task_ids = self._current_turn_task_ids
        manager._nonblocking_polls = self._nonblocking_polls
        manager._blocking_timeouts = self._blocking_timeouts
        return manager

    def note_nonblocking_poll(self, task_id: str) -> int:
        """Record a non-blocking poll on a still-running task; returns the streak count."""
        count = self._nonblocking_polls.get(task_id, 0) + 1
        self._nonblocking_polls[task_id] = count
        return count

    def note_blocking_timeout(self, task_id: str) -> int:
        """Record a blocking wait that timed out on a still-running task; returns the streak."""
        count = self._blocking_timeouts.get(task_id, 0) + 1
        self._blocking_timeouts[task_id] = count
        return count

    def reset_poll_escalation(self, task_id: str) -> None:
        """Clear both wait-escalation streaks after a terminal retrieval.

        A timed-out blocking wait deliberately does NOT reset the non-blocking
        streak: interleaving one blocking attempt between polls must not absolve
        the "STOP polling" escalation.
        """
        self._nonblocking_polls.pop(task_id, None)
        self._blocking_timeouts.pop(task_id, None)

    def bind_runtime(self, runtime: Runtime) -> None:
        self._runtime = runtime

    def _ensure_root(self) -> None:
        if self._owner_role != "root":
            raise RuntimeError("Background tasks are only supported from the root agent.")

    def _ensure_local_backend(self) -> None:
        if self._session.work_dir_meta.host != local_host.name:
            raise RuntimeError("Background tasks are only supported on local sessions.")

    def _journal_task_milestone(self, title: str, view: TaskView) -> None:
        if self._owner_role != "root":
            return
        try:
            from pythinker_code.scratchpad import append_scratch_event_sync

            details = [
                f"task: {view.spec.id}",
                f"kind: {view.spec.kind}",
                f"status: {view.runtime.status}",
                f"description: {view.spec.description}",
            ]
            if view.spec.kind == "agent":
                payload = view.spec.kind_payload or {}
                agent_id = payload.get("agent_id")
                subagent_type = payload.get("subagent_type")
                if isinstance(agent_id, str):
                    details.append(f"agent: {agent_id}")
                if isinstance(subagent_type, str):
                    details.append(f"type: {subagent_type}")
            if view.runtime.exit_code is not None:
                details.append(f"exit_code: {view.runtime.exit_code}")
            if view.runtime.failure_reason:
                details.append(f"reason: {view.runtime.failure_reason}")
            append_scratch_event_sync(
                self._session.work_dir,
                session_id=self._session.id,
                session_title=self._session.title or self._session.state.custom_title,
                labels=[f"kind:{view.spec.kind}"],
                title=title,
                details=details,
            )
        except Exception:
            logger.debug("background task scratchpad journal failed")

    def _active_task_count(self) -> int:
        return sum(
            1 for view in self._store.list_views() if not is_terminal_status(view.runtime.status)
        )

    def active_task_count(self) -> int:
        """Return the number of non-terminal background tasks."""
        return self._active_task_count()

    def available_task_slots(self) -> int:
        """Return how many new background tasks can be started right now."""
        return max(0, self._config.max_running_tasks - self._active_task_count())

    def has_active_tasks(self) -> bool:
        """Return True if any background tasks are in a non-terminal status.

        This includes ``running``, ``awaiting_approval``, and any other
        non-terminal state — not just actively executing tasks.
        """
        return self._active_task_count() > 0

    def _worker_command(self, task_dir: Path) -> list[str]:
        if getattr(sys, "frozen", False):
            return [
                sys.executable,
                "__background-task-worker",
                "--task-dir",
                str(task_dir),
                "--heartbeat-interval-ms",
                str(self._config.worker_heartbeat_interval_ms),
                "--control-poll-interval-ms",
                str(self._config.wait_poll_interval_ms),
                "--kill-grace-period-ms",
                str(self._config.kill_grace_period_ms),
                "--max-output-bytes",
                str(self._config.max_output_bytes),
            ]
        return [
            sys.executable,
            "-m",
            "pythinker_code.cli",
            "__background-task-worker",
            "--task-dir",
            str(task_dir),
            "--heartbeat-interval-ms",
            str(self._config.worker_heartbeat_interval_ms),
            "--control-poll-interval-ms",
            str(self._config.wait_poll_interval_ms),
            "--kill-grace-period-ms",
            str(self._config.kill_grace_period_ms),
            "--max-output-bytes",
            str(self._config.max_output_bytes),
        ]

    def _launch_worker(self, task_dir: Path) -> int:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": str(task_dir),
        }
        if os.name == "nt":
            # CREATE_NO_WINDOW: don't share the interactive console — a child
            # touching it via the Win32 console API bypasses DEVNULL stdio and
            # can blank the parent TUI until terminal restart.
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0x08000000
            )
        else:
            kwargs["start_new_session"] = True

        process = subprocess.Popen(self._worker_command(task_dir), **kwargs)
        return process.pid

    def create_bash_task(
        self,
        *,
        command: str,
        description: str,
        timeout_s: int,
        tool_call_id: str,
        shell_name: str,
        shell_path: str,
        cwd: str,
        scrub_secrets: bool = False,
    ) -> TaskView:
        self._ensure_root()
        self._ensure_local_backend()

        if self._active_task_count() >= self._config.max_running_tasks:
            raise RuntimeError("Too many background tasks are already running.")

        task_id = generate_task_id("bash", used=self._store.list_task_ids())
        spec = TaskSpec(
            id=task_id,
            kind="bash",
            session_id=self._session.id,
            description=description,
            tool_call_id=tool_call_id,
            owner_role="root",
            command=command,
            shell_name=shell_name,
            shell_path=shell_path,
            cwd=cwd,
            timeout_s=timeout_s,
            scrub_secrets=scrub_secrets,
        )
        self._store.create_task(spec)
        from pythinker_code.telemetry import track

        track("background_task_created")

        runtime = self._store.read_runtime(task_id)
        task_dir = self._store.task_dir(task_id)
        try:
            worker_pid = self._launch_worker(task_dir)
        except Exception as exc:
            runtime.status = "failed"
            runtime.failure_reason = f"Failed to launch worker: {exc}"
            runtime.finished_at = time.time()
            runtime.updated_at = runtime.finished_at
            self._store.write_runtime(task_id, runtime)
            raise

        def mark_worker_started(runtime: TaskRuntime) -> bool:
            if runtime.finished_at is not None:
                return False
            if runtime.status != "created" and not (
                runtime.status == "starting" and runtime.worker_pid is None
            ):
                return False
            runtime.status = "starting"
            runtime.worker_pid = worker_pid
            runtime.updated_at = time.time()
            return True

        self._store.update_runtime(task_id, mark_worker_started)
        self._current_turn_task_ids.add(task_id)
        view = self._store.merged_view(task_id)
        self._journal_task_milestone("background task started", view)
        return view

    def create_agent_task(
        self,
        *,
        agent_id: str,
        subagent_type: str,
        prompt: str,
        description: str,
        tool_call_id: str,
        model_override: str | None,
        timeout_s: int | None = None,
        resumed: bool = False,
        dependencies: list[str] | None = None,
        budget_seconds: int | None = None,
        isolation: str | None = None,
    ) -> TaskView:
        from .agent_runner import BackgroundAgentRunner

        self._ensure_root()
        self._ensure_local_backend()
        if self._runtime is None:
            raise RuntimeError("Background task manager is not bound to a runtime.")
        if self._active_task_count() >= self._config.max_running_tasks:
            raise RuntimeError("Too many background tasks are already running.")

        task_id = generate_task_id("agent", used=self._store.list_task_ids())
        # Explicit None check — the falsy idiom ``timeout_s or default``
        # would silently promote a caller-supplied ``0`` to the agent
        # default, matching the analogous fix in Print's wait-cap reader.
        effective_timeout = (
            timeout_s if timeout_s is not None else self._config.agent_task_timeout_s
        )
        spec = TaskSpec(
            id=task_id,
            kind="agent",
            session_id=self._session.id,
            description=description,
            tool_call_id=tool_call_id,
            owner_role="root",
            # Persist the effective timeout so downstream readers (e.g. the
            # Print-mode ``print_wait_ceiling_s`` cap calculation) can honour
            # an explicit per-agent timeout instead of always falling back to
            # ``config.background.agent_task_timeout_s``.
            timeout_s=effective_timeout,
            dependencies=list(dependencies or ()),
            budget_seconds=budget_seconds,
            synthesis_state="pending",
            isolation=isolation,
            kind_payload={
                "agent_id": agent_id,
                "subagent_type": subagent_type,
                "prompt": prompt,
                "model_override": model_override,
                "launch_mode": "background",
                "dependencies": list(dependencies or ()),
                "budget_seconds": budget_seconds,
                "isolation": isolation,
            },
        )
        self._store.create_task(spec)

        def mark_agent_starting(runtime: TaskRuntime) -> bool:
            if is_terminal_status(runtime.status):
                return False
            runtime.status = "starting"
            runtime.updated_at = time.time()
            return True

        self._store.update_runtime(task_id, mark_agent_starting)
        task = asyncio.create_task(
            BackgroundAgentRunner(
                runtime=self._runtime,
                manager=self,
                task_id=task_id,
                agent_id=agent_id,
                subagent_type=subagent_type,
                prompt=prompt,
                model_override=model_override,
                timeout_s=effective_timeout,
                resumed=resumed,
                isolation=isolation,
            ).run()
        )
        self._live_agent_tasks[task_id] = task

        # Cleanup safety net for the case where the runner is cancelled before
        # its first event-loop step. Python throws CancelledError into a
        # FRAME_CREATED coroutine without executing any of the function body,
        # so the runner's finally block never runs and cannot pop the entry
        # itself. The done callback fires regardless of how the task ends, and
        # is idempotent with the runner's own pop (both use pop(..., None)).
        # It also retrieves a crash of the runner ITSELF (e.g. store I/O failing
        # inside an error handler) — otherwise the exception surfaces only as
        # GC-time "never retrieved" noise and the failure is silently lost.
        def _reap_agent_task(t: asyncio.Task[None], tid: str = task_id) -> None:
            self._live_agent_tasks.pop(tid, None)
            if not t.cancelled() and (exc := t.exception()) is not None:
                logger.exception(
                    "Background agent runner for task {tid} crashed", tid=tid, exception=exc
                )

        task.add_done_callback(_reap_agent_task)
        self._current_turn_task_ids.add(task_id)
        view = self._store.merged_view(task_id)
        self._journal_task_milestone("background task started", view)
        return view

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        limit: int | None = 20,
    ) -> list[TaskView]:
        tasks = self._store.list_views()
        if status is not None:
            tasks = [task for task in tasks if task.runtime.status == status]
        if limit is None:
            return tasks
        return tasks[:limit]

    def get_task(self, task_id: str) -> TaskView | None:
        try:
            return self._store.merged_view(task_id)
        except (FileNotFoundError, ValueError):
            return None

    def resolve_output_path(self, task_id: str) -> Path:
        """Return the canonical output path for *task_id*."""
        return self._store.output_path(task_id)

    def write_input(self, task_id: str, *, text: str, newline: bool = True) -> TaskInputEvent:
        self._ensure_root()
        view = self._store.merged_view(task_id)
        if view.spec.kind != "bash":
            raise RuntimeError("TaskInput is only supported for bash background tasks.")
        if is_terminal_status(view.runtime.status):
            raise RuntimeError(f"Task {task_id} is already terminal ({view.runtime.status}).")
        if view.runtime.status not in {"starting", "running"}:
            raise RuntimeError(f"Task {task_id} is not ready for input ({view.runtime.status}).")
        event = TaskInputEvent(
            id=f"i{time.time_ns()}",
            text=text,
            newline=newline,
        )
        self._store.append_input_event(task_id, event)
        return event

    def read_output(
        self,
        task_id: str,
        *,
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> TaskOutputChunk:
        view = self._store.merged_view(task_id)
        return self._store.read_output(
            task_id,
            offset,
            max_bytes or self._config.read_max_bytes,
            status=view.runtime.status,
        )

    def tail_output(
        self,
        task_id: str,
        *,
        max_bytes: int | None = None,
        max_lines: int | None = None,
    ) -> str:
        self._store.merged_view(task_id)
        return self._store.tail_output(
            task_id,
            max_bytes=max_bytes or self._config.read_max_bytes,
            max_lines=max_lines or self._config.notification_tail_lines,
        )

    async def wait(self, task_id: str, *, timeout_s: int = 30) -> TaskView:
        end_time = time.monotonic() + timeout_s
        while True:
            view = self._store.merged_view(task_id)
            if is_terminal_status(view.runtime.status):
                return view
            if time.monotonic() >= end_time:
                return view
            await asyncio.sleep(self._config.wait_poll_interval_ms / 1000)

    _SIGKILL_ESCALATION_DELAY_S = 5.0

    def _best_effort_kill(self, task_id: str, runtime: TaskRuntime) -> None:
        try:
            if os.name == "nt":
                pid = runtime.child_pid or runtime.worker_pid
                if pid is None:
                    return
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                return

            if runtime.child_pgid is not None:
                os.killpg(runtime.child_pgid, signal.SIGTERM)
                self._schedule_sigkill(task_id, pgid=runtime.child_pgid)
                return
            if runtime.child_pid is not None:
                os.kill(runtime.child_pid, signal.SIGTERM)
                self._schedule_sigkill(task_id, pid=runtime.child_pid)
        except ProcessLookupError:
            pass
        except Exception:
            logger.exception("Failed to send best-effort kill signal")

    def _schedule_sigkill(
        self, task_id: str, *, pgid: int | None = None, pid: int | None = None
    ) -> None:
        """Escalate to SIGKILL after a grace delay if the task is still running."""
        t = threading.Timer(
            self._SIGKILL_ESCALATION_DELAY_S,
            self._escalate_sigkill,
            kwargs={"task_id": task_id, "pgid": pgid, "pid": pid},
        )
        t.daemon = True
        t.start()

    def _escalate_sigkill(
        self, task_id: str, *, pgid: int | None = None, pid: int | None = None
    ) -> None:
        """SIGKILL the process group/pid, but only if the task is still running.

        Re-reading the task status first avoids two problems with a blind timer:
        (a) sending a needless SIGKILL on the common path where SIGTERM already
        worked, and (b) signaling a pgid/pid the OS may have recycled once the
        original child exited and was reaped by its parent worker. The worker
        owns the child and runs its own SIGTERM->SIGKILL escalation, so this is
        only a fallback for when the worker itself is gone.
        """
        try:
            if is_terminal_status(self._store.read_runtime(task_id).status):
                return
            if pgid is not None:
                os.killpg(pgid, signal.SIGKILL)
            elif pid is not None:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            logger.warning("Failed to escalate kill signal to SIGKILL")

    def kill(self, task_id: str, *, reason: str = "Killed by user") -> TaskView:
        self._ensure_root()
        view = self._store.merged_view(task_id)
        if is_terminal_status(view.runtime.status):
            return view

        if view.spec.kind == "agent":
            self._mark_task_killed(task_id, reason)
            if self._runtime is not None and self._runtime.approval_runtime is not None:
                self._runtime.approval_runtime.cancel_by_source("background_agent", task_id)
            # Keep the task in _live_agent_tasks until BackgroundAgentRunner's
            # finally block removes it. asyncio holds tasks in a WeakSet, so if
            # we drop the only strong reference here the still-pending task can
            # be garbage-collected before cancellation propagates, which fires
            # loop.call_exception_handler with no 'exception' field — surfacing
            # as "Unhandled exception in event loop / Exception None" in the
            # prompt_toolkit terminal.
            task = self._live_agent_tasks.get(task_id)
            if task is not None:
                task.cancel()
            return self._store.merged_view(task_id)

        control = view.control.model_copy(
            update={
                "kill_requested_at": time.time(),
                "kill_reason": reason,
                "force": False,
            }
        )
        self._store.write_control(task_id, control)
        self._best_effort_kill(task_id, view.runtime)
        return self._store.merged_view(task_id)

    def begin_turn(self) -> None:
        """Mark the start of an interactive turn.

        Tasks created after this call belong to the turn and are killed by
        ``kill_turn_tasks`` if the user interrupts it. Tasks from earlier
        turns are deliberately left alone — an ESC means "stop what you are
        doing now", not "tear down everything I started before".
        """
        self._current_turn_task_ids.clear()

    def kill_turn_tasks(self, *, reason: str = "Interrupted by user") -> list[str]:
        """Kill still-active background tasks spawned during the current turn."""
        killed: list[str] = []
        for task_id in sorted(self._current_turn_task_ids):
            try:
                view = self._store.merged_view(task_id)
                if is_terminal_status(view.runtime.status):
                    continue
                self.kill(task_id, reason=reason)
                killed.append(task_id)
            except Exception:
                logger.exception("Failed to kill turn task {task_id} on interrupt", task_id=task_id)
        self._current_turn_task_ids.clear()
        return killed

    def kill_all_active(self, *, reason: str = "CLI session ended") -> list[str]:
        """Kill all non-terminal background tasks. Used during CLI shutdown."""
        killed: list[str] = []
        for view in self._store.list_views():
            if is_terminal_status(view.runtime.status):
                continue
            try:
                self.kill(view.spec.id, reason=reason)
                killed.append(view.spec.id)
            except Exception:
                logger.exception(
                    "Failed to kill task {task_id} during shutdown",
                    task_id=view.spec.id,
                )
        return killed

    def recover(self) -> None:
        now = time.time()
        stale_after = self._config.worker_stale_after_ms / 1000
        views = self._store.list_views()
        # agent_ids owned by a live in-process task. A terminal task that reused
        # the same agent_id (e.g. a prior run before a background resume) must
        # not reconcile its status onto the *live* agent's record.
        live_agent_ids = {
            agent_id
            for view in views
            if view.spec.id in self._live_agent_tasks
            and isinstance(agent_id := (view.spec.kind_payload or {}).get("agent_id"), str)
        }
        for view in views:
            if view.spec.kind == "agent":
                # Agent tasks are handled before the terminal-skip below so a
                # terminal task whose subagent record is still stuck at
                # running_background (crash after the authoritative TaskRuntime
                # write, or after kill() but before the runner's cancel handler)
                # still gets reconciled.
                self._recover_agent_view(view, now=now, live_agent_ids=live_agent_ids)
                continue
            if is_terminal_status(view.runtime.status):
                continue
            last_progress_at = (
                view.runtime.heartbeat_at
                or view.runtime.started_at
                or view.runtime.updated_at
                or view.spec.created_at
            )
            if now - last_progress_at <= stale_after:
                continue

            # Hold the cross-process lock for the read-then-write to eliminate
            # the race with a worker that heartbeats between our staleness check
            # and the status write.
            with self._store._runtime_lock(view.spec.id):  # pyright: ignore[reportPrivateUsage]
                fresh_runtime = self._store.read_runtime(view.spec.id)
                fresh_control = self._store.read_control(view.spec.id)
                if is_terminal_status(fresh_runtime.status):
                    continue
                fresh_progress = (
                    fresh_runtime.heartbeat_at
                    or fresh_runtime.started_at
                    or fresh_runtime.updated_at
                    or view.spec.created_at
                )
                if now - fresh_progress <= stale_after:
                    continue

                runtime = fresh_runtime.model_copy()
                runtime.finished_at = now
                runtime.updated_at = now
                if fresh_control.kill_requested_at is not None:
                    runtime.status = "killed"
                    runtime.interrupted = True
                    runtime.failure_reason = fresh_control.kill_reason or "Killed during recovery"
                else:
                    runtime.status = "lost"
                    runtime.failure_reason = (
                        "Background worker never heartbeat after startup"
                        if fresh_runtime.heartbeat_at is None
                        else "Background worker heartbeat expired"
                    )
                self._store._write_runtime_unlocked(view.spec.id, runtime)  # pyright: ignore[reportPrivateUsage]

    def _recover_agent_view(self, view: TaskView, *, now: float, live_agent_ids: set[str]) -> None:
        """Recover an in-process agent task and reconcile its subagent record.

        An orphaned task (non-terminal, no live asyncio task) gets an
        authoritative terminal status re-derived here. Either way the subagent
        instance record is reconciled to agree with the authoritative
        ``TaskRuntime`` — closing the window where a process crash between
        :meth:`finalize_agent_task`'s two writes, or between :meth:`kill` and the
        runner's cancellation handler, left the two records divergent.

        The runtime is re-read under the store's update lock before recovery so a
        stale ``TaskView`` snapshot cannot overwrite an authoritative terminal
        status that was written after the snapshot was created.
        """
        agent_id_raw = (view.spec.kind_payload or {}).get("agent_id")
        agent_id = agent_id_raw if isinstance(agent_id_raw, str) else None

        def recover_orphan(runtime: TaskRuntime) -> bool:
            if is_terminal_status(runtime.status) or view.spec.id in self._live_agent_tasks:
                return False
            runtime.finished_at = now
            runtime.updated_at = now
            runtime.status = "recoverable" if agent_id is not None else "lost"
            runtime.failure_reason = (
                "In-process background agent is no longer running; resume the stored agent "
                f"instance {agent_id} to continue."
                if agent_id is not None
                else "In-process background agent is no longer running"
            )
            return True

        runtime = self._store.update_runtime(view.spec.id, recover_orphan)
        self._reconcile_subagent_status(agent_id, runtime.status, live_agent_ids)

    def _reconcile_subagent_status(
        self, agent_id: str | None, runtime_status: TaskStatus, live_agent_ids: set[str]
    ) -> None:
        """Bring a subagent instance record into agreement with the authoritative
        agent ``TaskStatus``.

        Skipped when ``agent_id`` is owned by a live in-process task — the same
        agent_id may have been reused by a newer, currently-running task (e.g. a
        background resume), so the ``running_background`` record belongs to that
        live run, not to this (older, terminal) view. Otherwise only a record
        still parked at ``running_background`` is touched, so a record resumed in
        the foreground (``running_foreground``) or already settled to a terminal
        status is never clobbered. This is safe because a background agent's
        record stays ``running_background`` for the whole run, so an interrupted
        finalize always leaves it there.
        """
        if agent_id is None or agent_id in live_agent_ids:
            return
        if self._runtime is None or self._runtime.subagent_store is None:
            return
        target = _subagent_status_for_task_status(runtime_status)
        if target is None:
            return
        record = self._runtime.subagent_store.get_instance(agent_id)
        if record is None or record.status != "running_background":
            return
        self._runtime.subagent_store.update_instance(agent_id, status=target)

    def find_agent_task_view(self, agent_id: str) -> TaskView | None:
        """Return the latest agent task (by creation time) linked to ``agent_id``.

        The link is the ``kind_payload["agent_id"]`` stamped by
        :meth:`create_agent_task`; the latest view wins because a resumed agent
        reuses its agent_id across multiple tasks.
        """
        candidates = [
            view
            for view in self._store.list_views()
            if view.spec.kind == "agent"
            and (view.spec.kind_payload or {}).get("agent_id") == agent_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda view: view.spec.created_at)

    def reconcile_stale_agent_record(self, agent_id: str) -> TaskView | None:
        """Mid-session, single-agent counterpart of :meth:`recover`.

        If the latest task linked to ``agent_id`` is already terminal and not
        owned by a live in-process run, settle a subagent record still parked at
        ``running_background`` so a resume attempt is not rejected against a
        record that otherwise only a process restart would reconcile. Returns
        the latest linked task view, or ``None`` when the agent has no task.
        """
        view = self.find_agent_task_view(agent_id)
        if view is None:
            return None
        if view.spec.id in self._live_agent_tasks:
            return view
        runtime_status = self._store.read_runtime(view.spec.id).status
        if not is_terminal_status(runtime_status):
            return view
        self._reconcile_subagent_status(agent_id, runtime_status, set())
        return view

    def reconcile(self, *, limit: int | None = None) -> list[str]:
        self.recover()
        published = self.publish_terminal_notifications(limit=limit)
        # Opportunistic housekeeping: prune aged terminal task directories so the
        # store does not grow unbounded. Best-effort and bounded — cleanup skips
        # non-terminal tasks and any whose worker is still alive, and no-ops when
        # task_retention_days == 0. A failure here must never break reconcile.
        try:
            self._store.cleanup_old_tasks(self._config.task_retention_days)
        except Exception:
            logger.warning("Background task cleanup failed during reconcile")
        return published

    def publish_terminal_notifications(self, *, limit: int | None = None) -> list[str]:
        published: list[str] = []
        for view in self._store.list_views():
            if not is_terminal_status(view.runtime.status):
                continue

            # A terminal task can never be polled-while-running again, so its
            # escalation counter is dead weight; drop it here (not only in
            # TaskOutput) so tasks that finish unpolled don't leak entries.
            self._nonblocking_polls.pop(view.spec.id, None)

            status = view.runtime.status
            terminal_reason = "timed_out" if view.runtime.timed_out else status
            match terminal_reason:
                case "completed":
                    severity = "success"
                    title = f"Background task completed: {view.spec.description}"
                case "timed_out":
                    severity = "error"
                    title = f"Background task timed out: {view.spec.description}"
                case "failed":
                    severity = "error"
                    title = f"Background task failed: {view.spec.description}"
                case "killed":
                    severity = "warning"
                    title = f"Background task stopped: {view.spec.description}"
                case "lost":
                    severity = "warning"
                    title = f"Background task lost: {view.spec.description}"
                case "recoverable":
                    severity = "warning"
                    title = f"Background task recoverable: {view.spec.description}"
                case _:
                    severity = "info"
                    title = f"Background task updated: {view.spec.description}"

            body_lines = [
                f"Task ID: {view.spec.id}",
                f"Status: {status}",
                f"Description: {view.spec.description}",
            ]
            if view.spec.kind == "agent":
                # The agent id is the handle the parent needs to resume a
                # failed/timed-out agent instead of relaunching from scratch.
                payload = view.spec.kind_payload or {}
                agent_id = payload.get("agent_id")
                if isinstance(agent_id, str):
                    body_lines.append(f"Agent ID: {agent_id}")
            if terminal_reason != status:
                body_lines.append(f"Terminal reason: {terminal_reason}")
            if view.runtime.exit_code is not None:
                body_lines.append(f"Exit code: {view.runtime.exit_code}")
            if view.runtime.failure_reason:
                body_lines.append(f"Failure reason: {view.runtime.failure_reason}")
            output_path = self._store.output_path(view.spec.id)
            try:
                output_size = output_path.stat().st_size
            except OSError:
                output_size = 0
            if output_size > 0:
                body_lines.append(f"Output path: {output_path.resolve()}")
                body_lines.append(f"Output size bytes: {output_size}")

            event = NotificationEvent(
                id=self._notifications.new_id(),
                category="task",
                type=f"task.{terminal_reason}",
                source_kind="background_task",
                source_id=view.spec.id,
                title=title,
                body="\n".join(body_lines),
                severity=severity,
                payload={
                    "task_id": view.spec.id,
                    "task_kind": view.spec.kind,
                    "status": status,
                    "description": view.spec.description,
                    "exit_code": view.runtime.exit_code,
                    "interrupted": view.runtime.interrupted,
                    "timed_out": view.runtime.timed_out,
                    "terminal_reason": terminal_reason,
                    "failure_reason": view.runtime.failure_reason,
                    "output_path": str(output_path.resolve()) if output_size > 0 else None,
                    "output_size_bytes": output_size,
                },
                dedupe_key=f"background_task:{view.spec.id}:{terminal_reason}",
            )
            notification = self._notifications.publish(event)
            if notification.event.id == event.id:
                published.append(notification.event.id)
                self._journal_task_milestone(f"background task {terminal_reason}", view)
                self._completion_event.set()
            if limit is not None and len(published) >= limit:
                break
        return published

    def finalize_agent_task(
        self,
        task_id: str,
        agent_id: str | None,
        *,
        outcome: AgentTaskOutcome,
        reason: str | None = None,
    ) -> None:
        """Apply an in-process agent task's terminal outcome to both stores.

        The authoritative ``TaskRuntime`` is written FIRST and the derived
        subagent instance record LAST, in one fixed order, so different call
        sites can never interleave the pair in conflicting orders (the bug this
        replaces: ``run()`` wrote the record first, ``_run_core`` wrote the task
        first). A process crash between the two writes leaves the instance record
        at ``running_background``; :meth:`recover` then reconciles it to the
        status implied by the authoritative ``TaskRuntime``
        (see :func:`_subagent_status_for_task_status`). Callers must route every
        terminal agent update through here.
        """
        if outcome == "completed":
            self._mark_task_completed(task_id)
        elif outcome == "timed_out":
            self._mark_task_timed_out(task_id, reason or "Agent task timed out")
        elif outcome == "killed":
            self._mark_task_killed(task_id, reason or "Agent task stopped")
        else:
            self._mark_task_failed(task_id, reason or "Agent task failed")

        if (
            agent_id is not None
            and self._runtime is not None
            and self._runtime.subagent_store is not None
        ):
            # _mark_task_*() returns early when the task is already terminal, so a
            # kill/timeout race can leave the authoritative TaskRuntime at a
            # different terminal status than this call's `outcome`. Derive the
            # subagent status from the runtime that actually won (matching
            # recover()'s reconciliation) so the two records never diverge.
            final_status = self._store.read_runtime(task_id).status
            subagent_status = _subagent_status_for_task_status(final_status)
            if subagent_status is not None:
                self._runtime.subagent_store.update_instance(agent_id, status=subagent_status)

    def _transition_status(
        self,
        task_id: str,
        *,
        mutate: Callable[[TaskRuntime], None],
    ) -> TaskRuntime | None:
        """Locked, terminal-guarded status write via the public store API.

        Stamps ``updated_at`` then applies ``mutate``. Returns the resulting
        runtime when the transition was applied, or ``None`` when the task was
        already terminal (no write performed, so callers skip telemetry).
        """
        applied = False

        def _apply(runtime: TaskRuntime) -> bool:
            nonlocal applied
            if is_terminal_status(runtime.status):
                return False
            runtime.updated_at = time.time()
            mutate(runtime)
            applied = True
            return True

        runtime = self._store.update_runtime(task_id, _apply)
        return runtime if applied else None

    def _mark_task_running(self, task_id: str) -> None:
        def mutate(runtime: TaskRuntime) -> None:
            runtime.status = "running"
            runtime.heartbeat_at = runtime.updated_at
            runtime.failure_reason = None

        self._transition_status(task_id, mutate=mutate)

    def _mark_task_awaiting_approval(self, task_id: str, reason: str) -> None:
        def mutate(runtime: TaskRuntime) -> None:
            runtime.status = "awaiting_approval"
            runtime.failure_reason = reason

        self._transition_status(task_id, mutate=mutate)

    def _mark_task_completed(self, task_id: str) -> None:
        def mutate(runtime: TaskRuntime) -> None:
            runtime.status = "completed"
            runtime.finished_at = runtime.updated_at
            runtime.failure_reason = None

        runtime = self._transition_status(task_id, mutate=mutate)
        if runtime is not None and runtime.started_at and runtime.finished_at:
            from pythinker_code.telemetry import track

            track(
                "background_task_completed",
                success=True,
                duration_s=runtime.finished_at - runtime.started_at,
            )

    def _mark_task_failed(self, task_id: str, reason: str) -> None:
        def mutate(runtime: TaskRuntime) -> None:
            runtime.status = "failed"
            runtime.finished_at = runtime.updated_at
            runtime.failure_reason = reason

        runtime = self._transition_status(task_id, mutate=mutate)
        if runtime is not None and runtime.started_at and runtime.finished_at:
            from pythinker_code.telemetry import track

            track(
                "background_task_completed",
                success=False,
                duration_s=runtime.finished_at - runtime.started_at,
                reason="error",
            )

    def _mark_task_timed_out(self, task_id: str, reason: str) -> None:
        def mutate(runtime: TaskRuntime) -> None:
            runtime.status = "failed"
            runtime.finished_at = runtime.updated_at
            runtime.interrupted = True
            runtime.timed_out = True
            runtime.failure_reason = reason

        runtime = self._transition_status(task_id, mutate=mutate)
        if runtime is not None and runtime.started_at and runtime.finished_at:
            from pythinker_code.telemetry import track

            track(
                "background_task_completed",
                success=False,
                duration_s=runtime.finished_at - runtime.started_at,
                reason="timeout",
            )

    def _mark_task_killed(self, task_id: str, reason: str) -> None:
        def mutate(runtime: TaskRuntime) -> None:
            runtime.status = "killed"
            runtime.finished_at = runtime.updated_at
            runtime.interrupted = True
            runtime.failure_reason = reason

        runtime = self._transition_status(task_id, mutate=mutate)
        if runtime is not None and runtime.started_at and runtime.finished_at:
            from pythinker_code.telemetry import track

            track(
                "background_task_completed",
                success=False,
                duration_s=runtime.finished_at - runtime.started_at,
                reason="killed",
            )
