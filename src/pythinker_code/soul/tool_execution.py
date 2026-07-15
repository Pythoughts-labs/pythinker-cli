from __future__ import annotations

import asyncio
import contextlib
import copy
import difflib
import hashlib
import json
import math
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pythinker_core.tooling import (
    CallableTool,
    CallableTool2,
    HandleResult,
    ToolBatchContext,
    ToolBatchHandle,
    ToolBatchSummary,
    ToolCancellationTimeoutError,
    ToolError,
    ToolResultFuture,
)
from pythinker_core.tooling.error import ToolNotFoundError, ToolParseError, ToolRuntimeError
from pythinker_core.utils.typing import JsonType

from pythinker_code.hooks.engine import HookEngine
from pythinker_code.telemetry.names import sanitize_telemetry_tool_name
from pythinker_code.utils.logging import logger
from pythinker_code.wire.types import (
    ContentPart,
    TextPart,
    ToolCall,
    ToolExecutionStarted,
    ToolResult,
    ToolReturnValue,
    ToolUseSkipped,
)

if TYPE_CHECKING:
    from pythinker_code.soul.agent import Runtime


type ToolType = CallableTool | CallableTool2[Any]
type ToolCallKey = tuple[str, str]


current_tool_call = ContextVar[ToolCall | None]("current_tool_call", default=None)
_current_tool_execution_started_ids: ContextVar[set[str] | None] = ContextVar(
    "current_tool_execution_started_ids", default=None
)
_current_session_id: ContextVar[str] = ContextVar("_current_session_id", default="")


def set_session_id(sid: str) -> None:
    _current_session_id.set(sid)


def get_session_id() -> str:
    return _current_session_id.get()


def _get_session_id() -> str:
    return _current_session_id.get()


def get_current_tool_call_or_none() -> ToolCall | None:
    """
    Get the current tool call or None.
    Expect to be not None when called from a `__call__` method of a tool.
    """
    return current_tool_call.get()


def emit_current_tool_execution_started() -> None:
    """Emit ToolExecutionStarted once for the current tool call, if wire is active."""
    tool_call = get_current_tool_call_or_none()
    if tool_call is None:
        return

    started_ids = _current_tool_execution_started_ids.get()
    if started_ids is None:
        started_ids = set[str]()
        _current_tool_execution_started_ids.set(started_ids)
    if tool_call.id in started_ids:
        return
    started_ids.add(tool_call.id)

    try:
        from pythinker_code.soul import get_wire_or_none

        if wire := get_wire_or_none():
            wire.soul_side.send(ToolExecutionStarted(tool_call_id=tool_call.id))
    except Exception as exc:  # noqa: BLE001 - lifecycle events must not break tool execution
        logger.debug(
            "Failed to emit tool execution start: {tool_name} (call_id={call_id}): {error}",
            tool_name=tool_call.function.name,
            call_id=tool_call.id,
            error=exc,
        )


def _emit_tool_use_skipped(
    *,
    tool_call_id: str,
    tool_name: str,
    reason: Literal["dedup", "policy", "interrupt", "concurrent_inflight"],
    resumed: bool = False,
) -> None:
    try:
        from pythinker_code.soul import get_wire_or_none

        if wire := get_wire_or_none():
            wire.soul_side.send(
                ToolUseSkipped(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    reason=reason,
                    resumed=resumed,
                )
            )
    except Exception as exc:  # noqa: BLE001 - observability must not break tool execution
        logger.debug(
            "Failed to emit tool skipped event: {tool_name} (call_id={call_id}): {error}",
            tool_name=tool_name,
            call_id=tool_call_id,
            error=exc,
        )


def tool_defers_execution_started(tool: ToolType) -> bool:
    return bool(getattr(tool, "emits_tool_execution_started_after_approval", False))


_REMINDER_TEXT_1 = (
    "\n\n<system-reminder>\n"
    "You are repeating the exact same tool call with identical parameters."
    " Please carefully analyze the previous result. If the task is not yet complete,"
    " try a different method or parameters instead of repeating the same call."
    "\n</system-reminder>"
)

TOOL_USE_SKIPPED_REASONS = frozenset({"dedup", "policy", "interrupt", "concurrent_inflight"})


