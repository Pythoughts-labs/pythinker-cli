from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from pythinker_code.background import TaskRuntime, TaskSpec
from pythinker_code.subagents import AgentLaunchSpec, AgentTypeDefinition, ToolPolicy


def _register_coder(runtime):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Good at general software engineering tasks.",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )


@pytest.mark.asyncio
async def test_create_agent_task_persists_orchestration_metadata(runtime, monkeypatch):
    _register_coder(runtime)

    async def _noop(self):
        return None

    monkeypatch.setattr("pythinker_code.background.agent_runner.BackgroundAgentRunner.run", _noop)

    view = runtime.background_tasks.create_agent_task(
        agent_id="a1234567",
        subagent_type="coder",
        prompt="do work",
        description="metadata",
        tool_call_id="tool-meta",
        model_override=None,
        dependencies=["agent-a", "agent-b"],
        budget_seconds=300,
        isolation="worktree",
    )

    assert view.spec.dependencies == ["agent-a", "agent-b"]
    assert view.spec.budget_seconds == 300
    assert view.spec.synthesis_state == "pending"
    assert view.spec.isolation == "worktree"
    assert view.spec.kind_payload is not None
    assert view.spec.kind_payload["dependencies"] == ["agent-a", "agent-b"]
    assert view.spec.kind_payload["resolved_review_target"] is None

    task = runtime.background_tasks._live_agent_tasks.pop(view.spec.id)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_recover_marks_orphaned_background_agent_recoverable(runtime):
    store = runtime.background_tasks.store
    agent_id = "a7654321"
    runtime.subagent_store.create_instance(
        agent_id=agent_id,
        description="recover me",
        launch_spec=AgentLaunchSpec(
            agent_id=agent_id,
            subagent_type="coder",
            model_override=None,
            effective_model=None,
        ),
    )
    runtime.subagent_store.update_instance(agent_id, status="running_background")
    spec = TaskSpec(
        id="agent7777",
        kind="agent",
        session_id=runtime.session.id,
        description="recoverable agent",
        tool_call_id="tool-recover",
        kind_payload={"agent_id": agent_id, "subagent_type": "coder", "prompt": "continue"},
    )
    store.create_task(spec)
    store.write_runtime(spec.id, TaskRuntime(status="running", updated_at=time.time()))

    runtime.background_tasks.recover()

    view = store.merged_view(spec.id)
    assert view.runtime.status == "recoverable"
    assert "resume the stored agent instance" in (view.runtime.failure_reason or "")
    assert runtime.subagent_store.require_instance(agent_id).status == "idle"
