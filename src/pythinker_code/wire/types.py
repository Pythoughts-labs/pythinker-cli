from __future__ import annotations

import asyncio
from typing import Any, Literal, TypeGuard, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
from pythinker_core.chat_provider import TokenUsage
from pythinker_core.message import (
    AudioURLPart,
    ContentPart,
    ImageURLPart,
    TextPart,
    ThinkPart,
    ToolCall,
    ToolCallPart,
    VideoURLPart,
)
from pythinker_core.tooling import (
    BriefDisplayBlock,
    DisplayBlock,
    ToolResult,
    ToolReturnValue,
    UnknownDisplayBlock,
)
from pythinker_core.utils.typing import JsonType

from pythinker_code.tools.display import (
    BackgroundTaskDisplayBlock,
    DiffDisplayBlock,
    ShellDisplayBlock,
    TodoDisplayBlock,
    TodoDisplayItem,
)
from pythinker_code.utils.typing import flatten_union


class TurnBegin(BaseModel):
    """
    Indicates the beginning of a new agent turn.
    This event must be sent before any other event in the turn.
    """

    user_input: str | list[ContentPart]


class SteerInput(BaseModel):
    """
    Indicates that the user appended follow-up input to the current running turn.
    This event is emitted after the current step finishes and the input is appended
    to context, before the next step begins.
    """

    user_input: str | list[ContentPart]


class TurnEnd(BaseModel):
    """
    Indicates the end of the current agent turn.
    This event must be sent after all other events in the turn.
    If the turn is interrupted, this event may be omitted.
    """

    pass


class StepBegin(BaseModel):
    """
    Indicates the beginning of a new agent step.
    This event must be sent before any other event in the step.
    """

    n: int
    """The step number."""


class StepInterrupted(BaseModel):
    """Indicates the current step was interrupted, either by user intervention or an error."""

    pass


class StepRetry(BaseModel):
    """Indicates that the current step attempt failed and will be retried."""

    n: int
    """The step number."""
    next_attempt: int
    """The next attempt number, 1-based."""
    max_attempts: int
    """The maximum number of attempts for this step."""
    wait_s: float
    """Seconds to wait before retrying."""
    error_type: str
    """The exception class name that triggered the retry."""
    status_code: int | None = None
    """HTTP status code when available."""


class ToolExecutionStarted(BaseModel):
    """Indicates that a tool call has passed approval/hooks and is executing."""

    tool_call_id: str
    """The ID of the tool call that started executing."""


class ToolOutputPart(BaseModel):
    """Incremental output produced by a currently executing tool."""

    tool_call_id: str
    """The ID of the tool call that produced this output."""
    stream: Literal["stdout", "stderr", "output"] = "output"
    """The output stream, when known."""
    text: str
    """The output chunk text."""


class CompactionBegin(BaseModel):
    """
    Indicates that a compaction just began.
    This event must be sent during a step, which means, between `StepBegin` and the next
    `StepBegin` or `StepInterrupted`. And, there must be a `CompactionEnd` directly following
    this event.
    """

    pass


class CompactionEnd(BaseModel):
    """
    Indicates that a compaction just ended.
    This event must be sent directly after a `CompactionBegin` event.
    """

    pass


class HookTriggered(BaseModel):
    """A batch of hooks has been triggered and is now executing."""

    event: str
    """The hook event type, e.g. 'PreToolUse', 'Stop'."""
    target: str = ""
    """What the hooks are targeting: tool name for tool hooks,
    agent name for subagent hooks, etc."""
    hook_count: int = 1
    """Number of matched hooks running in parallel."""


# Engine truncates each stream to 12_000 chars (hooks/engine._MAX_HOOK_OUTPUT_CHARS)
# then appends a "\n...[truncated]" marker; allow headroom so the marked payload
# still validates while rejecting unbounded strings from any other producer.
_MAX_HOOK_OUTPUT_FIELD_CHARS = 12_032


