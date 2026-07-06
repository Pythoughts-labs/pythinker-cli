from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from pythinker_core.message import Message, TextPart

from pythinker_code.notifications import is_notification_message
from pythinker_code.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider
from pythinker_code.soul.message import is_system_reminder_message
from pythinker_code.utils.logging import logger

if TYPE_CHECKING:
    from pythinker_code.soul.pythinkersoul import PythinkerSoul

_ACTIVE_SKILLS_INJECTION_TYPE = "active_skills"
_REMINDER_MARKER = "Active skill reminder:"
_CLEAR_ALL_PHRASES = (
    "normal mode",
    "clear active skills",
    "clear skill mode",
    "stop skill mode",
    "disable active skills",
)
_DEACTIVATE_VERBS = r"(?:stop|disable|deactivate|turn\s+off)"


class ActiveSkillInjectionProvider(DynamicInjectionProvider):
    """Keep explicitly activated skills visible without reloading full skill bodies."""

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        active_skills = list(soul.runtime.session.state.active_skills)
        if not active_skills:
            return []

        latest_index = _latest_real_user_index(history)
        latest_text = _message_text(history[latest_index]) if latest_index is not None else ""
        active_skills, deactivated = _apply_deactivation_intent(latest_text, active_skills, soul)
        if deactivated:
            return []
        if not active_skills:
            return []

        if latest_index is not None and _has_active_skill_reminder_after(history, latest_index):
            return []

        return [
            DynamicInjection(
                type=_ACTIVE_SKILLS_INJECTION_TYPE,
                content=active_skill_reminder(active_skills),
            )
        ]


def active_skill_reminder(active_skills: Sequence[str]) -> str:
    names = ", ".join(f"`{name}`" for name in active_skills)
    return (
        f"{_REMINDER_MARKER} explicitly activated skills remain in effect: {names}. "
        "Continue applying their loaded instructions. Do not reload a skill unless "
        "you need its exact details. If the user asks to stop or disable a named "
        "active skill, or says normal mode, treat that as deactivation intent."
    )


def _apply_deactivation_intent(
    text: str,
    active_skills: list[str],
    soul: PythinkerSoul,
) -> tuple[list[str], bool]:
    if not text:
        return active_skills, False

    folded = text.casefold()
    if any(phrase in folded for phrase in _CLEAR_ALL_PHRASES):
        remaining: list[str] = []
    else:
        deactivated = {
            skill.casefold() for skill in active_skills if _asks_to_deactivate_skill(folded, skill)
        }
        if not deactivated:
            return active_skills, False
        remaining = [skill for skill in active_skills if skill.casefold() not in deactivated]

    soul.runtime.session.state.active_skills = remaining
    try:
        soul.runtime.session.save_state()
    except OSError:
        logger.warning("Failed to persist active skill deactivation", exc_info=True)
        raise
    return remaining, True


def _asks_to_deactivate_skill(folded_text: str, skill_name: str) -> bool:
    escaped_name = re.escape(skill_name.casefold())
    return bool(re.search(rf"\b{_DEACTIVATE_VERBS}\s+(?:using\s+)?{escaped_name}\b", folded_text))


def _latest_real_user_index(history: Sequence[Message]) -> int | None:
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        if message.role != "user":
            continue
        if is_notification_message(message) or is_system_reminder_message(message):
            continue
        if _message_text(message).strip():
            return index
    return None


def _message_text(message: Message) -> str:
    return " ".join(part.text for part in message.content if isinstance(part, TextPart))


def _has_active_skill_reminder_after(history: Sequence[Message], index: int) -> bool:
    for message in history[index + 1 :]:
        if message.role != "user":
            continue
        for part in message.content:
            if isinstance(part, TextPart) and _REMINDER_MARKER in part.text:
                return True
    return False
