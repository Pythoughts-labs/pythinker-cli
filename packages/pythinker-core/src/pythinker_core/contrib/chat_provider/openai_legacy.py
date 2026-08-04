import copy
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Any, Self, Unpack, cast

import httpx
from openai import AsyncStream, Omit, OpenAIError, omit
from openai.types import CompletionUsage, ReasoningEffort
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
)
from typing_extensions import TypedDict

from pythinker_core.chat_provider import (
    ChatProvider,
    RetryableChatProvider,
    StreamedMessagePart,
    ThinkingEffort,
    TokenUsage,
)
from pythinker_core.chat_provider.openai_common import (
    close_replaced_openai_client,
    convert_error,
    create_openai_client,
    reasoning_effort_to_thinking_effort,
    thinking_effort_to_reasoning_effort,
    tool_to_openai,
)
from pythinker_core.contrib.chat_provider.common import (
    ReasoningReplayMode,
    ToolMessageConversion,
)
from pythinker_core.message import ContentPart, Message, TextPart, ThinkPart, ToolCall, ToolCallPart
from pythinker_core.tooling import Tool

if TYPE_CHECKING:

    def type_check(openai_legacy: "OpenAILegacy"):
        _: ChatProvider = openai_legacy
        _: RetryableChatProvider = openai_legacy


