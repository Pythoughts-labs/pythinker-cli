from collections.abc import Sequence
from dataclasses import dataclass

from loguru import logger

from pythinker_core.chat_provider import (
    APIEmptyResponseError,
    APIStreamProtocolError,
    ChatProvider,
    StreamedMessagePart,
    TokenUsage,
)
from pythinker_core.message import Message, TextPart, ThinkPart, ToolCall
from pythinker_core.stream_message_assembler import StreamMessageAssembler
from pythinker_core.tooling import Tool
from pythinker_core.utils.aio import Callback, callback


async def generate(
    chat_provider: ChatProvider,
    system_prompt: str,
    tools: Sequence[Tool],
    history: Sequence[Message],
    *,
    on_message_part: Callback[[StreamedMessagePart], None] | None = None,
    on_tool_call: Callback[[ToolCall], None] | None = None,
) -> "GenerateResult":
    """
    Generate one message based on the given context.
    Parts of the message will be streamed to the specified callbacks if provided.

    Args:
        chat_provider: The chat provider to use for generation.
        system_prompt: The system prompt to use for generation.
        tools: The tools available for the model to call.
        history: The message history to use for generation.
        on_message_part: An optional callback to be called for each raw message part.
        on_tool_call: An optional callback to be called for each complete tool call.

    Returns:
        A tuple of the generated message and the token usage (if available).
        All parts in the message are guaranteed to be complete and merged as much as possible.

    Raises:
        APIConnectionError: If the API connection fails.
        APITimeoutError: If the API request times out.
        APIStatusError: If the API returns a status code of 4xx or 5xx.
        APIEmptyResponseError: If the API returns an empty response.
        ChatProviderError: If any other recognized chat provider error occurs.
    """
    assembler = StreamMessageAssembler()
    output_published = False

    logger.trace("Generating with history: {history}", history=history)
    stream = await chat_provider.generate(system_prompt, tools, history)
    async for part in stream:
        logger.trace("Received part: {part}", part=part)
        if on_message_part:
            await callback(on_message_part, part.model_copy(deep=True))
            output_published = True

        try:
            assembler.add(part)
        except APIStreamProtocolError as error:
            error.output_published = output_published
            if error.response_id is None:
                error.response_id = stream.id
            raise

    try:
        message = assembler.finish(
            response_id=stream.id,
            finish_reason=stream.finish_reason,
        )
    except APIStreamProtocolError as error:
        error.output_published = output_published
        if error.response_id is None:
            error.response_id = stream.id
        raise

    for tool_call in message.tool_calls or []:
        if on_tool_call:
            await callback(on_tool_call, tool_call)

    if not message.content and not message.tool_calls:
        raise APIEmptyResponseError("The API returned an empty response.")

    # A response with only ThinkPart (no TextPart, no tool calls) indicates an
    # abnormal termination — typically a stream interruption or max_tokens
    # exhaustion during reasoning.  The model should always produce visible
    # output after thinking; a think-only response is never intentional.
    has_think = any(isinstance(p, ThinkPart) for p in message.content)
    has_text = any(isinstance(p, TextPart) and p.text.strip() for p in message.content)
    if has_think and not has_text and not message.tool_calls:
        raise APIEmptyResponseError(
            "The API returned a response containing only thinking content "
            "without any text or tool calls. This usually indicates the "
            "stream was interrupted or the output token budget was exhausted "
            "during reasoning."
        )

    return GenerateResult(
        id=stream.id,
        message=message,
        usage=stream.usage,
        # finish_reason 'length' means the output-token limit cut the response off. It is a
        # required member of the StreamedMessage contract (every provider maps its own signal
        # onto it), so the agent loop can always detect and recover from truncation instead of
        # treating a cut-off response as a clean completion.
        truncated=stream.finish_reason == "length",
    )


@dataclass(frozen=True, slots=True)
class GenerateResult:
    """The result of a generation."""

    id: str | None
    """The ID of the generated message."""
    message: Message
    """The generated message."""
    usage: TokenUsage | None
    """The token usage of the generated message."""
    truncated: bool = False
    """True when the response was cut off by the output-token limit (finish_reason 'length')."""
