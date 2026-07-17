# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from pythinker_host.path import HostPath

from pythinker_code.approval_runtime import (
    ApprovalSource,
    reset_current_approval_source,
    set_current_approval_source,
)
from pythinker_code.soul import RunCancelled
from pythinker_code.subagents.builder import SubagentBuilder
from pythinker_code.subagents.core import SubagentRunSpec, prepare_soul
from pythinker_code.subagents.output import SubagentOutputWriter
from pythinker_code.subagents.review_target import ResolvedReviewTarget
from pythinker_code.subagents.runner import (
    _SUMMARY_MIN_LENGTH_BY_TYPE,
    _SUMMARY_MIN_LENGTH_DEFAULT,
    run_with_summary_continuation,
)
from pythinker_code.subagents.usage import format_usage_lines
from pythinker_code.utils.logging import logger
from pythinker_code.wire import Wire

if TYPE_CHECKING:
    from pythinker_code.approval_runtime.models import ApprovalRuntimeEvent
    from pythinker_code.background.manager import AgentTaskOutcome, BackgroundTaskManager
    from pythinker_code.soul.agent import Runtime


def _timeout_recovery_message(*, timeout_s: int | None, agent_id: str) -> str:
    timeout_text = f"{timeout_s}s" if timeout_s is not None else "the configured timeout"
    return (
        f"Agent task timed out after {timeout_text}.\n"
        "Recovery: the subagent context is still saved. Do not relaunch the same broad "
        "prompt unchanged. Run targeted direct scans, or resume the saved agent with a "
        f'narrower continuation prompt: Agent(resume="{agent_id}", prompt="...", '
        "timeout=3600)."
    )


def _failure_recovery_message(*, reason: str, agent_id: str) -> str:
    return (
        f"Agent task failed: {reason}\n"
        "Recovery: the subagent context is still saved. If the failure looks "
        "transient (rate limit, network), resume the saved agent instead of "
        f'relaunching the same prompt from scratch: Agent(resume="{agent_id}", '
        'prompt="...").'
    )


