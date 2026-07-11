"""Tests for Runtime approval state restoration."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import pythinker_code.soul.agent as agent_module
from pythinker_code.auth.oauth import OAuthManager
from pythinker_code.skill import SkillCatalog
from pythinker_code.soul.agent import Runtime
from pythinker_code.wire.types import ToolCall


def _shell_call(cmd: str) -> ToolCall:
    return ToolCall(
        id="call-1",
        function=ToolCall.FunctionBody(name="Shell", arguments=json.dumps({"command": cmd})),
    )


@pytest.fixture
def lightweight_runtime_create(monkeypatch: pytest.MonkeyPatch, environment) -> None:
    monkeypatch.setattr(agent_module, "list_directory", AsyncMock(return_value=""))
    monkeypatch.setattr(agent_module, "load_agents_md", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_module.Environment, "detect", AsyncMock(return_value=environment))
    monkeypatch.setattr(
        agent_module,
        "discover_runtime_skill_catalog",
        AsyncMock(return_value=(SkillCatalog({}, ()), [])),
    )


@pytest.mark.asyncio
async def test_runtime_create_restores_persisted_auto(
    config,
    session,
    lightweight_runtime_create,
) -> None:
    session.state.approval.auto = True

    runtime = await Runtime.create(
        config,
        OAuthManager(config),
        llm=None,
        session=session,
        yolo=False,
    )

    assert runtime.approval.is_auto() is True
    assert runtime.approval.is_auto_flag() is True


@pytest.mark.asyncio
async def test_explicit_auto_persists_to_session_state(
    config,
    session,
    lightweight_runtime_create,
) -> None:
    runtime = await Runtime.create(
        config,
        OAuthManager(config),
        llm=None,
        session=session,
        yolo=False,
        auto=True,
    )

    assert runtime.approval.is_auto() is True
    assert runtime.approval.is_auto_flag() is True
    assert session.state.approval.auto is True


@pytest.mark.asyncio
async def test_runtime_auto_overlay_does_not_persist_to_session_state(
    config,
    session,
    lightweight_runtime_create,
) -> None:
    runtime = await Runtime.create(
        config,
        OAuthManager(config),
        llm=None,
        session=session,
        yolo=False,
        runtime_auto=True,
    )

    assert runtime.approval.is_auto() is True
    assert runtime.approval.is_auto_flag() is False

    runtime.approval.set_yolo(True)

    assert session.state.approval.yolo is True
    assert session.state.approval.auto is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("auto", "runtime_auto"), [(True, False), (False, True)])
async def test_unattended_runtime_in_default_safe_mode_denies_without_waiting(
    config,
    session,
    lightweight_runtime_create,
    auto: bool,
    runtime_auto: bool,
) -> None:
    from tests.conftest import tool_call_context

    runtime = await Runtime.create(
        config,
        OAuthManager(config),
        llm=None,
        session=session,
        yolo=False,
        auto=auto,
        runtime_auto=runtime_auto,
    )

    assert runtime.approval.is_auto() is True
    assert runtime.approval.is_auto_approve() is False
    with tool_call_context("Shell", arguments={"command": "echo hello"}):
        result = await asyncio.wait_for(
            runtime.approval.request("Shell", "run command", "Run command `echo hello`"),
            timeout=0.1,
        )

    assert not result
    assert runtime.approval.runtime.list_pending() == []
    assert "safe mode prevents auto-approval" in result.rejection_error().message


@pytest.mark.asyncio
async def test_runtime_create_enables_destructive_deliberation_from_config(
    config,
    session,
    lightweight_runtime_create,
) -> None:
    config.auto_deliberate_destructive_actions = True

    runtime = await Runtime.create(
        config,
        OAuthManager(config),
        llm=None,
        session=session,
        yolo=True,
    )

    assert runtime.approval.deliberation_gate(_shell_call("rm -rf build")) is not None


@pytest.mark.asyncio
async def test_runtime_set_auto_persists_to_session_state(
    config,
    session,
    lightweight_runtime_create,
) -> None:
    runtime = await Runtime.create(
        config,
        OAuthManager(config),
        llm=None,
        session=session,
        yolo=False,
    )

    runtime.approval.set_auto(True)

    assert runtime.approval.is_auto() is True
    assert session.state.approval.auto is True


@pytest.mark.asyncio
async def test_yolo_run_does_not_corrupt_persisted_safe_mode(
    config,
    session,
    lightweight_runtime_create,
) -> None:
    """Hypothesis B3a: a ``--yolo`` invocation must not silently downgrade the workspace's
    persisted trust posture.

    Yolo bypasses safe mode *functionally* (is_auto_approve / _unattended_denial_feedback
    short-circuit on yolo before reading safe_mode), so there is no need to force
    ``safe_mode=False`` at runtime — and doing so used to get persisted back to
    ``session.state.trust.safe_mode`` via the on-change callback, corrupting trust state.
    """
    session.state.trust.safe_mode = True

    runtime = await Runtime.create(
        config,
        OAuthManager(config),
        llm=None,
        session=session,
        yolo=True,
    )

    # Yolo still auto-approves — no deadlock behind safe mode.
    assert runtime.approval.is_yolo() is True
    assert runtime.approval.is_auto_approve() is True

    # An approval-state change persists state; the workspace trust posture must survive.
    runtime.approval.set_auto(True)
    assert session.state.trust.safe_mode is True


@pytest.mark.asyncio
async def test_no_yolo_forces_yolo_off_over_persisted_state(
    config,
    session,
    lightweight_runtime_create,
) -> None:
    """Hypothesis B3c: ``--no-yolo`` forces yolo off for the run even when persisted state
    (or config ``default_yolo``) would otherwise enable it."""
    session.state.approval.yolo = True

    runtime = await Runtime.create(
        config,
        OAuthManager(config),
        llm=None,
        session=session,
        yolo=False,
        no_yolo=True,
    )

    assert runtime.approval.is_yolo() is False

    runtime.approval.set_auto(True)
    assert session.state.approval.yolo is True


@pytest.mark.asyncio
async def test_default_config_yolo_auto_deliberates_destructive_actions(
    config,
    session,
    lightweight_runtime_create,
) -> None:
    """Hypothesis B1 (fixed): the obvious manual combo (``--yolo --auto``, default
    config) now has the destructive backstop.

    ``auto_deliberate_destructive_actions`` still defaults False (config.py:381-382), but
    the deliberation gate fires whenever an irreversible action would be auto-approved
    with no user present (``is_auto``), regardless of the config flag. So a destructive
    ``rm -rf`` is bounced once for deliberation instead of running blind. This matches the
    purpose-built ``autonomous_coding`` profile rather than being more dangerous than it.
    """
    assert config.auto_deliberate_destructive_actions is False  # still the default
    session.state.approval.auto = True

    runtime = await Runtime.create(
        config,
        OAuthManager(config),
        llm=None,
        session=session,
        yolo=True,
    )

    assert runtime.approval.is_yolo() is True
    assert runtime.approval.is_auto() is True
    assert runtime.approval.is_auto_approve() is True
    # No user present + would auto-approve a destructive action -> the backstop bounces it.
    assert runtime.approval.deliberation_gate(_shell_call("rm -rf build")) is not None


@pytest.mark.asyncio
async def test_runtime_create_silently_resumes_both_yolo_and_auto(
    config,
    session,
    lightweight_runtime_create,
) -> None:
    """Hypothesis B3: a session that persisted both yolo and auto silently resumes
    fully unsupervised, with no flags passed and no re-confirmation.

    ``effective_yolo = yolo or session.state.approval.yolo`` (agent.py:282) and auto is
    read straight from persisted state (agent.py:300). There is no CLI flag to force a
    persisted yolo off.
    """
    session.state.approval.yolo = True
    session.state.approval.auto = True

    runtime = await Runtime.create(
        config,
        OAuthManager(config),
        llm=None,
        session=session,
        yolo=False,  # no --yolo on this invocation
    )

    assert runtime.approval.is_yolo() is True  # restored from disk regardless
    assert runtime.approval.is_auto() is True
    assert runtime.approval.is_auto_approve() is True