class HookOutput(BaseModel):
    """Bounded stdout/stderr captured from one hook invocation."""

    stdout: str = Field(default="", max_length=_MAX_HOOK_OUTPUT_FIELD_CHARS)
    """Captured stdout, truncated before it is sent over the wire."""
    stderr: str = Field(default="", max_length=_MAX_HOOK_OUTPUT_FIELD_CHARS)
    """Captured stderr, truncated before it is sent over the wire."""
    exit_code: int = 0
    """Hook process exit code, when available."""
    timed_out: bool = False
    """Whether the hook timed out."""
    truncated: bool = False
    """Whether stdout or stderr was truncated for transport/display."""


class HookResolved(BaseModel):
    """A batch of hooks has finished executing."""

    event: str
    """The hook event type, e.g. 'PreToolUse', 'Stop'."""
    target: str = ""
    """Same as HookTriggered.target."""
    action: Literal["allow", "block"] = "allow"
    """Aggregate decision: 'block' if any hook blocked, 'allow' otherwise."""
    reason: str = ""
    """Reason for blocking. Empty if allowed."""
    duration_ms: int = 0
    """Wall-clock time for the entire batch, in milliseconds."""
    outputs: tuple[HookOutput, ...] = ()
    """Bounded visible output emitted by matched hooks."""


class MCPLoadingBegin(BaseModel):
    """Indicates that MCP tool loading is in progress."""

    pass


class MCPLoadingEnd(BaseModel):
    """Indicates that MCP tool loading has finished."""

    pass


class MCPServerSnapshot(BaseModel):
    """A snapshot of one MCP server during startup."""

    name: str
    status: Literal["pending", "connecting", "connected", "failed", "unauthorized"]
    tools: tuple[str, ...] = ()
    error: str | None = None
    """One short actionable line explaining a failed connect."""


class MCPStatusSnapshot(BaseModel):
    """A snapshot of MCP startup progress."""

    loading: bool
    connected: int
    total: int
    tools: int
    servers: tuple[MCPServerSnapshot, ...] = ()


class StatusUpdate(BaseModel):
    """
    An update on the current status of the soul.
    None fields indicate no change from the previous status.
    """

    context_usage: float | None = None
    """The usage of the context, in percentage."""
    context_tokens: int | None = None
    """The number of tokens currently in the context."""
    max_context_tokens: int | None = None
    """The maximum number of tokens the context can hold."""
    token_usage: TokenUsage | None = None
    """The token usage statistics of the current step."""
    message_id: str | None = None
    """The message ID of the current step."""
    model_name: str | None = None
    """The model ID that produced this step (e.g. 'claude-sonnet-4-5')."""
    provider_key: str | None = None
    """The provider config key for this step (e.g. 'managed:openai-chatgpt')."""
    plan_mode: bool | None = None
    """Whether plan mode (read-only) is active. None means no change."""
    mcp_status: MCPStatusSnapshot | None = None
    """The current MCP startup snapshot. None means no change."""


class Notification(BaseModel):
    """A generic system notification for UI and client consumption."""

    id: str
    category: str
    type: str
    source_kind: str
    source_id: str
    title: str
    body: str
    severity: str
    created_at: float
    payload: dict[str, JsonType] = Field(default_factory=dict)


class PlanDisplay(BaseModel):
    """Displays a plan's content inline in the chat with special formatting."""

    content: str
    """The full markdown content of the plan."""
    file_path: str
    """The path to the plan file for reference."""


class BtwBegin(BaseModel):
    """Indicates that a side question (/btw) has started processing."""

    id: str
    """Unique ID to pair with the corresponding BtwEnd."""
    question: str
    """The user's original side question text."""


class BtwEnd(BaseModel):
    """Indicates that a side question (/btw) has finished."""

    id: str
    """Unique ID matching the BtwBegin."""
    response: str | None = None
    """The LLM's response text, or None if it failed."""
    error: str | None = None
    """Error message if the side question failed."""