def _make_reminder_text_2(tool_name: str, repeat_count: int, canonical_args: str) -> str:
    # Echo only a bounded preview of the arguments: large-payload tools
    # (WriteFile, MultiEdit) would otherwise re-inject the whole body into
    # context on every repeat — defeating the reminder by inflating tokens.
    # Exact identity is preserved by the args_hash in the dedup telemetry.
    args_limit = 256
    if len(canonical_args) > args_limit:
        dropped = len(canonical_args) - args_limit
        args_preview = f"{canonical_args[:args_limit]}... [truncated {dropped} chars]"
    else:
        args_preview = canonical_args
    return (
        "\n\n<system-reminder>\n"
        "You have repeatedly called the same tool with identical parameters many times.\n"
        "Repeated tool call detected:\n"
        f"- tool: {tool_name}\n"
        f"- repeated_times: {repeat_count}\n"
        f"- arguments: {args_preview}\n"
        "The previous repeated calls did not make progress. Do not call this exact same tool "
        "with the exact same arguments again.\n"
        "Carefully inspect the latest tool result and choose a different next action, "
        "different parameters, or finish the task if enough evidence has been gathered."
        "\n</system-reminder>"
    )


def _sort_json_value(value: object) -> object:
    if isinstance(value, list):
        return [_sort_json_value(item) for item in cast("list[object]", value)]
    if isinstance(value, dict):
        value_dict = cast("dict[str, object]", value)
        return {key: _sort_json_value(value_dict[key]) for key in sorted(value_dict)}
    return value


