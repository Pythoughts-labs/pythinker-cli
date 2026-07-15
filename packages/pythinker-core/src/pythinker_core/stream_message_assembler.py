from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from pythinker_core.chat_provider import (
    APIStreamProtocolError,
    StreamedMessagePart,
    StreamProtocolErrorCategory,
)
from pythinker_core.message import ContentPart, Message, ToolCall, ToolCallPart
from pythinker_core.utils.typing import JsonType

_ALLOWED_TOOL_FINISH_REASONS = {
    None,
    "stop",
    "tool_calls",
    "function_call",
    "tool_use",
    "end_turn",
    "stop_sequence",
    "completed",
}


@dataclass
class _CallState:
    stream_index: int | None = None
    call_id: str | None = None
    started: bool = False
    complete_name: str | None = None
    name_parts: list[str] = field(default_factory=list[str])
    argument_parts: list[str] = field(default_factory=list[str])
    extras: dict[str, JsonType] | None = None


class StreamMessageAssembler:
    """Correlate provider-neutral streamed message parts into one assistant message."""

    def __init__(self) -> None:
        self._content: list[ContentPart] = []
        self._calls: list[_CallState] = []
        self._calls_by_index: dict[int, _CallState] = {}
        self._calls_by_id: dict[str, _CallState] = {}

    def add(self, part: StreamedMessagePart) -> None:
        if isinstance(part, ToolCall):
            self._add_call(part)
            return
        if isinstance(part, ToolCallPart):
            self._add_call_part(part)
            return
        if not self._content or not self._content[-1].merge_in_place(part):
            self._content.append(part)

    def finish(
        self,
        *,
        response_id: str | None,
        finish_reason: str | None,
    ) -> Message:
        orphan = next((state for state in self._calls if not state.started), None)
        if orphan is not None:
            self._raise(
                "orphan_fragment",
                response_id=response_id,
                state=orphan,
            )

        started = [state for state in self._calls if state.started]
        if started:
            self._validate_finish_reason(finish_reason, response_id=response_id)

        ordered = (
            sorted(started, key=self._indexed_order)
            if started and all(state.stream_index is not None for state in started)
            else started
        )
        calls = [
            self._finalize_call(state, response_id=response_id, order=order)
            for order, state in enumerate(ordered)
        ]
        return Message(role="assistant", content=self._content, tool_calls=calls or None)

    def _add_call(self, call: ToolCall) -> None:
        call_id = self._nonblank(call.id)
        state = self._find_correlated(call.stream_index, call_id)
        if state is None:
            self._validate_new_start_mode(call.stream_index)
            state = self._new_state()
        elif not state.started:
            self._validate_new_start_mode(call.stream_index)

        self._bind_index(state, call.stream_index, call_id=call_id)
        self._bind_id(state, call_id, stream_index=call.stream_index)

        complete_name = self._nonblank(call.function.name)
        if state.complete_name is not None and complete_name is not None:
            if state.complete_name != complete_name:
                self._raise("conflicting_name", state=state)
        elif complete_name is not None:
            state.complete_name = complete_name

        if call.function.arguments is not None:
            state.argument_parts.append(call.function.arguments)
        if state.extras is None:
            state.extras = call.extras
        state.started = True

    def _add_call_part(self, part: ToolCallPart) -> None:
        call_id = self._nonblank(part.stream_call_id)
        keyed = part.stream_index is not None or call_id is not None
        state = self._find_correlated(part.stream_index, call_id)

        if state is None and keyed:
            state = self._new_state()
        elif state is None:
            started = [candidate for candidate in self._calls if candidate.started]
            if len(started) > 1:
                raise APIStreamProtocolError("ambiguous_fragment")
            if not started:
                raise APIStreamProtocolError("orphan_fragment")
            state = started[0]

        self._bind_index(state, part.stream_index, call_id=call_id)
        self._bind_id(state, call_id, stream_index=part.stream_index)
        if part.name_part is not None:
            state.name_parts.append(part.name_part)
        if part.arguments_part is not None:
            state.argument_parts.append(part.arguments_part)

    def _find_correlated(self, stream_index: int | None, call_id: str | None) -> _CallState | None:
        indexed = self._calls_by_index.get(stream_index) if stream_index is not None else None
        identified = self._calls_by_id.get(call_id) if call_id is not None else None
        if indexed is not None and identified is not None and indexed is not identified:
            raise APIStreamProtocolError(
                "conflicting_identity",
                stream_index=stream_index,
                call_id=call_id,
            )
        if indexed is not None:
            return indexed
        if identified is not None:
            if (
                stream_index is not None
                and identified.stream_index is not None
                and identified.stream_index != stream_index
            ):
                raise APIStreamProtocolError(
                    "conflicting_identity",
                    stream_index=stream_index,
                    call_id=call_id,
                )
            return identified
        return None

    def _new_state(self) -> _CallState:
        state = _CallState()
        self._calls.append(state)
        return state

    def _bind_index(
        self,
        state: _CallState,
        stream_index: int | None,
        *,
        call_id: str | None,
    ) -> None:
        if stream_index is None:
            return
        if state.stream_index is not None and state.stream_index != stream_index:
            raise APIStreamProtocolError(
                "conflicting_identity",
                stream_index=stream_index,
                call_id=call_id,
            )
        existing = self._calls_by_index.get(stream_index)
        if existing is not None and existing is not state:
            raise APIStreamProtocolError(
                "conflicting_identity",
                stream_index=stream_index,
                call_id=call_id,
            )
        state.stream_index = stream_index
        self._calls_by_index[stream_index] = state

    def _bind_id(
        self,
        state: _CallState,
        call_id: str | None,
        *,
        stream_index: int | None,
    ) -> None:
        if call_id is None:
            return
        if state.call_id is not None and state.call_id != call_id:
            raise APIStreamProtocolError(
                "conflicting_identity",
                stream_index=stream_index,
                call_id=call_id,
            )
        existing = self._calls_by_id.get(call_id)
        if existing is not None and existing is not state:
            raise APIStreamProtocolError(
                "conflicting_identity",
                stream_index=stream_index,
                call_id=call_id,
            )
        state.call_id = call_id
        self._calls_by_id[call_id] = state

    def _validate_new_start_mode(self, stream_index: int | None) -> None:
        started = [state for state in self._calls if state.started]
        if started and (started[0].stream_index is None) != (stream_index is None):
            raise APIStreamProtocolError(
                "mixed_correlation",
                stream_index=stream_index,
            )

    def _validate_finish_reason(
        self, finish_reason: str | None, *, response_id: str | None
    ) -> None:
        normalized = finish_reason.strip().lower() if finish_reason else None
        if normalized in _ALLOWED_TOOL_FINISH_REASONS:
            return
        category: StreamProtocolErrorCategory = (
            "truncated_tool_call" if normalized == "length" else "terminal_failure"
        )
        raise APIStreamProtocolError(category, response_id=response_id)

    def _finalize_call(
        self,
        state: _CallState,
        *,
        response_id: str | None,
        order: int,
    ) -> ToolCall:
        fragmented_name = "".join(state.name_parts)
        if state.complete_name is not None and fragmented_name:
            if state.complete_name != fragmented_name:
                self._raise("conflicting_name", response_id=response_id, state=state)
            name = state.complete_name
        else:
            name = state.complete_name or fragmented_name
        if not name:
            self._raise("missing_name", response_id=response_id, state=state)

        arguments = "".join(state.argument_parts) or "{}"
        call_id = state.call_id
        if call_id is None:
            seed = f"{response_id or 'local'}:{order}:{name}:{arguments}"
            digest = hashlib.sha256(seed.encode(encoding="utf-8")).hexdigest()[:20]
            call_id = f"call_{digest}"

        return ToolCall(
            id=call_id,
            function=ToolCall.FunctionBody(name=name, arguments=arguments),
            extras=state.extras,
        )

    @staticmethod
    def _indexed_order(state: _CallState) -> int:
        assert state.stream_index is not None
        return state.stream_index

    @staticmethod
    def _nonblank(value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value

    @staticmethod
    def _raise(
        category: StreamProtocolErrorCategory,
        *,
        response_id: str | None = None,
        state: _CallState,
    ) -> None:
        raise APIStreamProtocolError(
            category,
            response_id=response_id,
            stream_index=state.stream_index,
            call_id=state.call_id,
        )
