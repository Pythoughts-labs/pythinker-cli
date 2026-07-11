from __future__ import annotations

from collections.abc import Sequence

from pythinker_core.message import Message

from pythinker_code.notifications import is_notification_message


def estimate_injection_tokens(text: str) -> int:
    """Estimate request-fragment tokens using the project-wide len/4 heuristic."""
    return max(1, len(text) // 4)


def normalize_history(history: Sequence[Message]) -> list[Message]:
    """Merge adjacent non-notification user messages without altering other roles."""
    normalized: list[Message] = []
    for message in history:
        if _can_merge_user_message(normalized, message):
            previous = normalized[-1]
            normalized[-1] = Message(role="user", content=[*previous.content, *message.content])
        else:
            normalized.append(message)
    return normalized


def _can_merge_user_message(normalized: Sequence[Message], message: Message) -> bool:
    return bool(
        normalized
        and normalized[-1].role == "user"
        and message.role == "user"
        and not is_notification_message(normalized[-1])
        and not is_notification_message(message)
    )