class OpenAILegacy:
    """
    A chat provider that uses the OpenAI Chat Completions API.

    >>> chat_provider = OpenAILegacy(model="gpt-5", api_key="sk-1234567890")
    >>> chat_provider.name
    'openai'
    >>> chat_provider.model_name
    'gpt-5'
    """

    name = "openai"

    class GenerationKwargs(TypedDict, extra_items=Any, total=False):
        """
        Generation kwargs for various kinds of OpenAI-compatible APIs.
        `extra_items=Any` is used to support any extra args.
        """

        max_tokens: int | None
        temperature: float | None
        top_p: float | None
        n: int | None
        presence_penalty: float | None
        frequency_penalty: float | None
        stop: str | list[str] | None
        prompt_cache_key: str | None

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        stream: bool = True,
        reasoning_key: str | None = None,
        reasoning_replay_mode: ReasoningReplayMode | None = None,
        auto_reasoning_effort: bool = True,
        tool_stream: bool = False,
        tool_message_conversion: ToolMessageConversion | None = None,
        **client_kwargs: Any,
    ):
        """
        Initialize the OpenAILegacy chat provider.

        To support OpenAI-compatible APIs that inject reasoning content in a extra field in
        the message, such as `{"reasoning": ...}`, `reasoning_key` can be set to the key name.
        """
        self.model = model
        self.stream = stream
        self._api_key: str | None = api_key
        self._base_url: str | None = base_url
        self._client_kwargs: dict[str, Any] = dict(client_kwargs)
        self.client = create_openai_client(
            api_key=self._api_key,
            base_url=self._base_url,
            client_kwargs=self._client_kwargs,
        )
        """The underlying `AsyncOpenAI` client."""
        self._reasoning_effort: ReasoningEffort | Omit = omit
        self._reasoning_key = reasoning_key
        self._reasoning_replay_mode: ReasoningReplayMode = reasoning_replay_mode or (
            "strict_synthetic" if _is_strict_interleaved_model(model) else "tool_calls"
        )
        self._auto_reasoning_effort = auto_reasoning_effort
        self._tool_stream = tool_stream
        self._tool_message_conversion: ToolMessageConversion | None = tool_message_conversion
        self._generation_kwargs: OpenAILegacy.GenerationKwargs = {}

    @property
    def model_name(self) -> str:
        return self.model

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        if isinstance(self._reasoning_effort, Omit):
            return None
        return reasoning_effort_to_thinking_effort(self._reasoning_effort)

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> "OpenAILegacyStreamedMessage":
        messages: list[ChatCompletionMessageParam] = []
        if system_prompt:
            # `system` vs `developer`: see `message_to_openai` comments
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(self._convert_message(message) for message in history)

        generation_kwargs: dict[str, Any] = {}
        generation_kwargs.update(copy.deepcopy(self._generation_kwargs))
        if self._tool_stream and tools:
            extra_body = dict(generation_kwargs.get("extra_body") or {})
            extra_body["tool_stream"] = True
            generation_kwargs["extra_body"] = extra_body

        reasoning_effort = self._reasoning_effort
        # Auto-enable reasoning_effort when the history contains ThinkPart but reasoning
        # was not explicitly configured. This prevents server validation errors from APIs
        # (e.g. One API) that require reasoning_effort when messages contain reasoning_content.
        # See: https://github.com/Pythoughts-labs/pythinker-cli/issues/1616
        if (
            self._auto_reasoning_effort
            and isinstance(reasoning_effort, Omit)
            and self._reasoning_key
        ):
            has_think_part = any(
                isinstance(part, ThinkPart) for message in history for part in message.content
            )
            if has_think_part:
                reasoning_effort = "medium"

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=(tool_to_openai(tool) for tool in tools),
                stream=self.stream,
                stream_options={"include_usage": True} if self.stream else omit,
                reasoning_effort=reasoning_effort,
                **generation_kwargs,
            )
            return OpenAILegacyStreamedMessage(response, self._reasoning_key)
        except (OpenAIError, httpx.HTTPError) as e:
            raise convert_error(e) from e

    def on_retryable_error(self, error: BaseException) -> bool:
        old_client = self.client
        self.client = create_openai_client(
            api_key=self._api_key,
            base_url=self._base_url,
            client_kwargs=self._client_kwargs,
        )
        close_replaced_openai_client(old_client, client_kwargs=self._client_kwargs)
        return True

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        new_self = copy.copy(self)
        new_self._reasoning_effort = thinking_effort_to_reasoning_effort(effort)
        return new_self

    def with_generation_kwargs(self, **kwargs: Unpack[GenerationKwargs]) -> Self:
        """
        Copy the chat provider, updating the generation kwargs with the given values.

        Returns:
            Self: A new instance of the chat provider with updated generation kwargs.
        """
        new_self = copy.copy(self)
        new_self._generation_kwargs = copy.deepcopy(self._generation_kwargs)
        new_self._generation_kwargs.update(kwargs)
        return new_self

    @property
    def model_parameters(self) -> dict[str, Any]:
        """
        The parameters of the model to use.

        For tracing/logging purposes.
        """

        model_parameters: dict[str, Any] = {"base_url": str(self.client.base_url)}
        if self._reasoning_effort is not omit:
            model_parameters["reasoning_effort"] = self._reasoning_effort
        return model_parameters

    def _convert_message(self, message: Message) -> ChatCompletionMessageParam:
        """Convert a Pythinker Core message to OpenAI message."""
        # Note: for openai, `developer` role is more standard, but `system` is still accepted.
        # And many openai-compatible models do not accept `developer` role.
        # So we use `system` role here. OpenAIResponses will use `developer` role.
        # See https://cdn.openai.com/spec/model-spec-2024-05-08.html#definitions
        message = message.model_copy(deep=True)
        reasoning_content: str = ""
        content: list[ContentPart] = []
        for part in message.content:
            if isinstance(part, ThinkPart):
                reasoning_content += part.think
            else:
                content.append(part)
        # if tool message and `tool_result_conversion` is `extract_text`, patch all text parts into
        # one so that we can make use of the serialization process of `Message` to output string
        if message.role == "tool" and self._tool_message_conversion == "extract_text":
            message.content = [TextPart(text=message.extract_text(sep="\n"))]
        else:
            message.content = content
        dumped_message = message.model_dump(exclude_none=True)
        if self._reasoning_key and reasoning_content:
            dumped_message[self._reasoning_key] = reasoning_content
        elif self._reasoning_key and message.role == "assistant":
            if self._reasoning_replay_mode == "tool_calls" and message.tool_calls:
                dumped_message[self._reasoning_key] = ""
            elif self._reasoning_replay_mode == "strict_synthetic":
                dumped_message[self._reasoning_key] = (
                    message.extract_text() or "[reasoning unavailable]"
                )
        return cast(ChatCompletionMessageParam, dumped_message)


