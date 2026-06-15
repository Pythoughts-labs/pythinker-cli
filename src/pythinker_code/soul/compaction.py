from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable

import pythinker_core
from pythinker_core.chat_provider import TokenUsage
from pythinker_core.message import Message
from pythinker_core.tooling.empty import EmptyToolset

import pythinker_code.prompts as prompts
from pythinker_code.llm import LLM
from pythinker_code.soul.api_errors import is_context_overflow_error
from pythinker_code.soul.message import system
from pythinker_code.utils.logging import logger
from pythinker_code.wire.types import ContentPart, TextPart, ThinkPart


class CompactionResult(NamedTuple):
    messages: Sequence[Message]
    usage: TokenUsage | None

    @property
    def estimated_token_count(self) -> int:
        """Estimate the token count of the compacted messages.

        When LLM usage is available, ``usage.output`` gives the exact token count
        of the generated summary (the first message).  Preserved messages (all
        subsequent messages) are estimated from their text length.

        When usage is not available (no compaction LLM call was made), all
        messages are estimated from text length.

        The estimate is intentionally conservative — it will be replaced by the
        real value on the next LLM call.
        """
        if self.usage is not None and len(self.messages) > 0:
            summary_tokens = self.usage.output
            preserved_tokens = estimate_text_tokens(self.messages[1:])
            return summary_tokens + preserved_tokens

        return estimate_text_tokens(self.messages)


def estimate_text_tokens(messages: Sequence[Message]) -> int:
    """Estimate tokens from message text content using a character-based heuristic."""
    total_chars = 0
    for msg in messages:
        for part in msg.content:
            if isinstance(part, TextPart):
                total_chars += len(part.text)
    # ~4 chars per token for English; somewhat underestimates for CJK text,
    # but this is a temporary estimate that gets corrected on the next LLM call.
    return total_chars // 4


def should_auto_compact(
    token_count: int,
    max_context_size: int,
    *,
    trigger_ratio: float,
    reserved_context_size: int,
) -> bool:
    """Determine whether auto-compaction should be triggered.

    Returns True when either condition is met (whichever fires first):
    - Ratio-based: token_count >= max_context_size * trigger_ratio
    - Reserved-based: token_count + reserved_context_size >= max_context_size
    """
    return (
        token_count >= max_context_size * trigger_ratio
        or token_count + reserved_context_size >= max_context_size
    )


def should_prune(token_count: int, max_context_size: int, *, ratio: float) -> bool:
    """Whether the cheap stale-tool-output prune tier should run.

    Fires at a *lower* threshold than full compaction so large completed tool
    outputs can be elided before paying for an LLM summary. Set ``ratio`` at or
    above ``compaction_trigger_ratio`` to disable the tier (compaction fires
    first).
    """
    return token_count >= max_context_size * ratio


PRUNE_PLACEHOLDER = "[tool output elided to save context: {n} chars]"
CAP_PLACEHOLDER = "\n[tool output capped: removed {n} chars]"


def prune_stale_tool_outputs(
    messages: Sequence[Message], *, protect_last: int, min_chars: int
) -> tuple[list[Message], int]:
    """Replace large completed tool-result bodies in deep history with a short
    placeholder — a fidelity-preserving step before LLM summarization.

    Only ``tool``-role messages older than the last ``protect_last`` messages and
    whose text body exceeds ``min_chars`` are pruned. Message order, roles, and
    ``tool_call_id`` pairing are preserved (nothing is dropped), so the
    conversational structure stays valid. Returns the rewritten message list and
    the number of characters freed.
    """
    cutoff = max(0, len(messages) - protect_last)
    pruned: list[Message] = []
    freed = 0
    for index, msg in enumerate(messages):
        if index >= cutoff or msg.role != "tool":
            pruned.append(msg)
            continue
        body = msg.extract_text("")
        if len(body) <= min_chars:
            pruned.append(msg)
            continue
        freed += len(body)
        placeholder = TextPart(text=PRUNE_PLACEHOLDER.format(n=len(body)))
        pruned.append(msg.model_copy(update={"content": [placeholder]}))
    return pruned, freed


