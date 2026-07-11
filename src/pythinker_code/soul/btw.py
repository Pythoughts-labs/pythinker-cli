"""Side question ("/btw") - answer a quick question without interrupting the main conversation.

Uses the same system_prompt + normalized history + tool definitions as the main
agent to maximize prompt cache hits.  Tools are declared (for cache) but denied
at execution time.  maxTurns=2 so if the LLM mistakenly calls a tool on the
first turn, the error result gives it a second chance to answer with text.

The question and response are NOT written to the main context history.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

import pythinker_core
from pythinker_core.message import Message, ToolCall
from pythinker_core.tooling import Tool, ToolError, ToolResult

from pythinker_code.soul import LLMNotSet, wire_send
from pythinker_code.soul.dynamic_injection import normalize_history
from pythinker_code.soul.message import system_reminder
from pythinker_code.utils.logging import logger
from pythinker_code.wire.types import BtwBegin, BtwEnd, TextPart

if TYPE_CHECKING:
    from pythinker_core.chat_provider import StreamedMessagePart

    from pythinker_code.soul.pythinkersoul import PythinkerSoul

_BTW_MAX_TURNS = 2

SIDE_QUESTION_SYSTEM_REMINDER = """\
This is a side question from the user. Answer directly in a single response.

IMPORTANT:
- You are a separate, lightweight instance answering one question.
- The main agent continues independently — do NOT reference being interrupted.
- Do NOT call any tools. All tool calls are disabled and will be rejected.
  Even though tool definitions are visible in this request, they exist only
  for technical reasons (prompt cache). You MUST NOT use them.
- Respond ONLY with text based on what you already know from the conversation.
- This is a one-off response — no follow-up turns.
- If you don't know the answer, say so directly."""


MAX_STEPS_HANDOFF_REMINDER = """\
You have hit the step limit for this turn and are being paused before the work is
finished. Write a short handoff for the human who will resume:
- what you accomplished so far
- what still remains
- the single most useful next step

IMPORTANT:
- Do NOT call any tools. All tool calls are disabled and will be rejected.
  Tool definitions are visible only for technical reasons (prompt cache).
- Respond ONLY with text, grounded in the work already done this turn.
- Be concise (a few sentences or short bullets) and do not start new work."""


# ---------------------------------------------------------------------------
# DenyAllToolset: advertises tools (cache match) but rejects every call
# ---------------------------------------------------------------------------


class _DenyAllToolset:
    """A toolset that exposes the same tool definitions as the agent (for prompt
    cache matching) but rejects every tool call with an error message."""

    def __init__(self, source_tools: list[Tool]) -> None:
        self._tools = source_tools

    @property
    def tools(self) -> list[Tool]:
        return self._tools

    def handle(self, tool_call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call.id,
            return_value=ToolError(
                message="Tool calls are disabled for side questions. Answer with text only.",
                brief="denied",
            ),
        )


# ---------------------------------------------------------------------------
# Context construction
# ---------------------------------------------------------------------------


