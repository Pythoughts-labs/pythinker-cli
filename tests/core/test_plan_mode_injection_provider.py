"""Tests for PlanModeInjectionProvider.get_injections() flow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, PropertyMock

from pythinker_core.message import Message, TextPart

from pythinker_code.soul.dynamic_injections.plan_mode import (
    PlanModeInjectionProvider,
    _full_reminder,
    _reentry_reminder,
    _sparse_reminder,
)


def _make_soul_mock(
    plan_mode: bool = True,
    plan_path: Path | None = None,
    consume_pending: bool = False,
    is_subagent: bool = False,
) -> MagicMock:
    soul = MagicMock()
    type(soul).plan_mode = PropertyMock(return_value=plan_mode)
    type(soul).is_subagent = PropertyMock(return_value=is_subagent)
    soul.get_plan_file_path.return_value = plan_path
    soul.consume_pending_plan_activation_injection.return_value = consume_pending
    return soul


def _reminder_msg() -> Message:
    """Create a user message that looks like a plan mode reminder."""
    return Message(
        role="user",
        content=[TextPart(text=_full_reminder("/tmp/plan.md", False))],
    )


def _assistant_msg() -> Message:
    return Message(role="assistant", content=[TextPart(text="step")])


class TestPlanModeInjectionProvider:
    async def test_returns_empty_when_inactive(self) -> None:
        provider = PlanModeInjectionProvider()
        provider._inject_count = 5
        soul = _make_soul_mock(plan_mode=False)

        result = await provider.get_injections([], soul)

        assert result == []
        assert provider._inject_count == 0

    async def test_first_call_injects_full_reminder(self) -> None:
        provider = PlanModeInjectionProvider()
        soul = _make_soul_mock(plan_mode=True, plan_path=Path("/tmp/plan.md"))

        result = await provider.get_injections([], soul)

        assert len(result) == 1
        assert result[0].type == "plan_mode"
        assert "Plan mode is active" in result[0].content
        assert provider._inject_count == 1

    async def test_throttled_before_interval(self) -> None:
        provider = PlanModeInjectionProvider()
        soul = _make_soul_mock(plan_mode=True, plan_path=Path("/tmp/plan.md"))

        # History: reminder + 3 assistant turns (< 5 threshold)
        history = [_reminder_msg()] + [_assistant_msg() for _ in range(3)]

        result = await provider.get_injections(history, soul)
        assert result == []

    async def test_injects_after_interval_reached(self) -> None:
        provider = PlanModeInjectionProvider()
        soul = _make_soul_mock(plan_mode=True, plan_path=Path("/tmp/plan.md"))

        # History: reminder + 5 assistant turns (= threshold)
        history = [_reminder_msg()] + [_assistant_msg() for _ in range(5)]

        result = await provider.get_injections(history, soul)
        assert len(result) == 1

    async def test_sparse_on_non_full_cycle(self) -> None:
        provider = PlanModeInjectionProvider()
        # _inject_count=1 → after increment becomes 2 → 2 % 5 != 1 → sparse
        provider._inject_count = 1
        soul = _make_soul_mock(plan_mode=True, plan_path=Path("/tmp/plan.md"))

        history = [_reminder_msg()] + [_assistant_msg() for _ in range(5)]

        result = await provider.get_injections(history, soul)
        assert len(result) == 1
        assert "still active" in result[0].content

    async def test_full_on_every_5th_cycle(self) -> None:
        provider = PlanModeInjectionProvider()
        # _inject_count=5 → after increment becomes 6 → 6 % 5 == 1 → full
        provider._inject_count = 5
        soul = _make_soul_mock(plan_mode=True, plan_path=Path("/tmp/plan.md"))

        history = [_reminder_msg()] + [_assistant_msg() for _ in range(5)]

        result = await provider.get_injections(history, soul)
        assert len(result) == 1
        assert "Plan mode is active" in result[0].content

    async def test_pending_activation_returns_full(self, tmp_path: Path) -> None:
        provider = PlanModeInjectionProvider()
        # Use a path that is guaranteed not to exist so this hits the
        # plan-absent (full reminder) branch rather than the reentry branch —
        # the provider checks ``plan_path.exists()``, so a hardcoded /tmp path
        # makes the test non-hermetic.
        plan_path = tmp_path / "nonexistent-plan.md"
        soul = _make_soul_mock(plan_mode=True, plan_path=plan_path, consume_pending=True)

        result = await provider.get_injections([], soul)
        assert len(result) == 1
        assert result[0].type == "plan_mode"
        assert "Plan mode is active" in result[0].content
        assert provider._inject_count == 1

    async def test_pending_activation_with_plan_returns_reentry(self, tmp_path: Path) -> None:
        provider = PlanModeInjectionProvider()
        plan_path = tmp_path / "existing-plan.md"
        plan_path.write_text("# Existing plan", encoding="utf-8")
        soul = _make_soul_mock(plan_mode=True, plan_path=plan_path, consume_pending=True)

        result = await provider.get_injections([], soul)
        assert len(result) == 1
        assert result[0].type == "plan_mode_reentry"
        assert "Re-entering Plan Mode" in result[0].content

    async def test_resets_count_when_deactivated(self) -> None:
        provider = PlanModeInjectionProvider()
        provider._inject_count = 10
        soul = _make_soul_mock(plan_mode=False)

        await provider.get_injections([], soul)
        assert provider._inject_count == 0

    async def test_subagent_receives_no_plan_mode_injection(self) -> None:
        # Subagents share the session's plan_mode flag (for persistence/resume),
        # but their YAML usually excludes EnterPlanMode/ExitPlanMode. Injecting the
        # plan-mode workflow reminder would only invite hallucinated tool calls, so
        # the provider must suppress it for subagents even while plan mode is active.
        provider = PlanModeInjectionProvider()
        soul = _make_soul_mock(
            plan_mode=True,
            plan_path=Path("/tmp/plan.md"),
            is_subagent=True,
        )

        result = await provider.get_injections([], soul)

        assert result == []
        assert provider._inject_count == 0


class TestPlanModeVerificationClause:
    """planning-1 backfill: lock the mandatory Verification-section requirement
    into every plan-mode reminder variant so it cannot silently drift out of the
    authoring instructions the human reviews."""

    def test_full_reminder_requires_verification_section(self) -> None:
        text = _full_reminder("/tmp/plan.md", False)
        assert "Verification section" in text
        # It must be part of the plan-authoring workflow step, not an aside.
        assert "The plan MUST include a Verification section" in text

    def test_sparse_reminder_requires_verification_section(self) -> None:
        assert "Verification section" in _sparse_reminder("/tmp/plan.md")

    def test_reentry_reminder_requires_verification_section(self) -> None:
        assert "Verification section" in _reentry_reminder("/tmp/plan.md")


class TestPlanModeDecisionCompleteness:
    """Lock the unknowns-resolution protocol, the decision-complete exit bar,
    and the plan-file shape rubric into the plan-mode reminders so they cannot
    silently drift out of the authoring instructions."""

    def test_full_reminder_distinguishes_unknown_kinds(self) -> None:
        text = _full_reminder("/tmp/plan.md", False)
        assert "Repo-discoverable facts" in text
        assert "never ask the user" in text
        assert "AskUserQuestion" in text
        assert "recommended default" in text

    def test_full_reminder_records_defaults_as_assumptions(self) -> None:
        assert "Assumptions" in _full_reminder("/tmp/plan.md", False)

    def test_full_reminder_requires_decision_complete_exit(self) -> None:
        text = _full_reminder("/tmp/plan.md", False)
        assert "decision-complete" in text
        assert "Figure out" in text

    def test_full_reminder_includes_plan_shape_rubric(self) -> None:
        text = _full_reminder("/tmp/plan.md", False)
        assert "skimmable" in text
        assert "3-5 short sections" in text

    def test_sparse_reminder_mentions_decision_complete_and_assumptions(self) -> None:
        text = _sparse_reminder("/tmp/plan.md")
        assert "decision-complete" in text
        assert "Assumptions" in text

    def test_reentry_reminder_requires_decision_complete_exit(self) -> None:
        assert "decision-complete" in _reentry_reminder("/tmp/plan.md")
