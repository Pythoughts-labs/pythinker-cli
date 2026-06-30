"""Integration test proving Workflow's cancellation tears down a real child agent.

``test_cancellation_marks_running_cancelled_and_reraises`` in
``tests/tools/test_workflow_engine.py`` only proves the engine's own bookkeeping with a
fake ``agent_runner``. This test exercises the real spawn layer one level up: cancelling
the ``Workflow`` tool's task must propagate through ``AgentTool`` ->
``ForegroundSubagentRunner`` and mark the child instance "killed" in the subagent store,
the same pattern used by
``test_agent_tool.py::test_agent_tool_marks_instance_killed_when_initial_run_is_cancelled``.
"""

from __future__ import annotations

import asyncio

import pytest
from pythinker_core.tooling.empty import EmptyToolset

from pythinker_code.soul.agent import Agent as SoulAgent
from pythinker_code.subagents.models import AgentTypeDefinition, ToolPolicy
from pythinker_code.tools.workflow import Workflow
from tests.conftest import tool_call_context


class _RecordingWire:
    """Minimal wire stub so Workflow's progress emits (wire_send) don't assert-fail.

    Workflow emits ProgressNote updates via wire_send on phase/agent-start/agent-end
    hooks, which asserts a wire is set. Outside the full soul loop there is none, so
    this stub (the same pattern as test_agent_tool.py's ``_RecordingWire``) stands in.
    """

    def __init__(self) -> None:
        self.soul_side = self

    def send(self, msg: object) -> None:
        pass


@pytest.mark.asyncio
async def test_workflow_cancel_tears_down_real_child_through_agent_tool(runtime, monkeypatch):
    monkeypatch.setattr("pythinker_code.soul.get_wire_or_none", lambda: _RecordingWire())
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Good at general software engineering tasks.",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )

    async def fake_load_agent(agent_file, runtime, *, mcp_configs, start_mcp_loading=True):
        return SoulAgent(
            name=agent_file.stem,
            system_prompt="Subagent system prompt",
            toolset=EmptyToolset(),
            runtime=runtime,
        )

    started = asyncio.Event()

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        started.set()
        await asyncio.sleep(10.0)  # never resolves on its own; must be cancelled
        raise AssertionError("fake_run_soul should have been cancelled, not completed")

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    tool = Workflow(runtime)
    script = (
        'meta = {"name": "n", "description": "d"}\n'
        'r = await agent("investigate bug", {"label": "L"})\n'
        'return {"r": r}\n'
    )
    with tool_call_context("Workflow"):
        task = asyncio.create_task(tool(tool.params(script=script)))
    await asyncio.wait_for(started.wait(), timeout=2.0)  # let the real child actually start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    records = [
        r
        for r in runtime.subagent_store.list_instances()
        if r.description in ("L", "investigate bug")
    ]
    assert len(records) == 1
    assert records[0].status == "killed"  # real AgentTool/ForegroundSubagentRunner teardown ran,
    # not just the engine's own bookkeeping — this is the load-bearing assertion.