def _build_btw_context(
    soul: PythinkerSoul,
    question: str,
    *,
    system_reminder_text: str = SIDE_QUESTION_SYSTEM_REMINDER,
) -> tuple[str, list[Message], _DenyAllToolset]:
    """Build (system_prompt, history, toolset) aligned with the main agent.

    Uses the same system_prompt, normalize_history(), and tool definitions
    as ``PythinkerSoul._step`` so the LLM provider can reuse the prompt cache.
    ``system_reminder_text`` selects the framing (side question vs. max-steps
    handoff); both run tools-denied over the current history.
    """
    system_prompt = soul._agent.system_prompt  # pyright: ignore[reportPrivateUsage]
    effective_history = normalize_history(soul.context.history)

    wrapped = f"{system_reminder(system_reminder_text).text}\n\n{question}"
    side_message = Message(role="user", content=wrapped)

    toolset = _DenyAllToolset(soul._agent.toolset.tools)  # pyright: ignore[reportPrivateUsage]

    return system_prompt, [*effective_history, side_message], toolset


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def execute_side_question(
    soul: PythinkerSoul,
    question: str,
    on_text_chunk: Callable[[str], None] | None = None,
    *,
    system_reminder_text: str = SIDE_QUESTION_SYSTEM_REMINDER,
) -> tuple[str | None, str | None]:
    """Execute a side question and return (response, error).

    Runs up to ``_BTW_MAX_TURNS`` steps.  On the first step, if the LLM
    returns a tool call instead of text, the denied tool result is appended
    to the history and a second step gives the LLM another chance.

    Args:
        soul: The PythinkerSoul instance (for context and chat_provider access).
        question: The user's side question.
        on_text_chunk: Optional callback for streaming text chunks.

    Returns:
        (response_text, None) on success, (None, error_message) on failure.
    """
    if soul._runtime.llm is None:  # pyright: ignore[reportPrivateUsage]
        return None, "LLM is not set."

    try:
        chat_provider = soul._runtime.llm.chat_provider  # pyright: ignore[reportPrivateUsage]
        if system_reminder_text == SIDE_QUESTION_SYSTEM_REMINDER and inspect.iscoroutinefunction(
            soul.assemble_side_request
        ):
            assembled = await soul.assemble_side_request(question, system_reminder_text)
            system_prompt = assembled.system_prompt
            history = list(assembled.provider_history)
            toolset = _DenyAllToolset(
                soul._agent.toolset.tools  # pyright: ignore[reportPrivateUsage]
            )
        else:
            system_prompt, history, toolset = _build_btw_context(
                soul, question, system_reminder_text=system_reminder_text
            )

        text_chunks: list[str] = []

        def _on_part(part: StreamedMessagePart) -> None:
            if isinstance(part, TextPart) and part.text:
                text_chunks.append(part.text)
                if on_text_chunk is not None:
                    on_text_chunk(part.text)

        # Multi-turn loop: give the LLM a second chance if it calls tools
        for turn in range(_BTW_MAX_TURNS):
            result = await pythinker_core.step(
                chat_provider,
                system_prompt,
                toolset,
                history,
                on_message_part=_on_part,
            )

            # Check for text response — but only accept it if the LLM
            # didn't also call tools (mixed text+tool = incomplete preamble).
            response_text = "".join(text_chunks).strip()
            if response_text and not result.tool_calls:
                return response_text, None

            # No text — did the LLM try to call a tool?
            tool_results = await result.tool_results()
            if not result.tool_calls:
                break  # No text, no tool calls — give up

            # Tool calls were denied. If we have turns left, feed the error
            # back so the LLM can try again with text.
            if turn + 1 < _BTW_MAX_TURNS:
                # Build the next history: original + assistant message + tool error results
                history = [
                    *history,
                    result.message,
                    *[_tool_result_to_message(tr) for tr in tool_results],
                ]
                text_chunks.clear()  # Reset for next turn
                continue

            # Last turn and still no text — report the tool call attempt
            tool_names = [tc.function.name for tc in result.tool_calls]
            return None, (
                f"Side question tried to call tools ({', '.join(tool_names)}) "
                "instead of answering directly. Try rephrasing or ask in the main conversation."
            )

        return None, "No response received."
    except Exception as e:
        from pythinker_code.telemetry.errors import report_handled_error

        report_handled_error(e, site="soul.btw.execute")
        logger.warning("Side question failed: {error}", error=e)
        return None, str(e)


async def generate_max_steps_handoff(soul: PythinkerSoul) -> str | None:
    """Produce a brief, tools-disabled progress/handoff summary after the step
    ceiling is hit, so the human who resumes need not reconstruct state.

    Reuses the side-question (tools-denied, bounded) mechanism, so the summary
    turn cannot itself re-hit the step ceiling or mutate the workspace, and it is
    not written to the main context. Returns the summary text, or ``None`` if it
    could not be produced (the caller should fall back to the static line).
    """
    response, error = await execute_side_question(
        soul,
        "Provide your handoff summary now.",
        system_reminder_text=MAX_STEPS_HANDOFF_REMINDER,
    )
    if error:
        logger.warning("Max-steps handoff summary unavailable: {error}", error=error)
    return response


def _tool_result_to_message(tool_result: ToolResult) -> Message:
    """Convert a ToolResult to a tool-result Message for history."""
    content = tool_result.return_value.message or "Tool call denied."
    return Message(
        role="tool",
        content=content,
        tool_call_id=tool_result.tool_call_id,
    )


# ---------------------------------------------------------------------------
# Wire-based entry point (for web UI / non-interactive)
# ---------------------------------------------------------------------------


async def run_side_question(soul: PythinkerSoul, question: str) -> None:
    """Execute a side question via wire events."""
    if soul._runtime.llm is None:  # pyright: ignore[reportPrivateUsage]
        raise LLMNotSet()

    btw_id = uuid.uuid4().hex[:12]
    wire_send(BtwBegin(id=btw_id, question=question))

    try:
        response, error = await execute_side_question(soul, question)
        if response:
            wire_send(BtwEnd(id=btw_id, response=response))
        else:
            wire_send(BtwEnd(id=btw_id, error=error or "No response received."))
    except Exception as e:
        from pythinker_code.telemetry.errors import report_handled_error

        report_handled_error(e, site="soul.btw.run_wire")
        logger.warning("Side question failed: {error}", error=e)
        wire_send(BtwEnd(id=btw_id, error=str(e)))