class OpenAILegacyStreamedMessage:
    def __init__(
        self, response: ChatCompletion | AsyncStream[ChatCompletionChunk], reasoning_key: str | None
    ):
        self._reasoning_key: str | None = reasoning_key
        if isinstance(response, ChatCompletion):
            self._iter = self._convert_non_stream_response(response)
        else:
            self._iter = self._convert_stream_response(response)
        self._id: str | None = None
        self._usage: CompletionUsage | None = None
        self._finish_reason: str | None = None

    def __aiter__(self) -> AsyncIterator[StreamedMessagePart]:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    @property
    def id(self) -> str | None:
        return self._id

    @property
    def finish_reason(self) -> str | None:
        # OpenAI reports 'length' natively when the output-token limit cut the response off,
        # which is exactly the value the loop's truncation check expects.
        return self._finish_reason

    @property
    def usage(self) -> TokenUsage | None:
        if self._usage:
            cached = 0
            other_input = self._usage.prompt_tokens
            if (
                self._usage.prompt_tokens_details
                and self._usage.prompt_tokens_details.cached_tokens
            ):
                cached = self._usage.prompt_tokens_details.cached_tokens
                other_input -= cached
            return TokenUsage(
                input_other=other_input,
                output=self._usage.completion_tokens,
                input_cache_read=cached,
            )
        return None

    async def _convert_non_stream_response(
        self,
        response: ChatCompletion,
    ) -> AsyncIterator[StreamedMessagePart]:
        self._id = response.id
        self._usage = response.usage
        self._finish_reason = response.choices[0].finish_reason
        message = response.choices[0].message
        reasoning_key = self._reasoning_key
        if reasoning_key and (reasoning_content := getattr(message, reasoning_key, None)):
            assert isinstance(reasoning_content, str)
            yield ThinkPart(think=reasoning_content)
        if message.content:
            yield TextPart(text=message.content)
        if message.tool_calls:
            for tool_call in message.tool_calls:
                if isinstance(tool_call, ChatCompletionMessageFunctionToolCall):
                    yield ToolCall(
                        id=tool_call.id or str(uuid.uuid4()),
                        function=ToolCall.FunctionBody(
                            name=tool_call.function.name,
                            arguments=tool_call.function.arguments,
                        ),
                    )

    async def _convert_stream_response(
        self,
        response: AsyncIterator[ChatCompletionChunk],
    ) -> AsyncIterator[StreamedMessagePart]:
        started_tool_call_indices: set[int] = set()
        try:
            async for chunk in response:
                if chunk.id:
                    self._id = chunk.id
                if chunk.usage:
                    self._usage = chunk.usage

                if not chunk.choices:
                    continue

                if chunk.choices[0].finish_reason:
                    self._finish_reason = chunk.choices[0].finish_reason

                delta = chunk.choices[0].delta

                # convert thinking content
                reasoning_key = self._reasoning_key
                if reasoning_key and (reasoning_content := getattr(delta, reasoning_key, None)):
                    assert isinstance(reasoning_content, str)
                    yield ThinkPart(think=reasoning_content)

                # convert text content
                if delta.content:
                    yield TextPart(text=delta.content)

                # convert tool calls
                for tool_call in delta.tool_calls or []:
                    function = tool_call.function
                    if function is None:
                        if tool_call.id is not None:
                            yield ToolCallPart(
                                stream_index=tool_call.index,
                                stream_call_id=tool_call.id,
                            )
                        continue

                    if tool_call.index not in started_tool_call_indices:
                        started_tool_call_indices.add(tool_call.index)
                        yield ToolCall(
                            id=tool_call.id or "",
                            function=ToolCall.FunctionBody(
                                name=function.name or "",
                                arguments=function.arguments,
                            ),
                            stream_index=tool_call.index,
                        )
                    elif (
                        tool_call.id is not None
                        or function.name is not None
                        or function.arguments is not None
                    ):
                        yield ToolCallPart(
                            arguments_part=function.arguments,
                            name_part=function.name,
                            stream_index=tool_call.index,
                            stream_call_id=tool_call.id,
                        )
        except (OpenAIError, httpx.HTTPError) as e:
            raise convert_error(e) from e


def _is_strict_interleaved_model(model_name: str) -> bool:
    normalized = model_name.lower().replace("_", "-")
    return "kimi-k2" in normalized or "deepseek" in normalized


if __name__ == "__main__":

    async def _dev_main():
        chat = OpenAILegacy(model="gpt-4o", stream=False)
        system_prompt = "You are a helpful assistant."
        history = [Message(role="user", content="Hello, how are you?")]
        async for part in await chat.generate(system_prompt, [], history):
            print(part.model_dump(exclude_none=True))

        tools = [
            Tool(
                name="get_weather",
                description="Get the weather",
                parameters={
                    "type": "object",
                    "properties": {
                        "city": {
                            "type": "string",
                            "description": "The city to get the weather for.",
                        },
                    },
                },
            )
        ]
        history = [Message(role="user", content="What's the weather in Beijing?")]
        stream = await chat.generate(system_prompt, tools, history)
        async for part in stream:
            print(part.model_dump(exclude_none=True))
        print("usage:", stream.usage)

    import asyncio

    from dotenv import load_dotenv

    load_dotenv()
    asyncio.run(_dev_main())