class TodoListUpdated(BaseModel):
    """Todo list state was persisted or refreshed (independent of ToolResult rendering)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[tuple[str, str], ...]
    """Todo (title, status) pairs in display order."""
    complete: bool
    """True when the list is closed (all done or cleared)."""
    source: Literal["tool", "scratch", "compaction"]
    """What produced this update."""


class SubagentToolFallback(BaseModel):
    """Agent tool launch was rejected before a subagent started."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: Literal[
        "unavailable_agent_type",
        "mcp_unavailable",
        "policy_denied",
        "timeout",
        "exception",
    ]
    requested_type: str
    available_types: tuple[str, ...] = ()
    """Known built-in types when the rejection is type-related."""


class AgentListDelta(BaseModel):
    """Dynamic agent-type listing for cache-stable prompt injection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[str, ...]
    """Formatted agent lines, or delta lines when ``complete`` is False."""
    complete: bool
    """True closes the list; False means apply as a delta on the prior open list."""


class ToolUseSkipped(BaseModel):
    """A tool call did not execute normally (dedup, policy, interrupt, or busy skip)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_call_id: str
    tool_name: str
    reason: Literal["dedup", "policy", "interrupt", "concurrent_inflight"]
    resumed: bool = False
    """True when the skip is a reuse/resume of an in-flight or prior result."""