def _cap_text_to_budget(body: str, max_chars: int) -> str:
    removed = len(body) - max_chars
    while True:
        suffix = CAP_PLACEHOLDER.format(n=removed)
        if len(suffix) >= max_chars:
            return suffix[:max_chars]
        prefix_chars = max_chars - len(suffix)
        capped = body[:prefix_chars] + suffix
        new_removed = len(body) - len(capped)
        if new_removed == removed:
            return capped
        removed = new_removed


def cap_stale_tool_result_bodies(
    messages: Sequence[Message], *, protect_last: int, max_chars: int
) -> tuple[list[Message], int]:
    """Cap old tool-result text bodies without dropping messages or tool IDs."""
    if max_chars <= 0:
        return list(messages), 0

    cutoff = max(0, len(messages) - protect_last)
    capped_messages: list[Message] = []
    freed = 0
    for index, msg in enumerate(messages):
        if index >= cutoff or msg.role != "tool":
            capped_messages.append(msg)
            continue
        body = msg.extract_text("")
        if len(body) <= max_chars:
            capped_messages.append(msg)
            continue
        capped_body = _cap_text_to_budget(body, max_chars)
        freed += len(body) - len(capped_body)
        capped_messages.append(msg.model_copy(update={"content": [TextPart(text=capped_body)]}))
    return capped_messages, freed


@runtime_checkable
class Compaction(Protocol):
    async def compact(
        self, messages: Sequence[Message], llm: LLM, *, custom_instruction: str = ""
    ) -> CompactionResult:
        """
        Compact a sequence of messages into a new sequence of messages.

        Args:
            messages (Sequence[Message]): The messages to compact.
            llm (LLM): The LLM to use for compaction.
            custom_instruction: Optional user instruction to guide compaction focus.

        Returns:
            CompactionResult: The compacted messages and token usage from the compaction LLM call.

        Raises:
            ChatProviderError: When the chat provider returns an error.
        """
        ...


if TYPE_CHECKING:

    def type_check(simple: SimpleCompaction):
        _: Compaction = simple


