from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from pythinker_core.chat_provider import APIStatusError, ChatProviderError
from pythinker_core.message import Message
from pythinker_core.tooling import ToolError, ToolOk, ToolReturnValue

from pythinker_code.approval_runtime import (
    ApprovalSource,
    reset_current_approval_source,
    set_current_approval_source,
)
from pythinker_code.soul import MaxStepsReached, RunCancelled, UILoopFn, get_wire_or_none, run_soul
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.soul.toolset import get_current_tool_call_or_none
from pythinker_code.subagents.builder import SubagentBuilder
from pythinker_code.subagents.core import SubagentRunSpec, prepare_soul
from pythinker_code.subagents.models import AgentInstanceRecord, AgentLaunchSpec
from pythinker_code.subagents.output import SubagentOutputWriter
from pythinker_code.subagents.store import SubagentStore
from pythinker_code.subagents.usage import format_usage_lines, usage_extras
from pythinker_code.utils.logging import logger
from pythinker_code.wire import Wire
from pythinker_code.wire.file import WireFile
from pythinker_code.wire.types import (
    ApprovalRequest,
    ApprovalResponse,
    HookRequest,
    QuestionRequest,
    SubagentEvent,
    ToolCallRequest,
)

if TYPE_CHECKING:
    from pythinker_code.background.models import TaskView
    from pythinker_code.soul.agent import Runtime

SUMMARY_CONTINUATION_ATTEMPTS = 1
_SUMMARY_MIN_LENGTH_DEFAULT = 200
_SUMMARY_MIN_LENGTH_BY_TYPE: dict[str, int] = {
    "verifier": 50,
    "explore": 100,
    "debugger": 150,
    "judge": 150,
    "code-reviewer": 150,
    "review": 150,
    "security-reviewer": 150,
    "coder": 200,
    "implementer": 200,
    "plan": 300,
}
SUMMARY_CONTINUATION_PROMPT = """
Your previous response was too brief. Please provide a more comprehensive summary.
Keep the same natural-language output language as the original task/user request; do not switch
to a model/provider default language. Include:

1. Specific technical details and implementations
2. Detailed findings and analysis
3. All important information that the parent agent should know
""".strip()


# ---------------------------------------------------------------------------
# Shared result types and execution helpers (used by both foreground and
# background runners).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SoulRunFailure:
    """Describes why a soul run did not produce a usable result."""

    message: str
    brief: str


def _fail_with_usage(soul: PythinkerSoul, message: str, brief: str) -> ToolError:
    """Build a ToolError that still carries the child's accumulated token spend.

    A failed child burned tokens before failing; reporting them (in the message and
    in ``extras`` for batch aggregation) keeps a partial-failure fan-out's total
    spend visible to the orchestrator instead of silently under-counting it.
    """
    usage = soul.cumulative_usage
    err = ToolError(
        message="\n".join([message, *format_usage_lines("child", usage, soul.model_name)]),
        brief=brief,
    )
    err.extras = usage_extras(usage, soul.model_name)
    return err


async def run_soul_checked(
    soul: PythinkerSoul,
    prompt: str,
    ui_loop_fn: UILoopFn,
    wire_path: Path,
    phase: str,
) -> SoulRunFailure | None:
    """Run a single soul turn and validate the result.

    Returns a ``SoulRunFailure`` if the run failed or produced an invalid
    result, or ``None`` on success.  Most exceptions (``MaxStepsReached``,
    ``ChatProviderError``, generic ``Exception``) are converted to failures.
    Only ``CancelledError`` and ``RunCancelled`` are re-raised.
    """
    try:
        await run_soul(
            soul,
            prompt,
            ui_loop_fn,
            asyncio.Event(),
            wire_file=WireFile(wire_path),
            runtime=soul.runtime,
        )
    except MaxStepsReached as exc:
        logger.warning(
            "Subagent max steps reached ({n_steps}) when {phase}",
            n_steps=exc.n_steps,
            phase=phase,
        )
        return SoulRunFailure(
            message=(
                f"Max steps {exc.n_steps} reached when {phase}. "
                "Please try splitting the task into smaller subtasks."
            ),
            brief="Max steps reached",
        )
    except RunCancelled:
        raise
    except asyncio.CancelledError:
        raise
    except APIStatusError as exc:
        logger.warning(
            "Subagent LLM API error (HTTP {status_code}) when {phase}: {error}",
            status_code=exc.status_code,
            phase=phase,
            error=exc,
        )
        return SoulRunFailure(
            message=f"LLM API error (HTTP {exc.status_code}) when {phase}: {exc}",
            brief=f"API error ({exc.status_code})",
        )
    except ChatProviderError as exc:
        logger.warning(
            "Subagent LLM provider error when {phase}: {error}",
            phase=phase,
            error=exc,
        )
        return SoulRunFailure(
            message=f"LLM provider error when {phase}: {exc}",
            brief="LLM provider error",
        )
    except Exception as exc:
        from pythinker_code.telemetry.errors import report_handled_error

        report_handled_error(exc, site="subagents.run.soul")
        logger.exception("Subagent soul run failed when {phase}", phase=phase)
        return SoulRunFailure(
            message=f"Unexpected error when {phase}: {exc}",
            brief="Agent run error",
        )

    context = soul.context
    if not context.history or context.history[-1].role != "assistant":
        return SoulRunFailure(
            message="The agent did not produce a valid assistant response.",
            brief="Invalid agent result",
        )
    return None