class ContextOverflowRecovered(BaseModel):
    """Reactive context-overflow recovery attempt finished."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["recovered", "failed"]
    trigger_step: int
    """Step number that triggered the overflow rejection."""


class SubagentEvent(BaseModel):
    """
    An event from a subagent.
    """

    parent_tool_call_id: str | None = None
    """The ID of the parent Agent tool call associated with this subagent."""
    agent_id: str | None = None
    """The subagent instance ID."""
    subagent_type: str | None = None
    """The built-in subagent type used by this instance."""
    event: Event
    """The event from the subagent."""
    # TODO: maybe restrict the event types? to exclude approval request, etc.

    @model_validator(mode="before")
    @classmethod
    def _compat_legacy_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(cast(dict[str, Any], value))
        if "parent_tool_call_id" not in data and "task_tool_call_id" in data:
            data["parent_tool_call_id"] = data["task_tool_call_id"]
        return data

    @field_serializer("event", when_used="json")
    def _serialize_event(self, event: Event) -> dict[str, Any]:
        envelope = WireMessageEnvelope.from_wire_message(event)
        return envelope.model_dump(mode="json")

    @field_validator("event", mode="before")
    @classmethod
    def _validate_event(cls, value: Any) -> Event:
        if is_wire_message(value):
            if is_event(value):
                return value
            raise ValueError("SubagentEvent event must be an Event")

        if not isinstance(value, dict):
            raise ValueError("SubagentEvent event must be a dict")
        event_type = cast(dict[str, Any], value).get("type")
        event_payload = cast(dict[str, Any], value).get("payload")
        envelope = WireMessageEnvelope.model_validate(
            {"type": event_type, "payload": event_payload}
        )
        event = envelope.to_wire_message()
        if not is_event(event):
            raise ValueError("SubagentEvent event must be an Event")
        return event


class ApprovalResponse(BaseModel):
    """
    Indicates that an approval request has been resolved.
    """

    type Kind = Literal["approve", "approve_for_session", "reject"]

    request_id: str
    """The ID of the resolved approval request."""
    response: Kind
    """The response to the approval request."""
    feedback: str = ""
    """Optional user feedback when rejecting (e.g. instructions for the model)."""


class ApprovalRequest(BaseModel):
    """
    A request for user approval before proceeding with an action.
    """

    id: str
    tool_call_id: str
    sender: str
    action: str
    description: str
    source_kind: Literal["foreground_turn", "background_agent"] | None = None
    source_id: str | None = None
    agent_id: str | None = None
    subagent_type: str | None = None
    source_description: str | None = None
    display: list[DisplayBlock] = Field(default_factory=list[DisplayBlock])
    """Defaults to an empty list for backwards-compatible wire.jsonl loading."""

    # Note that the above fields are just a copy of `pythinker_code.soul.approval.Request`, but
    # we cannot directly use that class here because we want to avoid dependency from Wire
    # to Soul.

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._future: asyncio.Future[ApprovalResponse.Kind] | None = None
        self._feedback: str = ""

    def _get_future(self) -> asyncio.Future[ApprovalResponse.Kind]:
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
        return self._future

    async def wait(self) -> ApprovalResponse.Kind:
        """
        Wait for the request to be resolved or cancelled.

        Returns:
            ApprovalResponse.Kind: The response to the approval request.
        """
        return await self._get_future()

    def resolve(self, response: ApprovalResponse.Kind, feedback: str = "") -> None:
        """
        Resolve the approval request with the given response.
        This will cause the `wait()` method to return the response.
        """
        self._feedback = feedback
        future = self._get_future()
        if not future.done():
            future.set_result(response)

    @property
    def feedback(self) -> str:
        """User feedback text provided with a rejection, if any."""
        return self._feedback

    @property
    def resolved(self) -> bool:
        """Whether the request is resolved."""
        return self._future is not None and self._future.done()


class QuestionOption(BaseModel):
    """A single option for a question."""

    label: str
    """The display text for this option."""
    description: str = ""
    """Explanation of what this option means."""


class QuestionItem(BaseModel):
    """A single question to ask the user."""

    question: str
    """The complete question text."""
    header: str = ""
    """Short label displayed as a tag (max 12 chars)."""
    options: list[QuestionOption]
    """The available choices for this question (2-4 options)."""
    multi_select: bool = False
    """Whether multiple options can be selected."""
    body: str = ""
    """Optional body content (markdown) displayed above options."""
    other_label: str = ""
    """Custom label for the synthetic 'Other' free-text option. Empty uses default."""
    other_description: str = ""
    """Custom description for the synthetic 'Other' option. Empty uses default."""


class QuestionResponse(BaseModel):
    """Response to a question request."""

    request_id: str
    """The ID of the resolved question request."""
    answers: dict[str, str]
    """Mapping from question text to selected option label(s). Multi-select answers are
    comma-separated."""


class QuestionAnswered(BaseModel):
    """Transcript event emitted after an interactive question is resolved."""

    request_id: str
    """The ID of the resolved question request."""
    tool_call_id: str
    """The tool call that asked the question."""
    answers: dict[str, str]
    """Mapping from question text to selected answer label(s). Empty when dismissed."""
    dismissed: bool = False
    """Whether the user dismissed the question without answering."""


class ProgressNote(BaseModel):
    """A compact progress/checkpoint note for transcript UIs."""

    title: str
    """Short note title, for example ``Checkpoint``."""
    body: str = ""
    """Optional markdown body shown below the title."""


class Suggestion(BaseModel):
    """A non-blocking, optional next-action suggestion for the user.

    Unlike the blocking AskUserQuestion, this never pauses the turn: the model
    posts an optional steer (e.g. "review my changes with /review") that the user
    may act on or ignore.
    """

    label: str
    """The suggested action, e.g. ``Review my changes``."""
    prefill: str = ""
    """Optional text/slash-command to prefill the input if the user accepts."""
    category: str = ""
    """Optional grouping, e.g. ``review``."""


class QuestionNotSupported(Exception):
    """Raised when the connected client does not support interactive questions."""


class QuestionRequest(BaseModel):
    """
    A request to ask the user structured questions during execution.
    """

    id: str
    """The unique request ID."""
    tool_call_id: str
    """The ID of the tool call that initiated this question."""
    questions: list[QuestionItem]
    """The questions to ask the user (1-4 questions)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._future: asyncio.Future[dict[str, str]] | None = None

    def _get_future(self) -> asyncio.Future[dict[str, str]]:
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
        return self._future

    async def wait(self) -> dict[str, str]:
        """
        Wait for the question to be answered.

        Returns:
            dict[str, str]: Mapping from question text to answer.
        """
        return await self._get_future()

    def resolve(self, answers: dict[str, str]) -> None:
        """
        Resolve the question request with the given answers.
        This will cause the `wait()` method to return the answers.
        """
        future = self._get_future()
        if not future.done():
            future.set_result(answers)

    def set_exception(self, exc: BaseException) -> None:
        """Resolve the question request with an exception."""
        future = self._get_future()
        if not future.done():
            future.set_exception(exc)

    @property
    def resolved(self) -> bool:
        """Whether the question request is resolved."""
        return self._future is not None and self._future.done()