class SimpleCompaction:
    def __init__(self, max_preserved_messages: int = 2, base_prompt: str | None = None) -> None:
        self.max_preserved_messages = max_preserved_messages
        # None -> the built-in prompts.COMPACT; set from config.compact_prompt.
        self.base_prompt = base_prompt

    async def compact(
        self, messages: Sequence[Message], llm: LLM, *, custom_instruction: str = ""
    ) -> CompactionResult:
        prepared = self.prepare(messages, custom_instruction=custom_instruction)
        compact_message, to_preserve = prepared.compact_message, prepared.to_preserve
        if compact_message is None:
            return CompactionResult(messages=list(to_preserve), usage=None)

        logger.debug("Compacting context...")
        summary_message, usage = await self._summarize_to_message(
            list(prepared.to_compact), compact_message, llm, custom_instruction
        )
        if summary_message is None:
            note = Message(
                role="user",
                content=[
                    system(
                        "Previous context exceeded the model's window and was "
                        "dropped without summarization. Re-read files or re-run "
                        "commands if earlier results are needed."
                    )
                ],
            )
            return CompactionResult(messages=[note, *to_preserve], usage=None)
        if usage:
            logger.debug(
                "Compaction used {input} input tokens and {output} output tokens",
                input=usage.input,
                output=usage.output,
            )

        content: list[ContentPart] = [
            system("Previous context has been compacted. Here is the compaction output:")
        ]
        # drop thinking parts if any
        content.extend(part for part in summary_message.content if not isinstance(part, ThinkPart))
        compacted_messages: list[Message] = [Message(role="user", content=content)]
        compacted_messages.extend(to_preserve)
        return CompactionResult(messages=compacted_messages, usage=usage)

    async def summarize_all(
        self, messages: Sequence[Message], llm: LLM, *, custom_instruction: str = ""
    ) -> str | None:
        """Summarize *messages* to plain text with no preserved tail.

        For boundaries where raw history must not cross — e.g. carrying a
        conversation to a different model, whose provider may reject the
        outgoing model's thinking blocks or tool-call schemas — only text
        survives. Returns ``None`` when there is nothing to summarize or
        nothing fits the context window.
        """
        to_compact = list(messages)
        if not to_compact:
            return None
        compact_message = self._build_compact_message(
            to_compact, custom_instruction=custom_instruction
        )
        summary_message, _usage = await self._summarize_to_message(
            to_compact, compact_message, llm, custom_instruction
        )
        if summary_message is None:
            return None
        text = "\n".join(
            part.text for part in summary_message.content if isinstance(part, TextPart)
        ).strip()
        return text or None

    async def _summarize_to_message(
        self,
        to_compact: list[Message],
        compact_message: Message,
        llm: LLM,
        custom_instruction: str,
    ) -> tuple[Message | None, TokenUsage | None]:
        """Run the summarization request, halving the slice on context overflow.

        The request carries the whole to-compact slice, so it can itself
        exceed the context window. On a context-length rejection the oldest
        half is dropped and the request retried; ``(None, None)`` means even
        a single message did not fit.

        NOTE: the summary length is bounded by the chat provider's
        construction-time max output tokens (LLM default_max_tokens, or
        PYTHINKER_MODEL_MAX_TOKENS). A tighter per-call cap would require a
        max-tokens parameter on ``ChatProvider.generate`` (and
        ``pythinker_core.step``), which neither exposes today.
        """
        while True:
            try:
                result = await pythinker_core.step(
                    chat_provider=llm.chat_provider,
                    system_prompt="You are a helpful assistant that compacts conversation context.",
                    toolset=EmptyToolset(),
                    history=[compact_message],
                )
                return result.message, result.usage
            except Exception as e:
                if not is_context_overflow_error(e):
                    raise
                if len(to_compact) <= 1:
                    logger.warning(
                        "Compaction request still exceeds the context window with a "
                        "single message; dropping unsummarized older context"
                    )
                    return None, None
                dropped = len(to_compact) // 2
                to_compact = to_compact[dropped:]
                logger.warning(
                    "Compaction request exceeded the context window; retrying with the "
                    "newest {kept} of the slice ({dropped} oldest dropped)",
                    kept=len(to_compact),
                    dropped=dropped,
                )
                compact_message = self._build_compact_message(
                    to_compact, custom_instruction=custom_instruction
                )

    class PrepareResult(NamedTuple):
        compact_message: Message | None
        to_preserve: Sequence[Message]
        to_compact: Sequence[Message] = ()

    def prepare(
        self, messages: Sequence[Message], *, custom_instruction: str = ""
    ) -> PrepareResult:
        if not messages or self.max_preserved_messages <= 0:
            return self.PrepareResult(compact_message=None, to_preserve=messages)

        history = list(messages)
        preserve_start_index = len(history)
        n_preserved = 0
        for index in range(len(history) - 1, -1, -1):
            if history[index].role in {"user", "assistant"}:
                n_preserved += 1
                if n_preserved == self.max_preserved_messages:
                    preserve_start_index = index
                    break

        if n_preserved < self.max_preserved_messages:
            return self.PrepareResult(compact_message=None, to_preserve=messages)

        to_compact = history[:preserve_start_index]
        to_preserve = history[preserve_start_index:]

        if not to_compact:
            # Let's hope this won't exceed the context size limit
            return self.PrepareResult(compact_message=None, to_preserve=to_preserve)

        compact_message = self._build_compact_message(
            to_compact, custom_instruction=custom_instruction
        )
        return self.PrepareResult(
            compact_message=compact_message,
            to_preserve=to_preserve,
            to_compact=to_compact,
        )

    def _build_compact_message(
        self, to_compact: Sequence[Message], *, custom_instruction: str = ""
    ) -> Message:
        compact_message = Message(role="user", content=[])
        for i, msg in enumerate(to_compact):
            compact_message.content.append(
                TextPart(text=f"## Message {i + 1}\nRole: {msg.role}\nContent:\n")
            )
            compact_message.content.extend(
                part for part in msg.content if isinstance(part, TextPart)
            )
        prompt_text = "\n" + (self.base_prompt or prompts.COMPACT)
        if custom_instruction:
            prompt_text += (
                "\n\n**User's Custom Compaction Instruction:**\n"
                "The user has specifically requested the following focus during compaction. "
                "You MUST prioritize this instruction above the default compression priorities:\n"
                f"{custom_instruction}"
            )
        compact_message.content.append(TextPart(text=prompt_text))
        return compact_message
