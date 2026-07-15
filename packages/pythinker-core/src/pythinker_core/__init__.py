"""
Pythinker Core contains the low-level building blocks used by Pythinker agents.

It provides message models, streaming chat provider interfaces, provider implementations,
tool abstractions, and the `generate` / `step` primitives used by Pythinker CLI and
Pythinker SDK.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import cast

from loguru import logger

from pythinker_core._generate import GenerateResult, generate
from pythinker_core.chat_provider import (
    ChatProvider,
    StreamedMessagePart,
    TokenUsage,
)
from pythinker_core.message import Message, ToolCall
from pythinker_core.tooling import (
    BatchToolset,
    ToolBatchContext,
    ToolBatchHandle,
    ToolBatchSummary,
    ToolCancellationTimeoutError,
    ToolResult,
    ToolResultFuture,
    Toolset,
)
from pythinker_core.utils.aio import Callback

# Explicitly import submodules
from . import chat_provider, contrib, message, tooling, utils

logger.disable("pythinker_core")

__all__ = [
    # submodules
    "chat_provider",
    "tooling",
    "message",
    "utils",
    "contrib",
    # classes and functions
    "generate",
    "GenerateResult",
    "step",
    "StepResult",
    "BatchToolset",
    "ToolBatchContext",
    "ToolBatchHandle",
    "ToolBatchSummary",
    "ToolCancellationTimeoutError",
]


class _ToolResultCallbackSupervisor:
    """Own async result-publication callbacks without making callback errors fatal."""

    def __init__(self, callback: Callable[[ToolResult], object]) -> None:
        self._callback = callback
        self._loop = asyncio.get_running_loop()
        self._futures: set[asyncio.Future[None]] = set()

    def _report_failure(self, error: Exception) -> None:
        self._loop.call_exception_handler(
            {
                "message": "Tool result callback failed",
                "exception_type": type(error).__name__,
            }
        )

    def _async_callback_done(self, future: asyncio.Future[None]) -> None:
        self._futures.discard(future)
        try:
            future.result()
        except asyncio.CancelledError:
            return
        except Exception as error:
            self._report_failure(error)

    def __call__(self, result: ToolResult) -> None:
        try:
            outcome = self._callback(result)
        except asyncio.CancelledError:
            return
        except Exception as error:
            self._report_failure(error)
            return

        if inspect.isawaitable(outcome):
            future = asyncio.ensure_future(cast(Awaitable[None], outcome))
            self._futures.add(future)
            future.add_done_callback(self._async_callback_done)

    async def settle(self, *, cancel: bool) -> None:
        futures = list(self._futures)
        if cancel:
            for future in futures:
                future.cancel()
        await asyncio.gather(*futures, return_exceptions=True)


async def _dispatch_individual_tool_calls(
    tool_calls: Sequence[ToolCall],
    toolset: Toolset,
    on_tool_result: Callable[[ToolResult], None] | None,
) -> dict[str, ToolResultFuture]:
    tool_result_futures: dict[str, ToolResultFuture] = {}
    callbacks_active = True

    def future_done_callback(future: ToolResultFuture) -> None:
        if not callbacks_active:
            return
        if on_tool_result is not None:
            try:
                on_tool_result(future.result())
            except asyncio.CancelledError:
                return

    try:
        for tool_call in tool_calls:
            result = toolset.handle(tool_call)
            if isinstance(result, ToolResult):
                future = ToolResultFuture()
                future.add_done_callback(future_done_callback)
                future.set_result(result)
                tool_result_futures[tool_call.id] = future
            else:
                result.add_done_callback(future_done_callback)
                tool_result_futures[tool_call.id] = result
    except BaseException:
        # A later terminal dispatch can fail after earlier work was accepted. Deactivate
        # publication before touching callbacks/tasks: already-queued callbacks cannot be
        # retracted by remove_done_callback().
        callbacks_active = False
        futures = list(tool_result_futures.values())
        for future in futures:
            future.remove_done_callback(future_done_callback)
            future.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        raise

    return tool_result_futures


async def _await_owned_settlement(task: asyncio.Task[None]) -> None:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error

    # Retrieve settlement failures before restoring the caller's cancellation.
    task.result()
    if cancellation is not None:
        raise cancellation


async def step(
    chat_provider: ChatProvider,
    system_prompt: str,
    toolset: Toolset,
    history: Sequence[Message],
    *,
    on_message_part: Callback[[StreamedMessagePart], None] | None = None,
    on_tool_result: Callable[[ToolResult], object] | None = None,
    tool_batch_context: ToolBatchContext | None = None,
) -> "StepResult":
    """Generate one complete response, then dispatch its terminal tool-call batch.

    The message history is not modified. Batch-capable toolsets receive all calls at once after
    successful terminal assembly; legacy toolsets keep per-call ``handle()`` dispatch.
    """
    callback_supervisor = (
        _ToolResultCallbackSupervisor(on_tool_result) if on_tool_result is not None else None
    )
    result = await generate(
        chat_provider,
        system_prompt,
        toolset.tools,
        history,
        on_message_part=on_message_part,
    )
    tool_calls = list(result.message.tool_calls or ())

    if isinstance(toolset, BatchToolset):
        tool_batch = toolset.handle_batch(
            tool_calls,
            tool_batch_context or ToolBatchContext(),
            on_tool_result=callback_supervisor,
        )
        tool_result_futures: dict[str, ToolResultFuture] = {}
    else:
        tool_batch = None
        tool_result_futures = await _dispatch_individual_tool_calls(
            tool_calls,
            toolset,
            callback_supervisor,
        )

    return StepResult(
        result.id,
        result.message,
        result.usage,
        tool_calls,
        tool_result_futures,
        truncated=result.truncated,
        _tool_batch=tool_batch,
        _tool_result_callback_supervisor=callback_supervisor,
    )


@dataclass(frozen=True, slots=True)
class StepResult:
    id: str | None
    """The ID of the generated message."""

    message: Message
    """The message generated in this step."""

    usage: TokenUsage | None
    """The token usage in this step."""

    tool_calls: list[ToolCall]
    """All the tool calls generated in this step."""

    _tool_result_futures: dict[str, ToolResultFuture]
    """@private Legacy futures for per-call toolset dispatch."""

    truncated: bool = False
    """True when the model's response was cut off by the output-token limit."""

    _tool_batch: ToolBatchHandle | None = field(default=None, repr=False, compare=False)
    """@private Supervising handle for batch-capable toolset dispatch."""

    _tool_result_callback_supervisor: _ToolResultCallbackSupervisor | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    _cancel_settlement_task: asyncio.Task[None] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    async def tool_results(self) -> list[ToolResult]:
        """Return results in model call order, settling all owned work."""
        try:
            results: list[ToolResult]
            if self._tool_batch is not None:
                results = await self._tool_batch.results()
            else:
                results = []
                for tool_call in self.tool_calls:
                    future = self._tool_result_futures[tool_call.id]
                    results.append(await future)
                await self._cancel_legacy_futures()

            if self._tool_result_callback_supervisor is not None:
                await self._tool_result_callback_supervisor.settle(cancel=False)
            return results
        except BaseException as primary_error:
            try:
                await self.cancel_tool_execution()
            except ToolCancellationTimeoutError:
                raise
            except asyncio.CancelledError:
                # Preserve the original control-flow/error after supervised settlement.
                pass
            raise primary_error

    async def cancel_tool_execution(self) -> None:
        """Idempotently cancel and settle all owned tool execution."""
        settlement = self._cancel_settlement_task
        if settlement is None:
            settlement = asyncio.create_task(self._cancel_and_settle_owned_work())
            object.__setattr__(self, "_cancel_settlement_task", settlement)
        await _await_owned_settlement(settlement)

    async def _cancel_and_settle_owned_work(self) -> None:
        try:
            if self._tool_batch is not None:
                await self._tool_batch.cancel_and_settle()
            else:
                await self._cancel_legacy_futures()
        finally:
            if self._tool_result_callback_supervisor is not None:
                await self._tool_result_callback_supervisor.settle(cancel=True)

    async def _cancel_legacy_futures(self) -> None:
        futures = list(self._tool_result_futures.values())
        for future in futures:
            future.cancel()
        await asyncio.gather(*futures, return_exceptions=True)

    @property
    def completed_tool_results(self) -> dict[str, ToolResult]:
        """Snapshot of successful results that have already completed."""
        if self._tool_batch is not None:
            return dict(self._tool_batch.completed_results)

        completed: dict[str, ToolResult] = {}
        for tool_call_id, future in self._tool_result_futures.items():
            if not future.done() or future.cancelled() or future.exception() is not None:
                continue
            completed[tool_call_id] = future.result()
        return completed

    @property
    def tool_execution_summary(self) -> ToolBatchSummary:
        """Final batch metadata, or the empty summary for legacy dispatch."""
        if self._tool_batch is None:
            return ToolBatchSummary()
        return self._tool_batch.summary