class ToolCallRequest(BaseModel):
    """
    A tool call request routed to the Wire client for execution.
    """

    id: str
    """The ID of the tool call."""
    name: str
    """The name of the tool to call."""
    arguments: str | None
    """Arguments of the tool call in JSON string format."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._future: asyncio.Future[ToolReturnValue] | None = None

    def _get_future(self) -> asyncio.Future[ToolReturnValue]:
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
        return self._future

    @staticmethod
    def from_tool_call(tool_call: ToolCall) -> ToolCallRequest:
        return ToolCallRequest(
            id=tool_call.id,
            name=tool_call.function.name,
            arguments=tool_call.function.arguments,
        )

    async def wait(self) -> ToolReturnValue:
        """
        Wait for the tool call to be resolved or cancelled.

        Returns:
            ToolReturnValue: The tool execution result.
        """
        return await self._get_future()

    def resolve(self, result: ToolReturnValue) -> None:
        """
        Resolve the tool call with the given result.
        This will cause the `wait()` method to return the result.
        """
        future = self._get_future()
        if not future.done():
            future.set_result(result)

    @property
    def resolved(self) -> bool:
        """Whether the tool call is resolved."""
        return self._future is not None and self._future.done()


type Event = (
    TurnBegin
    | SteerInput
    | TurnEnd
    | StepBegin
    | StepInterrupted
    | StepRetry
    | ToolExecutionStarted
    | ToolOutputPart
    | HookTriggered
    | HookResolved
    | CompactionBegin
    | CompactionEnd
    | MCPLoadingBegin
    | MCPLoadingEnd
    | StatusUpdate
    | Notification
    | ContentPart
    | ToolCall
    | ToolCallPart
    | ToolResult
    | ApprovalResponse
    | QuestionAnswered
    | ProgressNote
    | Suggestion
    | SubagentEvent
    | PlanDisplay
    | BtwBegin
    | BtwEnd
    | TodoListUpdated
    | SubagentToolFallback
    | AgentListDelta
    | ToolUseSkipped
    | ContextOverflowRecovered
)
"""Any event, including control flow and content/tooling events."""


class HookResponse(BaseModel):
    """
    Client response to a HookRequest.
    """

    request_id: str
    """The ID of the HookRequest being responded to."""
    action: Literal["allow", "block"] = "allow"
    """The decision: allow the action or block it."""
    reason: str = ""
    """Reason for blocking. Empty if allowed."""


class HookRequest(BaseModel):
    """
    A request for the wire client to handle a hook event.
    The client runs its own logic and responds with allow/block.
    """

    type Action = Literal["allow", "block"]

    id: str
    """Unique request ID."""
    subscription_id: str = ""
    """Which subscription triggered this request."""
    event: str
    """The hook event type, e.g. 'PreToolUse', 'Stop'."""
    target: str = ""
    """What triggered the hook: tool name, agent name, etc."""
    input_data: dict[str, Any] = Field(default_factory=dict)
    """Full event payload (same as what shell hooks get on stdin)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._future: asyncio.Future[tuple[HookRequest.Action, str]] | None = None

    def _get_future(self) -> asyncio.Future[tuple[HookRequest.Action, str]]:
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
        return self._future

    async def wait(self) -> tuple[Action, str]:
        """Wait for client response. Returns (action, reason)."""
        return await self._get_future()

    def resolve(self, action: Action, reason: str = "") -> None:
        """Resolve with client's decision."""
        future = self._get_future()
        if not future.done():
            future.set_result((action, reason))

    @property
    def resolved(self) -> bool:
        return self._future is not None and self._future.done()


type Request = ApprovalRequest | ToolCallRequest | QuestionRequest | HookRequest
"""Any request. Request is a message that expects a response."""

type WireMessage = Event | Request
"""Any message sent over the `Wire`."""