async def run_with_summary_continuation(
    soul: PythinkerSoul,
    prompt: str,
    ui_loop_fn: UILoopFn,
    wire_path: Path,
    *,
    min_length: int = _SUMMARY_MIN_LENGTH_DEFAULT,
) -> tuple[str | None, SoulRunFailure | None]:
    """Run soul, then optionally extend the summary if it is too short.

    Returns ``(final_response, failure)``.  On success ``failure`` is
    ``None`` and ``final_response`` contains the agent's output text.
    On failure ``final_response`` is ``None``.
    """
    failure = await run_soul_checked(soul, prompt, ui_loop_fn, wire_path, "running agent")
    if failure is not None:
        return None, failure

    final_response = soul.context.history[-1].extract_text(sep="\n")
    remaining = SUMMARY_CONTINUATION_ATTEMPTS
    while remaining > 0 and len(final_response) < min_length:
        remaining -= 1
        failure = await run_soul_checked(
            soul,
            SUMMARY_CONTINUATION_PROMPT,
            ui_loop_fn,
            wire_path,
            "continuing the agent summary",
        )
        if failure is not None:
            # The continuation is a nicety; a transient failure here must
            # never convert the agent's completed work into a hard failure.
            logger.warning(
                "Summary continuation failed ({brief}); returning the existing summary",
                brief=failure.brief,
            )
            break
        final_response = soul.context.history[-1].extract_text(sep="\n")

    return final_response, None


# ---------------------------------------------------------------------------
# Foreground runner
# ---------------------------------------------------------------------------