def _canonical_tool_arguments(arguments: Any) -> str:
    try:
        return json.dumps(
            _sort_json_value(arguments),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return str(arguments)


def _canonical_tool_arguments_text(arguments: str) -> str:
    try:
        return _canonical_tool_arguments(json.loads(arguments, strict=False))
    except json.JSONDecodeError:
        return arguments


def _normalize_call_key(tool_name: str, arguments: str) -> ToolCallKey:
    return (tool_name, _canonical_tool_arguments_text(arguments))


def _append_reminder_to_return_value(return_value: Any, reminder_text: str) -> Any:
    """Append dedup reminder text to a ToolReturnValue output."""
    if not isinstance(return_value, ToolReturnValue):
        return return_value

    output = return_value.output

    if isinstance(output, str):
        new_output: str | list[ContentPart] = output + reminder_text
    else:
        new_output = list(output)
        if new_output and isinstance(new_output[-1], TextPart):
            new_output[-1] = TextPart(text=new_output[-1].text + reminder_text)
        else:
            new_output.append(TextPart(text=reminder_text))

    return return_value.model_copy(update={"output": new_output})


def _emit_tool_use_skipped_if_opted_in(
    tool: ToolType,
    *,
    tool_call_id: str,
    tool_name: str,
    reason: Literal["dedup", "policy", "interrupt", "concurrent_inflight"],
    resumed: bool = False,
) -> None:
    if not getattr(tool, "emits_tool_use_skipped", False):
        return
    _emit_tool_use_skipped(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        reason=reason,
        resumed=resumed,
    )


TOOL_CANCELLATION_TIMEOUT_SECONDS = 5.0

_DEFAULT_MAX_CONCURRENT_READERS = 10
"""Cap on concurrent parallel-safe tool calls. A turn that fans out many readers
(e.g. dozens of FetchURL) overlaps freely up to this bound rather than opening an
unbounded number of sockets/file handles at once."""


class ReadWriteGate:
    """Async reader-writer gate for same-step parallel tool calls.

    Parallel-safe tools (readers) overlap freely up to ``max_concurrent_readers``;
    a mutating tool (writer) waits for in-flight readers to drain and excludes
    everything while it runs. Writers hold the lock while draining, which also
    blocks new readers behind a queued writer — dispatch order stays deterministic
    and writers cannot starve. Unflagged/plugin-style tools default to the
    exclusive writer path unless they explicitly declare ``supports_parallel=True``.
    """

    def __init__(self, max_concurrent_readers: int = _DEFAULT_MAX_CONCURRENT_READERS) -> None:
        self._writer_lock = asyncio.Lock()
        self._active_readers = 0
        self._readers_drained = asyncio.Event()
        self._readers_drained.set()
        self._reader_slots = asyncio.Semaphore(max_concurrent_readers)

    @contextlib.asynccontextmanager
    async def shared(self) -> AsyncGenerator[None]:
        # Only tools that opted into ``supports_parallel=True`` should enter this
        # shared path; unflagged/plugin adapters stay exclusive by default.
        # Cap concurrent readers. Acquire the slot BEFORE the writer lock / counter
        # bump: a reader still queued here has not incremented _active_readers, so it
        # never holds _readers_drained open, and writers (which never touch the
        # semaphore) cannot be starved — keeping the cap deadlock-safe.
        await self._reader_slots.acquire()
        try:
            async with self._writer_lock:
                self._active_readers += 1
                self._readers_drained.clear()
            try:
                yield
            finally:
                self._active_readers -= 1
                if self._active_readers == 0:
                    self._readers_drained.set()
        finally:
            self._reader_slots.release()

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncGenerator[None]:
        async with self._writer_lock:
            await self._readers_drained.wait()
            yield


@dataclass(frozen=True, slots=True)
class _PreparedToolCall:
    tool_call: ToolCall
    tool: ToolType | None = None
    arguments: JsonType | None = None
    canonical_args: str = ""
    immediate_result: ToolResult | None = None


class ToolExecutionEngine:
    """Private execution state machine behind :class:`PythinkerToolset`.

    Registry, visibility, and MCP ownership stay in the facade. This engine owns call
    preparation, dedup state, concurrency scheduling, lifecycle hooks, and batch supervision.
    """

    def __init__(
        self,
        runtime: Runtime | None,
        resolve_tool: Callable[[str], ToolType | None],
        available_tool_names: Callable[[], list[str]],
        get_hook_engine: Callable[[], HookEngine],
        execute_tool: Callable[[ToolType, JsonType], Awaitable[ToolReturnValue]],
    ) -> None:
        self._runtime = runtime
        self._resolve_tool = resolve_tool
        self._available_tool_names = available_tool_names
        self._get_hook_engine = get_hook_engine
        self._execute_tool = execute_tool
        self._concurrency_gate = ReadWriteGate()
        self._previous_step_calls: list[ToolCallKey] = []
        self._current_step_calls: list[ToolCallKey] = []
        self._current_step_tasks: dict[ToolCallKey, asyncio.Task[ToolResult]] = {}
        self._seen_call_keys: set[ToolCallKey] = set()
        self._consecutive_key: ToolCallKey | None = None
        self._consecutive_count = 0
        self._step_closed = True
        self._step_started = False
        self._dedup_triggered = False
        self._step_no = 0
        self._turn_id = ""
        self._poisoned_batches: set[_ExecutionBatch] = set()
        self._late_drain_tasks: set[asyncio.Task[None]] = set()

    @property
    def poisoned(self) -> bool:
        return bool(self._poisoned_batches)

    def _ensure_healthy(self) -> None:
        if self.poisoned:
            raise ToolCancellationTimeoutError(
                "Tool execution is unavailable while timed-out cancellation finishes"
            )

    def register_cancellation_timeout(self, batch: _ExecutionBatch) -> None:
        if batch in self._poisoned_batches:
            return
        self._poisoned_batches.add(batch)

        async def drain_late_batch() -> None:
            try:
                await batch.wait_until_drained()
            finally:
                self._poisoned_batches.discard(batch)
                logger.info("Timed-out tool cancellation drained; tool execution recovered")

        drain_task = asyncio.create_task(drain_late_batch())
        self._late_drain_tasks.add(drain_task)
        drain_task.add_done_callback(self._late_drain_tasks.discard)

    def begin_step(
        self,
        previous_calls: Sequence[ToolCallKey],
        *,
        step_no: int = 0,
        turn_id: str = "",
    ) -> None:
        self._previous_step_calls = [
            _normalize_call_key(tool_name, arguments) for tool_name, arguments in previous_calls
        ]
        self._current_step_calls = []
        self._current_step_tasks = {}
        self._step_closed = False
        self._step_started = True
        self._dedup_triggered = False
        self._step_no = step_no
        self._turn_id = turn_id
        if not self._previous_step_calls:
            self._seen_call_keys = set()
            self._consecutive_key = None
            self._consecutive_count = 0
        else:
            self._seen_call_keys.update(self._previous_step_calls)
            if self._consecutive_key is None and self._consecutive_count == 0:
                self._advance_consecutive_streak(self._previous_step_calls)

    def end_step(self) -> list[ToolCallKey]:
        if not self._step_closed:
            self._advance_consecutive_streak(self._current_step_calls)
            self._seen_call_keys.update(self._current_step_calls)
            self._step_closed = True
        return list(self._current_step_calls)

    def _advance_consecutive_streak(self, calls: Sequence[ToolCallKey]) -> None:
        for call_key in calls:
            if call_key == self._consecutive_key:
                self._consecutive_count += 1
            else:
                self._consecutive_key = call_key
                self._consecutive_count = 1

    def _projected_streak_for_call(self, call_index: int) -> int:
        consecutive_key = self._consecutive_key
        consecutive_count = self._consecutive_count
        for call_key in self._current_step_calls[: call_index + 1]:
            if call_key == consecutive_key:
                consecutive_count += 1
            else:
                consecutive_key = call_key
                consecutive_count = 1
        return consecutive_count

    @property
    def dedup_triggered(self) -> bool:
        return self._dedup_triggered

    @property
    def consecutive_repeat_count(self) -> int:
        return self._consecutive_count

    @property
    def summary(self) -> ToolBatchSummary:
        return ToolBatchSummary(
            current_call_fingerprints=tuple(self._current_step_calls),
            dedup_triggered=self._dedup_triggered,
            consecutive_identical_call_count=self._consecutive_count,
            finalized=self._step_closed,
        )

    async def gated_call(self, tool: ToolType, arguments: JsonType) -> ToolReturnValue:
        if getattr(tool, "supports_parallel", False):
            async with self._concurrency_gate.shared():
                return await tool.call(arguments)
        async with self._concurrency_gate.exclusive():
            return await tool.call(arguments)

    def prepare(self, tool_call: ToolCall) -> _PreparedToolCall:
        tool_name = tool_call.function.name
        tool = self._resolve_tool(tool_name)
        if tool is None:
            matches = difflib.get_close_matches(
                tool_name,
                self._available_tool_names(),
                n=1,
                cutoff=0.6,
            )
            return _PreparedToolCall(
                tool_call=tool_call,
                immediate_result=ToolResult(
                    tool_call_id=tool_call.id,
                    return_value=ToolNotFoundError(
                        tool_name,
                        suggestion=matches[0] if matches else None,
                    ),
                ),
            )

        if tool_name == "ToolSearch" and self._runtime is not None:
            from pythinker_code.llm import supports_deferred_tool_search

            if not supports_deferred_tool_search(self._runtime.llm):
                return _PreparedToolCall(
                    tool_call=tool_call,
                    immediate_result=ToolResult(
                        tool_call_id=tool_call.id,
                        return_value=ToolNotFoundError(tool_name),
                    ),
                )

        try:
            arguments: JsonType = json.loads(tool_call.function.arguments or "{}", strict=False)
        except json.JSONDecodeError as error:
            logger.warning(
                "Tool call JSON parse error: {tool_name} (call_id={call_id}): {error}",
                tool_name=tool_name,
                call_id=tool_call.id,
                error=error,
            )
            return _PreparedToolCall(
                tool_call=tool_call,
                immediate_result=ToolResult(
                    tool_call_id=tool_call.id,
                    return_value=ToolParseError(str(error)),
                ),
            )

        return _PreparedToolCall(
            tool_call=tool_call,
            tool=tool,
            arguments=arguments,
            canonical_args=_canonical_tool_arguments(arguments),
        )

    def prepare_batch(self, tool_calls: Sequence[ToolCall]) -> tuple[_PreparedToolCall, ...]:
        return tuple(self.prepare(tool_call) for tool_call in tool_calls)

    def handle(self, tool_call: ToolCall) -> HandleResult:
        self._ensure_healthy()
        if not self._step_started or self._step_closed:
            self.begin_step(())
        return self.dispatch(self.prepare(tool_call))

    def handle_batch(
        self,
        tool_calls: Sequence[ToolCall],
        context: ToolBatchContext,
        *,
        on_tool_result: Callable[[ToolResult], None] | None = None,
    ) -> ToolBatchHandle:
        self._ensure_healthy()
        prepared = self.prepare_batch(tool_calls)
        if not self._step_started or self._step_closed:
            self.begin_step(
                context.prior_call_fingerprints,
                step_no=context.step_no,
                turn_id=context.turn_id,
            )
        return _ExecutionBatch(self, prepared, on_tool_result)

    def dispatch(self, prepared: _PreparedToolCall) -> HandleResult:
        tool_call = prepared.tool_call
        if prepared.immediate_result is not None:
            return prepared.immediate_result
        if prepared.tool is None or prepared.arguments is None:
            raise RuntimeError("prepared tool call is missing execution data")

        token = current_tool_call.set(tool_call)
        try:
            tool = prepared.tool
            arguments = prepared.arguments
            canonical_args = prepared.canonical_args
            call_key = (tool_call.function.name, canonical_args)
            call_index = len(self._current_step_calls)
            self._current_step_calls.append(call_key)

            if call_key in self._current_step_tasks:
                from pythinker_code.telemetry import track

                _emit_tool_use_skipped_if_opted_in(
                    tool,
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.function.name,
                    reason="dedup",
                    resumed=True,
                )
                track(
                    "tool_call_dedup_detected",
                    turn_id=self._turn_id,
                    step_no=self._step_no,
                    tool_name=tool_call.function.name,
                    dup_type="same_step",
                    args_hash=hashlib.sha256(canonical_args.encode("utf-8")).hexdigest()[:8],
                )
                original_task = self._current_step_tasks[call_key]

                async def await_duplicate() -> ToolResult:
                    original_result = await original_task
                    return ToolResult(
                        tool_call_id=tool_call.id,
                        return_value=original_result.return_value,
                    )

                return asyncio.create_task(await_duplicate())

            is_cross_step_dup = call_key in self._seen_call_keys
            reminder_text: str | None = None
            if is_cross_step_dup:
                from pythinker_code.telemetry import track

                track(
                    "tool_call_dedup_detected",
                    turn_id=self._turn_id,
                    step_no=self._step_no,
                    tool_name=tool_call.function.name,
                    dup_type="cross_step",
                    args_hash=hashlib.sha256(canonical_args.encode("utf-8")).hexdigest()[:8],
                )
                self._dedup_triggered = True
                repeat_count = self._projected_streak_for_call(call_index)
                if repeat_count == 3:
                    reminder_text = _REMINDER_TEXT_1
                elif repeat_count in (5, 8):
                    reminder_text = _make_reminder_text_2(
                        tool_call.function.name,
                        repeat_count,
                        canonical_args,
                    )
                if reminder_text is not None:
                    _emit_tool_use_skipped_if_opted_in(
                        tool,
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.function.name,
                        reason="dedup",
                        resumed=False,
                    )

            async def call_tool() -> ToolResult:
                started_ids_token = _current_tool_execution_started_ids.set(set[str]())
                try:
                    return await call_with_lifecycle()
                finally:
                    _current_tool_execution_started_ids.reset(started_ids_token)

            async def call_with_lifecycle() -> ToolResult:
                tool_input_dict = copy.deepcopy(arguments) if isinstance(arguments, dict) else {}

                if self._runtime is not None:
                    from pythinker_code.soul.permission import check_tool_call_allowed

                    if error := check_tool_call_allowed(
                        self._runtime,
                        tool_call.function.name,
                        tool_input_dict,
                        tool=tool,
                    ):
                        _emit_tool_use_skipped_if_opted_in(
                            tool,
                            tool_call_id=tool_call.id,
                            tool_name=tool_call.function.name,
                            reason="policy",
                        )
                        return ToolResult(tool_call_id=tool_call.id, return_value=error)

                from pythinker_code.hooks import events

                hook_engine = self._get_hook_engine()
                hook_results = await hook_engine.trigger(
                    "PreToolUse",
                    matcher_value=tool_call.function.name,
                    input_data=events.pre_tool_use(
                        session_id=_get_session_id(),
                        cwd=str(Path.cwd()),
                        tool_name=tool_call.function.name,
                        tool_input=copy.deepcopy(tool_input_dict),
                        tool_call_id=tool_call.id,
                    ),
                )
                for hook_result in hook_results:
                    if hook_result.action == "block":
                        _emit_tool_use_skipped_if_opted_in(
                            tool,
                            tool_call_id=tool_call.id,
                            tool_name=tool_call.function.name,
                            reason="policy",
                        )
                        return ToolResult(
                            tool_call_id=tool_call.id,
                            return_value=ToolError(
                                message=hook_result.reason or "Blocked by PreToolUse hook",
                                brief="Hook blocked",
                            ),
                        )

                from pythinker_code.telemetry import metrics, otel

                if not tool_defers_execution_started(tool):
                    emit_current_tool_execution_started()

                started_at = time.monotonic()
                telemetry_tool_name = sanitize_telemetry_tool_name(tool_call.function.name)
                span_context = otel.start_span(
                    "pythinker.tool",
                    {
                        "tool.name": telemetry_tool_name,
                        "tool.call_id": tool_call.id,
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.tool.name": telemetry_tool_name,
                    },
                )
                span = span_context.__enter__()
                try:
                    return_value = await self._execute_tool(tool, copy.deepcopy(arguments))
                except Exception as error:
                    elapsed = time.monotonic() - started_at
                    span.set_attribute("tool.success", False)
                    span.set_attribute("tool.error_type", type(error).__name__)
                    span.set_attribute("tool.duration_ms", int(elapsed * 1000))
                    span_context.__exit__(type(error), error, error.__traceback__)
                    metrics.record_tool_call(
                        tool_name=telemetry_tool_name,
                        duration_seconds=elapsed,
                        success=False,
                        error_type=type(error).__name__,
                    )
                    metrics.record_error(kind="tool_error", error_type=type(error).__name__)
                    logger.exception(
                        "Tool execution failed: {tool_name} (call_id={call_id})",
                        tool_name=tool_call.function.name,
                        call_id=tool_call.id,
                    )
                    hook_engine.fire_and_forget_trigger(
                        "PostToolUseFailure",
                        matcher_value=tool_call.function.name,
                        input_data=events.post_tool_use_failure(
                            session_id=_get_session_id(),
                            cwd=str(Path.cwd()),
                            tool_name=tool_call.function.name,
                            tool_input=copy.deepcopy(tool_input_dict),
                            error=str(error),
                            tool_call_id=tool_call.id,
                        ),
                    )
                    from pythinker_code.telemetry import track

                    error_type = type(error).__name__
                    track("tool_error", tool_name=telemetry_tool_name, error_type=error_type)
                    track(
                        "tool_call",
                        tool_name=telemetry_tool_name,
                        success=False,
                        duration_ms=int(elapsed * 1000),
                        error_type=error_type,
                        dup_type="cross_step" if is_cross_step_dup else "normal",
                    )
                    return ToolResult(
                        tool_call_id=tool_call.id,
                        return_value=ToolRuntimeError(str(error)),
                    )
                except BaseException as error:
                    span_context.__exit__(type(error), error, error.__traceback__)
                    raise

                elapsed = time.monotonic() - started_at
                succeeded = not isinstance(return_value, ToolError)
                span.set_attribute("tool.success", succeeded)
                if isinstance(return_value, ToolError):
                    span.set_attribute("tool.error_brief", return_value.brief or "")
                span.set_attribute("tool.duration_ms", int(elapsed * 1000))
                span_context.__exit__(None, None, None)
                metrics.record_tool_call(
                    tool_name=telemetry_tool_name,
                    duration_seconds=elapsed,
                    success=succeeded,
                )
                logger.info(
                    "Tool {tool_name} completed in {elapsed:.1f}s (call_id={call_id})",
                    tool_name=tool_call.function.name,
                    elapsed=elapsed,
                    call_id=tool_call.id,
                )
                from pythinker_code.telemetry import track

                track(
                    "tool_call",
                    tool_name=telemetry_tool_name,
                    success=succeeded,
                    duration_ms=int(elapsed * 1000),
                    dup_type="cross_step" if is_cross_step_dup else "normal",
                )
                hook_engine.fire_and_forget_trigger(
                    "PostToolUse",
                    matcher_value=tool_call.function.name,
                    input_data=events.post_tool_use(
                        session_id=_get_session_id(),
                        cwd=str(Path.cwd()),
                        tool_name=tool_call.function.name,
                        tool_input=copy.deepcopy(tool_input_dict),
                        tool_output=str(return_value)[:2000],
                        tool_call_id=tool_call.id,
                    ),
                )
                if reminder_text is not None:
                    return ToolResult(
                        tool_call_id=tool_call.id,
                        return_value=_append_reminder_to_return_value(
                            return_value,
                            reminder_text,
                        ),
                    )
                return ToolResult(tool_call_id=tool_call.id, return_value=return_value)

            task = asyncio.create_task(call_tool())
            self._current_step_tasks[call_key] = task
            return task
        finally:
            current_tool_call.reset(token)


class _ExecutionBatch:
    def __init__(
        self,
        engine: ToolExecutionEngine,
        prepared_calls: Sequence[_PreparedToolCall],
        on_tool_result: Callable[[ToolResult], None] | None,
    ) -> None:
        self._engine = engine
        self._prepared_calls = tuple(prepared_calls)
        self._on_tool_result = on_tool_result
        self._completed_results: dict[str, ToolResult] = {}
        self._source_futures: list[ToolResultFuture] = []
        self._watcher_tasks: list[asyncio.Task[ToolResult]] = []
        self._summary = ToolBatchSummary(finalized=False)
        self._callbacks_active = True
        self._settlement_task: asyncio.Task[None] | None = None
        self._supervisor = asyncio.create_task(self._run())
        self._supervisor.add_done_callback(self._consume_supervisor_failure)

    @property
    def tool_calls(self) -> Sequence[ToolCall]:
        return tuple(prepared.tool_call for prepared in self._prepared_calls)

    @property
    def completed_results(self) -> Mapping[str, ToolResult]:
        return dict(self._completed_results)

    @property
    def summary(self) -> ToolBatchSummary:
        return self._summary

    @staticmethod
    def _consume_supervisor_failure(supervisor: asyncio.Task[list[ToolResult]]) -> None:
        try:
            supervisor.exception()
        except asyncio.CancelledError:
            return

    async def _watch(self, future: ToolResultFuture) -> ToolResult:
        result = await future
        self._completed_results[result.tool_call_id] = result
        if self._callbacks_active and self._on_tool_result is not None:
            self._on_tool_result(result)
        return result

    async def _run(self) -> list[ToolResult]:
        try:
            for prepared in self._prepared_calls:
                handled = self._engine.dispatch(prepared)
                if isinstance(handled, ToolResult):
                    future = ToolResultFuture()
                    future.set_result(handled)
                else:
                    future = handled
                self._source_futures.append(future)

            self._engine.end_step()
            self._summary = self._engine.summary
            self._watcher_tasks = [
                asyncio.create_task(self._watch(future)) for future in self._source_futures
            ]
            return list(await asyncio.gather(*self._watcher_tasks))
        except BaseException:
            self._callbacks_active = False
            for task in self._watcher_tasks:
                task.cancel()
            for future in self._source_futures:
                future.cancel()
            await asyncio.gather(*self._watcher_tasks, return_exceptions=True)
            await asyncio.gather(*self._source_futures, return_exceptions=True)
            if not self._summary.finalized:
                self._engine.end_step()
                self._summary = self._engine.summary
            raise

    async def results(self) -> list[ToolResult]:
        return await asyncio.shield(self._supervisor)

    async def _wait_for_supervisor(self) -> None:
        try:
            await asyncio.shield(self._supervisor)
        except BaseException:
            if not self._supervisor.done():
                raise
            await asyncio.gather(self._supervisor, return_exceptions=True)

    async def wait_until_drained(self) -> None:
        await asyncio.gather(self._supervisor, return_exceptions=True)

    async def _bounded_settlement(self, timeout: float) -> None:
        self._callbacks_active = False
        if not self._supervisor.done() and self._supervisor.cancelling() == 0:
            self._supervisor.cancel()
        try:
            await asyncio.wait_for(self._wait_for_supervisor(), timeout=timeout)
        except TimeoutError as error:
            self._engine.register_cancellation_timeout(self)
            logger.error(
                "Tool cancellation timed out after {timeout:g}s; pausing new tool batches until "
                "late work drains",
                timeout=timeout,
            )
            raise ToolCancellationTimeoutError(
                f"Tool execution cancellation did not settle within {timeout:g} seconds"
            ) from error

    async def cancel_and_settle(self, *, timeout: float | None = None) -> None:
        effective_timeout = TOOL_CANCELLATION_TIMEOUT_SECONDS if timeout is None else timeout
        if not math.isfinite(effective_timeout) or effective_timeout < 0:
            raise ValueError("tool cancellation timeout must be finite and non-negative")

        settlement = self._settlement_task
        if settlement is None:
            settlement = asyncio.create_task(self._bounded_settlement(effective_timeout))
            settlement.add_done_callback(self._consume_settlement_failure)
            self._settlement_task = settlement
        await asyncio.shield(settlement)

    @staticmethod
    def _consume_settlement_failure(settlement: asyncio.Task[None]) -> None:
        try:
            settlement.exception()
        except asyncio.CancelledError:
            return