_EVENT_TYPES = cast(tuple[type[Event], ...], flatten_union(Event))
_REQUEST_TYPES = cast(tuple[type[Request], ...], flatten_union(Request))
_WIRE_MESSAGE_TYPES = cast(tuple[type[WireMessage], ...], flatten_union(WireMessage))


def is_event(msg: Any) -> TypeGuard[Event]:
    """Check if the message is an Event."""
    return isinstance(msg, _EVENT_TYPES)


def is_request(msg: Any) -> TypeGuard[Request]:
    """Check if the message is a Request."""
    return isinstance(msg, _REQUEST_TYPES)


def is_wire_message(msg: Any) -> TypeGuard[WireMessage]:
    """Check if the message is a WireMessage."""
    return isinstance(msg, _WIRE_MESSAGE_TYPES)


_NAME_TO_WIRE_MESSAGE_TYPE: dict[str, type[WireMessage]] = {
    cls.__name__: cls for cls in _WIRE_MESSAGE_TYPES
}
# for backwards compatibility with Wire v1
_NAME_TO_WIRE_MESSAGE_TYPE["ApprovalRequestResolved"] = ApprovalResponse


class WireMessageEnvelope(BaseModel):
    type: str
    payload: dict[str, JsonType]

    @classmethod
    def from_wire_message(cls, msg: WireMessage) -> WireMessageEnvelope:
        typename: str | None = None
        for name, typ in _NAME_TO_WIRE_MESSAGE_TYPE.items():
            if issubclass(type(msg), typ):
                typename = name
                break
        assert typename is not None, f"Unknown wire message type: {type(msg)}"
        return cls(
            type=typename,
            payload=msg.model_dump(mode="json"),
        )

    def to_wire_message(self) -> WireMessage:
        """
        Convert the envelope back into a `WireMessage`.

        Raises:
            ValueError: If the message type is unknown or the payload is invalid.
        """
        msg_type = _NAME_TO_WIRE_MESSAGE_TYPE.get(self.type)
        if msg_type is None:
            raise ValueError(f"Unknown wire message type: {self.type}")
        return msg_type.model_validate(self.payload)


__all__ = [
    # `WireMessage` variants
    "TurnBegin",
    "SteerInput",
    "TurnEnd",
    "StepBegin",
    "StepInterrupted",
    "StepRetry",
    "ToolExecutionStarted",
    "ToolOutputPart",
    "HookTriggered",
    "HookOutput",
    "HookResolved",
    "CompactionBegin",
    "CompactionEnd",
    "MCPLoadingBegin",
    "MCPLoadingEnd",
    "StatusUpdate",
    "MCPServerSnapshot",
    "MCPStatusSnapshot",
    "Notification",
    "ContentPart",
    "ToolCall",
    "ToolCallPart",
    "ToolResult",
    "ApprovalResponse",
    "SubagentEvent",
    "PlanDisplay",
    "BtwBegin",
    "BtwEnd",
    "TodoListUpdated",
    "SubagentToolFallback",
    "AgentListDelta",
    "ToolUseSkipped",
    "ContextOverflowRecovered",
    "ApprovalRequest",
    "ToolCallRequest",
    "QuestionOption",
    "QuestionItem",
    "QuestionResponse",
    "QuestionAnswered",
    "ProgressNote",
    "Suggestion",
    "QuestionRequest",
    "QuestionNotSupported",
    # helpers
    "WireMessageEnvelope",
    # `StatusUpdate`-related
    "TokenUsage",
    # `ContentPart` types
    "TextPart",
    "ThinkPart",
    "ImageURLPart",
    "AudioURLPart",
    "VideoURLPart",
    # `ToolResult`-related
    "ToolReturnValue",
    # `DisplayBlock` types
    "DisplayBlock",
    "UnknownDisplayBlock",
    "BriefDisplayBlock",
    "DiffDisplayBlock",
    "TodoDisplayBlock",
    "TodoDisplayItem",
    "ShellDisplayBlock",
    "BackgroundTaskDisplayBlock",
]