def busy_resume_message(record: AgentInstanceRecord, task_view: TaskView | None) -> str:
    """Actionable rejection text for resuming an instance that is still running.

    Keeps the "cannot be resumed concurrently" marker (callers match on it) and
    tells the parent how to actually retrieve the result instead of dead-ending.
    """
    base = (
        f"Agent instance {record.agent_id} is still {record.status} and cannot be "
        "resumed concurrently."
    )
    if record.status != "running_background":
        return base + " Wait for its current run to finish, then resume."
    if task_view is not None:
        task_id = task_view.spec.id
        status = task_view.runtime.status or "non-terminal"
        started_at = task_view.runtime.started_at
        age = f", started {int(time.time() - started_at)}s ago" if started_at else ""
        return base + (
            f" Its background task {task_id} is verifiably still {status}{age} — it has NOT"
            " completed, even if earlier output suggested otherwise. You will be notified"
            " automatically when it finishes. Use"
            f' TaskOutput(task_id="{task_id}") for a progress snapshot (block=true only if'
            " you intend to wait), and resume this instance only after completion."
        )
    return base + (
        " You will be notified automatically when it finishes; use TaskOutput for a"
        " progress snapshot and resume only after completion."
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ForegroundRunRequest:
    description: str
    prompt: str
    requested_type: str
    model: str | None
    resume: str | None
    fork_context: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedInstance:
    record: AgentInstanceRecord
    actual_type: str
    resumed: bool


class ForegroundSubagentRunner:
    def __init__(self, runtime: Runtime):
        self._runtime = runtime
        assert runtime.subagent_store is not None
        self._store: SubagentStore = runtime.subagent_store
        self._builder = SubagentBuilder(runtime)

    async def _load_fork_history(self) -> list[Message] | None:
        """Read and filter the parent transcript for a context-forked child.

        Reads the persisted parent context file rather than live soul state,
        so the fork inherits exactly what would survive a parent restore
        (including the restore-time pairing repair). An explicit fork must not
        silently degrade to a blank child: read failures abort the spawn.
        """
        from pythinker_code.soul.context import Context
        from pythinker_code.subagents.core import filter_history_for_fork

        try:
            parent_context = Context(file_backend=self._runtime.session.context_file)
            await parent_context.restore()
        except Exception as exc:
            logger.warning("Context fork: failed to read parent history", exc_info=True)
            raise RuntimeError(
                "Context fork requested, but the parent history could not be restored."
            ) from exc
        forked = filter_history_for_fork(parent_context.history)
        return forked or None

    async def run(self, req: ForegroundRunRequest) -> ToolReturnValue:
        prepared = await self._prepare_instance(req)
        agent_id = prepared.record.agent_id
        actual_type = prepared.actual_type
        resumed = prepared.resumed

        from pythinker_code.soul.agent import require_agent_type_definition

        type_def = require_agent_type_definition(self._runtime, actual_type)
        launch_spec = prepared.record.launch_spec
        if req.model is not None:
            launch_spec = replace(
                launch_spec,
                model_override=req.model,
                effective_model=req.model,
            )

        output_writer = SubagentOutputWriter(self._store.output_path(agent_id))
        output_writer.stage("runner_started")

        fork_history = None
        if req.fork_context and not resumed:
            fork_history = await self._load_fork_history()
        spec = SubagentRunSpec(
            agent_id=agent_id,
            type_def=type_def,
            launch_spec=launch_spec,
            prompt=req.prompt,
            resumed=resumed,
            fork_history=fork_history,
        )
        self._store.update_instance(
            agent_id,
            status="running_foreground",
            description=req.description.strip(),
        )
        try:
            soul, prompt = await prepare_soul(
                spec,
                self._runtime,
                self._builder,
                self._store,
                on_stage=output_writer.stage,
            )
        except asyncio.CancelledError:
            self._store.update_instance(agent_id, status="killed")
            raise
        except Exception:
            self._store.update_instance(agent_id, status="failed")
            raise

        approval_source: ApprovalSource | None = None
        approval_source_token = None
        try:
            # Propagate hook engine from parent runtime to subagent soul
            if self._runtime.hook_engine is not None:
                soul.set_hook_engine(self._runtime.hook_engine)
            tool_call = get_current_tool_call_or_none()
            ui_loop_fn = self._make_ui_loop_fn(
                parent_tool_call_id=tool_call.id if tool_call is not None else None,
                agent_id=agent_id,
                subagent_type=actual_type,
                output_writer=output_writer,
            )

            # Use a single stable ApprovalSource for the entire run (including summary
            # continuation).  This ensures cancel_by_source can reliably cancel all
            # pending approval requests belonging to this foreground subagent execution.
            approval_source = ApprovalSource(
                kind="foreground_turn",
                id=uuid.uuid4().hex,
                agent_id=agent_id,
                subagent_type=actual_type,
            )
            approval_source_token = set_current_approval_source(approval_source)

            # --- SubagentStart hook ---
            hook_engine = soul.hook_engine
            from pythinker_code.hooks import events as hook_events

            await hook_engine.trigger(
                "SubagentStart",
                matcher_value=actual_type,
                input_data=hook_events.subagent_start(
                    session_id=self._runtime.session.id,
                    cwd=str(Path.cwd()),
                    agent_name=actual_type,
                    prompt=req.prompt[:500],
                ),
            )

            output_writer.stage("run_soul_start")
            final_response, failure = await run_with_summary_continuation(
                soul,
                prompt,
                ui_loop_fn,
                self._store.wire_path(agent_id),
                min_length=_SUMMARY_MIN_LENGTH_BY_TYPE.get(
                    actual_type, _SUMMARY_MIN_LENGTH_DEFAULT
                ),
            )
            if failure is not None:
                self._store.update_instance(agent_id, status="failed")
                output_writer.stage(f"failed: {failure.brief}")
                return _fail_with_usage(soul, failure.message, failure.brief)
            output_writer.stage("run_soul_finished")

            # --- SubagentStop hook ---
            # fire_and_forget_trigger keeps a strong reference to the task on
            # the hook engine so it cannot be garbage-collected before it
            # finishes. Without that, asyncio's WeakSet bookkeeping would let
            # GC reap the still-pending task and trigger the broken
            # "Exception None" loop-handler path.
            hook_engine.fire_and_forget_trigger(
                "SubagentStop",
                matcher_value=actual_type,
                input_data=hook_events.subagent_stop(
                    session_id=self._runtime.session.id,
                    cwd=str(Path.cwd()),
                    agent_name=actual_type,
                    response=(final_response or "")[:500],
                ),
            )
        except asyncio.CancelledError:
            self._store.update_instance(agent_id, status="killed")
            output_writer.stage("cancelled")
            raise
        except RunCancelled as exc:
            self._store.update_instance(agent_id, status="killed")
            output_writer.stage("cancelled")
            raise RunCancelled("Subagent run was cancelled.") from exc
        except Exception as exc:
            from pythinker_code.telemetry.errors import report_handled_error

            report_handled_error(exc, site="subagents.run.background")
            self._store.update_instance(agent_id, status="failed")
            output_writer.stage("failed_exception")
            raise
        finally:
            if approval_source_token is not None:
                reset_current_approval_source(approval_source_token)
            if approval_source is not None and self._runtime.approval_runtime is not None:
                self._runtime.approval_runtime.cancel_by_source(
                    approval_source.kind, approval_source.id
                )

        if final_response is None:
            self._store.update_instance(agent_id, status="failed")
            output_writer.stage("failed: empty output")
            return _fail_with_usage(
                soul, "Agent completed but produced no output.", "Empty agent output"
            )
        self._store.update_instance(agent_id, status="idle")
        output_writer.summary(final_response)
        usage = soul.cumulative_usage
        model = soul.model_name
        lines = [
            f"agent_id: {agent_id}",
            "resumed: true" if resumed else "resumed: false",
        ]
        if resumed and req.requested_type and req.requested_type != actual_type:
            lines.append(f"requested_subagent_type: {req.requested_type}")
        lines.append(f"actual_subagent_type: {actual_type}")
        lines.append("status: completed")
        # Surface this child's total LLM spend so the orchestrating parent can
        # budget effort across a fan-out instead of discovering it on the bill.
        lines.extend(format_usage_lines("child", usage, model))
        lines.extend(["", "[summary]", final_response])
        result = ToolOk(output="\n".join(lines))
        result.extras = usage_extras(usage, model)
        return result

    async def _prepare_instance(self, req: ForegroundRunRequest) -> PreparedInstance:
        if req.resume:
            record = self._store.require_instance(req.resume)
            task_view = None
            if record.status == "running_background":
                manager = getattr(self._runtime, "background_tasks", None)
                if manager is not None:
                    task_view = manager.reconcile_stale_agent_record(record.agent_id)
                    record = self._store.require_instance(req.resume)
            if record.status in {"running_foreground", "running_background"}:
                raise RuntimeError(busy_resume_message(record, task_view))
            return PreparedInstance(
                record=record,
                actual_type=record.subagent_type,
                resumed=True,
            )

        actual_type = req.requested_type or "coder"
        from pythinker_code.soul.agent import require_agent_type_definition

        type_def = require_agent_type_definition(self._runtime, actual_type)
        agent_id = f"a{uuid.uuid4().hex[:8]}"
        record = self._store.create_instance(
            agent_id=agent_id,
            description=req.description.strip(),
            launch_spec=AgentLaunchSpec(
                agent_id=agent_id,
                subagent_type=actual_type,
                model_override=req.model,
                effective_model=req.model or type_def.default_model,
                thinking=self._runtime.llm.thinking if self._runtime.llm is not None else None,
                thinking_effort=(
                    self._runtime.llm.thinking_effort if self._runtime.llm is not None else None
                ),
                parent_agent_id=self._runtime.subagent_id,
            ),
        )
        from pythinker_code.telemetry import track

        track("subagent_created")
        return PreparedInstance(
            record=record,
            actual_type=actual_type,
            resumed=False,
        )

    @staticmethod
    def _make_ui_loop_fn(
        *,
        parent_tool_call_id: str | None,
        agent_id: str,
        subagent_type: str,
        output_writer: SubagentOutputWriter,
    ):
        super_wire = get_wire_or_none()

        async def _ui_loop_fn(wire: Wire) -> None:
            wire_ui = wire.ui_side(merge=True)
            while True:
                msg = await wire_ui.receive()
                # Always write to output file regardless of wire availability.
                output_writer.write_wire_message(msg)
                if super_wire is None or parent_tool_call_id is None:
                    continue
                if isinstance(
                    msg,
                    ApprovalRequest | ApprovalResponse | ToolCallRequest | QuestionRequest,
                ):
                    super_wire.soul_side.send(msg)
                    continue
                if isinstance(msg, HookRequest):
                    continue
                super_wire.soul_side.send(
                    SubagentEvent(
                        parent_tool_call_id=parent_tool_call_id,
                        agent_id=agent_id,
                        subagent_type=subagent_type,
                        event=msg,
                    )
                )

        return _ui_loop_fn
