"""
Pythinker Core contains the low-level building blocks used by Pythinker agents.

It provides message models, streaming chat provider interfaces, provider implementations,
tool abstractions, and the `generate` / `step` primitives used by Pythinker CLI and
Pythinker SDK.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from loguru import logger

from pythinker_core._generate import GenerateResult, generate
from pythinker_core.chat_provider import (
    ChatProvider,
    StreamedMessagePart,
    TokenUsage,
)
from pythinker_core.message import Message, ToolCall
from pythinker_core.tooling import ToolResult, ToolResultFuture, Toolset
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
]


async def step(
    chat_provider: ChatProvider,
    system_prompt: str,
    toolset: Toolset,
    history: Sequence[Message],
    *,
    on_message_part: Callback[[StreamedMessagePart], None] | None = None,
    on_tool_result: Callable[[ToolResult], None] | None = None,
) -> "StepResult":
    """
    Run one agent "step". In one step, the function generates LLM response based on the given
    context for exactly one time. All new message parts will be streamed to `on_message_part` in
    real-time if provided. Tool calls will be handled by `toolset`. The generated message will be
    returned in a `StepResult`. Depending on the toolset implementation, the tool calls may be
    handled asynchronously and the results need to be fetched with `await result.tool_results()`.

    The message history will NOT be modified in this function.

    The token usage will be returned in the `StepResult` if available.

    Raises:
        APIConnectionError: If the API connection fails.
        APITimeoutError: If the API request times out.
        APIStatusError: If the API returns a status code of 4xx or 5xx.
        APIEmptyResponseError: If the API returns an empty response.
        ChatProviderError: If any other recognized chat provider error occurs.
        asyncio.CancelledError: If the step is cancelled.
    """

    tool_calls: list[ToolCall] = []
    tool_result_futures: dict[str, ToolResultFuture] = {}
    tool_callbacks_active = True

    def future_done_callback(future: ToolResultFuture) -> None:
        if not tool_callbacks_active:
            return
        if on_tool_result:
            try:
                result = future.result()
                on_tool_result(result)
            except asyncio.CancelledError:
                return

    async def on_tool_call(tool_call: ToolCall) -> None:
        tool_calls.append(tool_call)
        result = toolset.handle(tool_call)

        if isinstance(result, ToolResult):
            future = ToolResultFuture()
            future.add_done_callback(future_done_callback)
            future.set_result(result)
            tool_result_futures[tool_call.id] = future
        else:
            result.add_done_callback(future_done_callback)
            tool_result_futures[tool_call.id] = result

    try:
        result = await generate(
            chat_provider,
            system_prompt,
            toolset.tools,
            history,
            on_message_part=on_message_part,
            on_tool_call=on_tool_call,
        )
    except BaseException:
        # A later terminal dispatch can fail after earlier work was accepted. Deactivate
        # publication before touching callbacks/tasks: already-queued callbacks cannot be
        # retracted by remove_done_callback().
        tool_callbacks_active = False
        futures = list(tool_result_futures.values())
        for future in futures:
            future.remove_done_callback(future_done_callback)
            future.cancel()
        await asyncio.gather(*futures, return_exceptions=True)
        raise

    return StepResult(
        result.id,
        result.message,
        result.usage,
        tool_calls,
        tool_result_futures,
        truncated=result.truncated,
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
    """@private The futures of the results of the spawned tool calls."""

    truncated: bool = False
    """True when the model's response was cut off by the output-token limit."""

    async def tool_results(self) -> list[ToolResult]:
        """All the tool results returned by corresponding tool calls."""
        if not self._tool_result_futures:
            return []

        try:
            results: list[ToolResult] = []
            for tool_call in self.tool_calls:
                future = self._tool_result_futures[tool_call.id]
                result = await future
                results.append(result)
            return results
        finally:
            # one exception should cancel all the futures to avoid hanging tasks
            for future in self._tool_result_futures.values():
                future.cancel()
            await asyncio.gather(*self._tool_result_futures.values(), return_exceptions=True)
