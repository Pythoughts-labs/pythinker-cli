from __future__ import annotations

import json
from typing import cast

import pytest

from pythinker_code.soul.agent import Runtime
from pythinker_code.tools.workflow import Workflow
from pythinker_code.wire.types import ProgressNote


class FakeApproval:
    def __init__(self, approved=True):
        self._approved = approved
        self._orchestrations = set()
        self.requests = 0

    def is_orchestration_approved(self, fp):
        return fp in self._orchestrations

    def approve_orchestration(self, fp):
        self._orchestrations.add(fp)

    async def request(self, sender, action, description):
        self.requests += 1
        return _ApprovalResult(self._approved)


class _ApprovalResult:
    def __init__(self, approved):
        self._approved = approved

    def __bool__(self):
        return self._approved

    def rejection_error(self):
        from pythinker_core.tooling import ToolError

        return ToolError(message="rejected", brief="rejected")


class FakeAgentTool:
    """Stands in for AgentTool: returns the runner-format `[summary]` output."""

    def __init__(self, responder):
        self._responder = responder
        self.calls = []

    async def __call__(self, params):
        from pythinker_core.tooling import ToolOk

        self.calls.append(params)
        body = self._responder(params)
        return ToolOk(output=f"agent_id: x\nstatus: completed\n\n[summary]\n{body}")


def make_tool(monkeypatch, *, responder, approved=True, role="root"):
    approval = FakeApproval(approved)

    class FakeConfig:
        class background:
            max_running_tasks = 4

    class FakeRuntime:
        def __init__(self):
            self.role = role
            self.approval = approval
            self.config = FakeConfig()

            class _WD:
                def __str__(self):
                    return "."

            self.work_dir = _WD()

    runtime = FakeRuntime()
    # Patch AgentTool so the Workflow tool wires our fake instead of the real one.
    monkeypatch.setattr(
        "pythinker_code.tools.workflow.AgentTool", lambda rt: FakeAgentTool(responder)
    )
    # Suppress wire emission (no Wire ContextVar in a unit test).
    monkeypatch.setattr("pythinker_code.tools.workflow.wire_send", lambda *a, **k: None)
    tool = Workflow(cast(Runtime, runtime))
    return tool, approval


@pytest.mark.asyncio
async def test_runs_and_returns_result(monkeypatch):
    tool, approval = make_tool(monkeypatch, responder=lambda p: "SUMMARY_TEXT")
    script = (
        'meta = {"name": "n", "description": "d"}\n'
        'r = await agent("inspect repo", {"label": "L"})\n'
        'return {"r": r}\n'
    )
    res = await tool(tool.params(script=script))
    assert not res.is_error
    assert isinstance(res.output, str)
    assert json.loads(res.output) == {"r": "SUMMARY_TEXT"}
    assert approval.requests == 1  # approval requested exactly once


@pytest.mark.asyncio
async def test_root_only_guard(monkeypatch):
    tool, _ = make_tool(monkeypatch, responder=lambda p: "x", role="subagent")
    res = await tool(
        tool.params(script='meta = {"name": "n", "description": "d"}\nawait agent("x")\n')
    )
    assert res.is_error
    assert "root" in res.message.lower()


@pytest.mark.asyncio
async def test_rejected_approval_short_circuits(monkeypatch):
    tool, _ = make_tool(monkeypatch, responder=lambda p: "x", approved=False)
    res = await tool(
        tool.params(script='meta = {"name": "n", "description": "d"}\nawait agent("x")\n')
    )
    assert res.is_error


@pytest.mark.asyncio
async def test_no_agent_call_errors(monkeypatch):
    tool, _ = make_tool(monkeypatch, responder=lambda p: "x")
    res = await tool(
        tool.params(script='meta = {"name": "n", "description": "d"}\nreturn {"x": 1}\n')
    )
    assert res.is_error
    assert "agent()" in res.message


@pytest.mark.asyncio
async def test_token_budget_exhausted_fails_the_workflow(monkeypatch):
    # Drives the tool through its public ToolOk/ToolError surface rather than
    # spying on run_workflow's internal kwargs: a tiny token_budget is exhausted
    # by the first agent() call's estimated spend, so the second (bare, not
    # inside parallel()) agent() call propagates "budget exhausted" and fails
    # the whole tool call.
    tool, _ = make_tool(monkeypatch, responder=lambda p: "x" * 200)
    script = (
        'meta = {"name": "n", "description": "d"}\n'
        'r1 = await agent("x", {"label": "L1"})\n'
        'r2 = await agent("y", {"label": "L2"})\n'
        'return {"r1": r1, "r2": r2}\n'
    )
    res = await tool(tool.params(script=script, token_budget=1))
    assert res.is_error
    assert "budget" in res.message.lower()


@pytest.mark.asyncio
async def test_token_budget_unset_allows_large_results(monkeypatch):
    # No token_budget set: even a large agent() result must not trip any cap,
    # proving the default is genuinely unbounded (observable via ToolOk).
    tool, _ = make_tool(monkeypatch, responder=lambda p: "x" * 5000)
    script = (
        'meta = {"name": "n", "description": "d"}\n'
        'r = await agent("x", {"label": "L"})\n'
        'return {"r": r}\n'
    )
    res = await tool(tool.params(script=script))
    assert not res.is_error


@pytest.mark.asyncio
async def test_log_calls_reach_the_progress_note(monkeypatch):
    tool, _ = make_tool(monkeypatch, responder=lambda p: "ok")
    notes: list[ProgressNote] = []
    monkeypatch.setattr("pythinker_code.tools.workflow.wire_send", notes.append)
    script = (
        'meta = {"name": "n", "description": "d"}\n'
        'log("custom checkpoint reached")\n'
        'r = await agent("x", {"label": "L"})\n'
        'return {"r": r}\n'
    )
    res = await tool(tool.params(script=script))
    assert not res.is_error
    assert any("custom checkpoint reached" in note.body for note in notes)


@pytest.mark.asyncio
async def test_schema_validation_and_retry(monkeypatch):
    attempts = {"n": 0}

    def responder(params):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return "not json"  # first attempt fails validation
        return '```json\n{"paths": ["a.py"]}\n```'

    tool, _ = make_tool(monkeypatch, responder=responder)
    script = (
        'meta = {"name": "n", "description": "d"}\n'
        'r = await agent("find files", {"schema": {"type": "object", '
        '"properties": {"paths": {"type": "array"}}, "required": ["paths"]}})\n'
        "return r\n"
    )
    res = await tool(tool.params(script=script))
    assert isinstance(res.output, str)
    assert json.loads(res.output) == {"paths": ["a.py"]}
    assert attempts["n"] == 2  # retried once
