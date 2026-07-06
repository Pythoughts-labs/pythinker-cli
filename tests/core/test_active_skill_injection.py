"""Tests for active skill reminder injection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from pythinker_core.message import Message, TextPart

from pythinker_code.soul.dynamic_injections.active_skills import (
    _ACTIVE_SKILLS_INJECTION_TYPE,
    ActiveSkillInjectionProvider,
    active_skill_reminder,
)


def _user(text: str) -> Message:
    return Message(role="user", content=[TextPart(text=text)])


def _reminder(text: str) -> Message:
    return Message(
        role="user", content=[TextPart(text=f"<system-reminder>\n{text}\n</system-reminder>")]
    )


def _soul(active_skills: list[str]) -> MagicMock:
    session = SimpleNamespace(
        state=SimpleNamespace(active_skills=active_skills), save_state=lambda: None
    )
    runtime = SimpleNamespace(session=session)
    soul = MagicMock()
    soul.runtime = runtime
    return soul


def _soul_with_save_error(active_skills: list[str]) -> MagicMock:
    def save_state() -> None:
        raise OSError("read-only session")

    session = SimpleNamespace(
        state=SimpleNamespace(active_skills=active_skills), save_state=save_state
    )
    runtime = SimpleNamespace(session=session)
    soul = MagicMock()
    soul.runtime = runtime
    return soul


async def test_active_skill_injects_compact_reminder() -> None:
    provider = ActiveSkillInjectionProvider()
    result = await provider.get_injections([_user("implement the fix")], _soul(["ponytail"]))
    assert len(result) == 1
    assert result[0].type == _ACTIVE_SKILLS_INJECTION_TYPE
    assert "Active skill reminder:" in result[0].content
    assert "ponytail" in result[0].content
    assert "SKILL.md" not in result[0].content


async def test_active_skill_reminder_is_not_repeated_for_same_user_message() -> None:
    provider = ActiveSkillInjectionProvider()
    history = [
        _user("implement the fix"),
        _reminder(active_skill_reminder(["ponytail"])),
        Message(role="assistant", content=[TextPart(text="I will inspect it.")]),
    ]
    result = await provider.get_injections(history, _soul(["ponytail"]))
    assert result == []


async def test_new_user_message_rearms_active_skill_reminder() -> None:
    provider = ActiveSkillInjectionProvider()
    history = [
        _user("implement the fix"),
        _reminder(active_skill_reminder(["ponytail"])),
        Message(role="assistant", content=[TextPart(text="done")]),
        _user("now adjust the tests"),
    ]
    result = await provider.get_injections(history, _soul(["ponytail"]))
    assert len(result) == 1


async def test_stop_named_skill_deactivates_only_that_skill() -> None:
    provider = ActiveSkillInjectionProvider()
    soul = _soul(["ponytail", "test-driven-development"])
    result = await provider.get_injections([_user("stop ponytail, keep going")], soul)
    assert result == []
    active = cast(Any, soul.runtime).session.state.active_skills
    assert active == ["test-driven-development"]


async def test_stop_word_without_named_skill_command_keeps_skill_active() -> None:
    provider = ActiveSkillInjectionProvider()
    soul = _soul(["ponytail"])
    result = await provider.get_injections(
        [_user("stop overcomplicating the fix, keep ponytail active")], soul
    )
    assert len(result) == 1
    active = cast(Any, soul.runtime).session.state.active_skills
    assert active == ["ponytail"]


async def test_normal_mode_deactivates_all_active_skills() -> None:
    provider = ActiveSkillInjectionProvider()
    soul = _soul(["ponytail", "test-driven-development"])
    result = await provider.get_injections([_user("normal mode now")], soul)
    assert result == []
    active = cast(Any, soul.runtime).session.state.active_skills
    assert active == []


async def test_deactivation_persistence_failure_is_visible() -> None:
    provider = ActiveSkillInjectionProvider()

    with pytest.raises(OSError, match="read-only session"):
        await provider.get_injections([_user("stop ponytail")], _soul_with_save_error(["ponytail"]))