class BackgroundAgentRunner:
    def __init__(
        self,
        *,
        runtime: Runtime,
        manager: BackgroundTaskManager,
        task_id: str,
        agent_id: str,
        subagent_type: str,
        prompt: str,
        model_override: str | None,
        timeout_s: int | None = None,
        resumed: bool = False,
        isolation: str | None = None,
        resolved_review_target: ResolvedReviewTarget | None = None,
    ) -> None:
        self._runtime = runtime
        self._manager = manager
        self._task_id = task_id
        self._agent_id = agent_id
        self._subagent_type = subagent_type
        self._prompt = prompt
        self._model_override = model_override
        self._timeout_s = timeout_s
        self._resumed = resumed
        self._isolation = isolation
        self._resolved_review_target = resolved_review_target
        self._worktree_path: Path | None = None
        self._builder = SubagentBuilder(runtime)
        self._approval_update_tasks: set[asyncio.Task[None]] = set()

    def _finalize_safely(self, *, outcome: AgentTaskOutcome, reason: str | None = None) -> None:
        """Finalize without letting store I/O replace the original failure.

        An exception escaping finalize inside an error handler would kill the
        runner task, leave the record non-terminal until a later reconcile
        guesses "recoverable", and lose the real error entirely.
        """
        try:
            self._manager.finalize_agent_task(
                self._task_id, self._agent_id, outcome=outcome, reason=reason
            )
        except Exception:
            logger.exception("Failed to finalize background agent task {tid}", tid=self._task_id)

    async def run(self) -> None:
        assert self._runtime.approval_runtime is not None
        assert self._runtime.subagent_store is not None
        token = set_current_approval_source(
            ApprovalSource(
                kind="background_agent",
                id=self._task_id,
                agent_id=self._agent_id,
                subagent_type=self._subagent_type,
            )
        )
        approval_subscription = self._runtime.approval_runtime.subscribe(
            self._on_approval_runtime_event
        )
        task_output_path = self._manager.store.output_path(self._task_id)
        output = SubagentOutputWriter(
            self._runtime.subagent_store.output_path(self._agent_id),
            extra_paths=[task_output_path],
        )

        try:
            if self._timeout_s is not None:
                await asyncio.wait_for(self._run_core(output), timeout=self._timeout_s)
            else:
                await self._run_core(output)
        except TimeoutError as exc:
            if isinstance(exc.__cause__, asyncio.CancelledError):
                # Task-level timeout from wait_for (it raises TimeoutError from CancelledError)
                logger.warning(
                    "Background agent task {id} timed out after {t}s",
                    id=self._task_id,
                    t=self._timeout_s,
                )
                self._finalize_safely(
                    outcome="timed_out",
                    reason=f"Agent task timed out after {self._timeout_s}s",
                )
                output.error(
                    _timeout_recovery_message(timeout_s=self._timeout_s, agent_id=self._agent_id)
                )
                self._note_retained_worktree(output)
            else:
                # Internal timeout (e.g. aiohttp request) — treat as generic failure
                logger.exception("Background agent runner failed")
                self._finalize_safely(outcome="failed", reason=str(exc))
                output.error(_failure_recovery_message(reason=str(exc), agent_id=self._agent_id))
                self._note_retained_worktree(output)
        except asyncio.CancelledError:
            self._finalize_safely(outcome="killed", reason="Stopped by TaskStop")
            output.stage("cancelled")
            raise
        except RunCancelled:
            # RunCancelled is Exception (not BaseException), so re-raising it from
            # an asyncio.create_task would trigger "Task exception was never retrieved".
            # Just mark killed and return — cleanup is already done.
            self._finalize_safely(outcome="killed", reason="Run was cancelled")
            output.stage("cancelled")
        except Exception as exc:
            logger.exception("Background agent runner failed")
            self._finalize_safely(outcome="failed", reason=str(exc))
            output.error(_failure_recovery_message(reason=str(exc), agent_id=self._agent_id))
            self._note_retained_worktree(output)
        finally:
            # Whatever happens in approval cleanup below, the dict pop must
            # run — it is the *only* place that removes this task from
            # _live_agent_tasks, and BackgroundTaskManager.kill() relies on
            # that strong reference staying valid until cancellation has
            # finished propagating. If we let an exception in the cleanup
            # block skip the pop, the entry leaks forever.
            try:
                for task in list(self._approval_update_tasks):
                    task.cancel()
                for task in list(self._approval_update_tasks):
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                self._runtime.approval_runtime.unsubscribe(approval_subscription)
                self._runtime.approval_runtime.cancel_by_source("background_agent", self._task_id)
                reset_current_approval_source(token)
            finally:
                self._manager._live_agent_tasks.pop(self._task_id, None)

    async def _run_core(self, output: SubagentOutputWriter) -> None:
        assert self._runtime.subagent_store is not None
        self._manager._mark_task_running(self._task_id)
        output.stage("runner_started")

        type_def = self._runtime.labor_market.require_builtin_type(self._subagent_type)
        record = self._runtime.subagent_store.require_instance(self._agent_id)
        launch_spec = record.launch_spec
        if self._model_override is not None:
            launch_spec = replace(
                launch_spec,
                model_override=self._model_override,
                effective_model=self._model_override,
            )

        work_dir_override = await self._prepare_isolation_worktree(output)
        spec = SubagentRunSpec(
            agent_id=self._agent_id,
            type_def=type_def,
            launch_spec=launch_spec,
            prompt=self._prompt,
            resumed=self._resumed,
            work_dir_override=work_dir_override,
            resolved_review_target=self._resolved_review_target,
        )
        soul, prompt = await prepare_soul(
            spec,
            self._runtime,
            self._builder,
            self._runtime.subagent_store,
            on_stage=output.stage,
        )
        if self._runtime.hook_engine is not None:
            soul.set_hook_engine(self._runtime.hook_engine)

        async def _ui_loop_fn(wire: Wire) -> None:
            wire_ui = wire.ui_side(merge=True)
            while True:
                msg = await wire_ui.receive()
                output.write_wire_message(msg)

        output.stage("run_soul_start")
        final_response, failure = await run_with_summary_continuation(
            soul,
            prompt,
            _ui_loop_fn,
            self._runtime.subagent_store.wire_path(self._agent_id),
            min_length=_SUMMARY_MIN_LENGTH_BY_TYPE.get(
                self._subagent_type, _SUMMARY_MIN_LENGTH_DEFAULT
            ),
        )
        if failure is not None:
            self._finalize_safely(outcome="failed", reason=failure.message)
            output.error(_failure_recovery_message(reason=failure.message, agent_id=self._agent_id))
            self._note_retained_worktree(output)
            output.stage(f"failed: {failure.brief}")
            return
        output.stage("run_soul_finished")

        if final_response is None:
            self._finalize_safely(
                outcome="failed", reason="Agent completed but produced no output."
            )
            self._note_retained_worktree(output)
            output.stage("failed: empty output")
            return
        # Surface this child's total LLM spend so the orchestrating parent can budget a
        # fan-out instead of discovering it on the bill — parity with the foreground
        # runner. Background results are read later via TaskOutput, so the spend rides in
        # the written transcript rather than the immediate (launch-stub) tool return.
        output.usage(format_usage_lines("child", soul.cumulative_usage, soul.model_name))
        final_response = await self._append_worktree_report(final_response)
        output.summary(final_response)
        self._finalize_safely(outcome="completed")

    async def _prepare_isolation_worktree(self, output: SubagentOutputWriter) -> HostPath | None:
        """Honor isolation='worktree' for write-profile children.

        Read-profile children gain nothing from isolation (they cannot
        mutate), so the request is logged and skipped rather than failing
        the task. Raises WorktreeError for non-git roots — actionable, and
        better surfaced before any model spend.
        """
        if self._isolation != "worktree":
            return None
        from pythinker_code.soul.permission import subagent_type_allows_file_mutation
        from pythinker_code.subagents.worktree import create_agent_worktree

        if not subagent_type_allows_file_mutation(self._subagent_type):
            logger.info(
                "isolation='worktree' ignored for read-profile subagent type {t}",
                t=self._subagent_type,
            )
            return None
        worktree = Path(str(self._runtime.session.dir)) / "worktrees" / self._agent_id
        await create_agent_worktree(Path(str(self._runtime.work_dir)), worktree)
        self._worktree_path = worktree
        output.stage(f"worktree_created: {worktree}")
        return HostPath.unsafe_from_local_path(worktree)

    def _note_retained_worktree(self, output: SubagentOutputWriter) -> None:
        """Name the retained worktree on failure/timeout paths.

        Retention on failure is deliberate — resume reuses the worktree and a
        post-mortem may need its state — but it must never be silent.
        """
        if self._worktree_path is None:
            return
        output.stage(
            f"worktree_retained: {self._worktree_path} (resume reuses it; remove with "
            f"`git worktree remove {self._worktree_path}`)"
        )

    async def _append_worktree_report(self, final_response: str) -> str:
        """Tell the orchestrator where the isolated changes live.

        Changes are never auto-merged; the report carries the worktree path
        and a diff summary so merging stays a deliberate decision. Clean
        worktrees are removed.
        """
        if self._worktree_path is None:
            return final_response
        from pythinker_code.subagents.worktree import (
            cleanup_agent_worktree,
            worktree_change_summary,
        )

        summary = await worktree_change_summary(self._worktree_path)
        disposition = await cleanup_agent_worktree(
            Path(str(self._runtime.work_dir)),
            self._worktree_path,
            has_changes=bool(summary),
        )
        return (
            f"{final_response}\n\n## Isolation worktree\n"
            f"Path: {self._worktree_path} ({disposition})\n"
            f"{summary or 'No changes were made.'}"
        )

    def _on_approval_runtime_event(self, event: ApprovalRuntimeEvent) -> None:
        request = event.request
        if request.source.kind != "background_agent" or request.source.id != self._task_id:
            return
        task = asyncio.create_task(self._apply_approval_runtime_event(event))
        self._approval_update_tasks.add(task)
        task.add_done_callback(self._approval_update_tasks.discard)
        task.add_done_callback(self._log_approval_update_failure)

    async def _apply_approval_runtime_event(self, event: ApprovalRuntimeEvent) -> None:
        request = event.request
        if event.kind == "request_created":
            await asyncio.to_thread(
                self._manager._mark_task_awaiting_approval,
                self._task_id,
                request.description,
            )
        elif event.kind == "request_resolved":
            assert self._runtime.approval_runtime is not None
            pending_for_task = [
                pending
                for pending in self._runtime.approval_runtime.list_pending()
                if pending.source.kind == "background_agent" and pending.source.id == self._task_id
            ]
            if pending_for_task:
                return
            await asyncio.to_thread(
                self._manager._mark_task_running,
                self._task_id,
            )

    @staticmethod
    def _log_approval_update_failure(task: asyncio.Task[None]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            exc = task.exception()
            if exc is not None:
                logger.opt(exception=exc).error("Failed to apply background approval state update")
