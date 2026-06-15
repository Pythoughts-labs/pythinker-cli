from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest
from pythinker_core.chat_provider import APIConnectionError, APIStatusError, ChatProviderError
from pythinker_core.message import Message
from pythinker_core.tooling import ToolOk
from pythinker_core.tooling.empty import EmptyToolset

from pythinker_code import scratchpad
from pythinker_code.approval_runtime import get_current_approval_source_or_none
from pythinker_code.background import TaskRuntime, TaskSpec
from pythinker_code.soul import MaxStepsReached, RunCancelled
from pythinker_code.soul.agent import Agent as SoulAgent
from pythinker_code.soul.agent import Runtime
from pythinker_code.soul.approval import ApprovalResult
from pythinker_code.subagents import AgentLaunchSpec, AgentTypeDefinition, ToolPolicy
from pythinker_code.subagents.core import SUBAGENT_OUTPUT_LANGUAGE_INSTRUCTION
from pythinker_code.tools.agent import AgentRunConfig, RunAgents
from pythinker_code.wire.types import (
    ApprovalRequest,
    MCPServerSnapshot,
    MCPStatusSnapshot,
    SubagentToolFallback,
    TextPart,
)
from tests.conftest import tool_call_context


class _RecordingWire:
    def __init__(self, captured: list[object]) -> None:
        self.soul_side = self
        self._captured = captured

    def send(self, msg: object) -> None:
        self._captured.append(msg)


def _extract_agent_id(output: str) -> str:
    match = re.search(r"^agent_id: (\S+)$", output, re.MULTILINE)
    assert match is not None
    return match.group(1)


def _mcp_snapshot(loading: bool, servers: list[tuple[str, str]]) -> MCPStatusSnapshot:
    return MCPStatusSnapshot(
        loading=loading,
        connected=sum(1 for _, s in servers if s == "connected"),
        total=len(servers),
        tools=0,
        servers=tuple(MCPServerSnapshot(name=n, status=s) for n, s in servers),  # type: ignore[arg-type]
    )


def test_missing_required_mcp_servers() -> None:
    """The spawn gate's pure core: a required MCP server counts as missing only when MCP
    loading has settled and the server is not connected (absent or failed). While loading,
    nothing is reported (the server may yet connect — avoid a spurious rejection)."""
    from pythinker_code.tools.agent import _missing_required_mcp_servers

    assert _missing_required_mcp_servers((), None) == []  # nothing required
    assert _missing_required_mcp_servers(("db",), None) == ["db"]  # no MCP configured -> absent
    assert _missing_required_mcp_servers(("db",), _mcp_snapshot(True, [])) == []  # loading
    assert (
        _missing_required_mcp_servers(("db",), _mcp_snapshot(False, [("db", "connected")])) == []
    )  # connected
    assert _missing_required_mcp_servers(("db",), _mcp_snapshot(False, [("db", "failed")])) == [
        "db"
    ]  # settled + failed
    assert _missing_required_mcp_servers(
        ("db", "fs"), _mcp_snapshot(False, [("db", "connected")])
    ) == ["fs"]  # one connected, other absent


def test_check_required_mcp_servers_gates_absent_server(runtime: Runtime) -> None:
    """The spawn gate rejects a fresh agent whose required MCP server is absent, allows it
    while MCP is loading or once connected, and never gates a type with no requirement."""
    from pythinker_code.subagents import AgentTypeDefinition, ToolPolicy
    from pythinker_code.tools.agent import AgentTool

    assert runtime.subagent_store is not None  # populated by the runtime fixture
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="needs_db",
            description="needs the db MCP server",
            agent_file=runtime.subagent_store.root / "needs_db.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
            required_mcp_servers=("db",),
        )
    )
    tool = AgentTool(runtime)

    runtime.mcp_status = lambda: None  # no MCP configured -> required server absent
    err = tool.check_required_mcp_servers("needs_db")
    assert err is not None
    assert "db" in err.message

    runtime.mcp_status = lambda: _mcp_snapshot(True, [])  # still loading -> allow
    assert tool.check_required_mcp_servers("needs_db") is None

    runtime.mcp_status = lambda: _mcp_snapshot(False, [("db", "connected")])  # connected -> allow
    assert tool.check_required_mcp_servers("needs_db") is None

    assert tool.check_required_mcp_servers("mocker") is None  # no requirement -> never gated


def _extract_task_id(output: str) -> str:
    match = re.search(r"^task_id: (\S+)$", output, re.MULTILINE)
    assert match is not None
    return match.group(1)


async def test_agent_tool_creates_instance_and_returns_agent_id(agent_tool, runtime, monkeypatch):
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

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        await soul.context.append_message(
            Message(role="assistant", content=[TextPart(text="done")])
        )

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    result = await agent_tool(
        agent_tool.params(
            description="investigate bug",
            prompt="look into parser issue",
        )
    )

    assert not result.is_error
    agent_id = _extract_agent_id(result.output)
    assert "resumed: false" in result.output
    assert "actual_subagent_type: coder" in result.output
    assert runtime.subagent_store.require_instance(agent_id).subagent_type == "coder"
    assert runtime.subagent_store.prompt_path(agent_id).read_text(encoding="utf-8") == (
        f"{SUBAGENT_OUTPUT_LANGUAGE_INSTRUCTION}\n\nlook into parser issue"
    )


async def test_agent_tool_foreground_passes_subagent_wire_file(agent_tool, runtime, monkeypatch):
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

    seen_wire_paths: list[str] = []

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        seen_wire_paths.append(str(wire_file.path) if wire_file is not None else "")
        await soul.context.append_message(
            Message(role="assistant", content=[TextPart(text="done")])
        )

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    result = await agent_tool(
        agent_tool.params(
            description="foreground wire",
            prompt="look into parser issue",
        )
    )

    assert not result.is_error
    agent_id = _extract_agent_id(result.output)
    assert seen_wire_paths
    assert set(seen_wire_paths) == {str(runtime.subagent_store.wire_path(agent_id))}


async def test_agent_tool_resume_uses_actual_type(agent_tool, runtime, monkeypatch):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="mocker",
            description="The mock agent for testing purposes.",
            agent_file=runtime.subagent_store.root / "mocker.yaml",
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

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        await soul.context.append_message(
            Message(role="assistant", content=[TextPart(text="done")])
        )

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    runtime.subagent_store.create_instance(
        agent_id="aexisting",
        description="old instance",
        launch_spec=AgentLaunchSpec(
            agent_id="aexisting",
            subagent_type="mocker",
            model_override=None,
            effective_model=None,
        ),
    )

    result = await agent_tool(
        agent_tool.params(
            description="resume work",
            prompt="continue the previous work",
            subagent_type="coder",
            resume="aexisting",
        )
    )

    assert not result.is_error
    assert "resumed: true" in result.output
    assert "requested_subagent_type: coder" in result.output
    assert "actual_subagent_type: mocker" in result.output


async def test_agent_tool_rejects_resume_when_instance_is_already_running(agent_tool, runtime):
    runtime.subagent_store.create_instance(
        agent_id="arunning",
        description="running instance",
        launch_spec=AgentLaunchSpec(
            agent_id="arunning",
            subagent_type="coder",
            model_override=None,
            effective_model=None,
        ),
    )
    runtime.subagent_store.update_instance("arunning", status="running_foreground")

    result = await agent_tool(
        agent_tool.params(
            description="resume work",
            prompt="continue the previous work",
            resume="arunning",
        )
    )

    assert result.is_error
    assert result.brief == "Agent already running"
    assert "cannot be resumed concurrently" in result.message


async def test_unknown_subagent_type_emits_fallback_wire_event(
    agent_tool, monkeypatch: pytest.MonkeyPatch
):
    captured: list[object] = []
    monkeypatch.setattr("pythinker_code.soul.get_wire_or_none", lambda: _RecordingWire(captured))

    result = await agent_tool(
        agent_tool.params(
            description="unknown type",
            prompt="look into parser issue",
            subagent_type="does-not-exist",
        )
    )

    assert result.is_error
    assert captured == [
        SubagentToolFallback(
            reason="unavailable_agent_type",
            requested_type="does-not-exist",
            available_types=("mocker",),
        )
    ]


async def test_policy_denied_subagent_type_emits_fallback_wire_event(
    agent_tool, runtime, monkeypatch: pytest.MonkeyPatch
):
    runtime.config.agent_execution_profile = "plan_only"
    captured: list[object] = []
    monkeypatch.setattr("pythinker_code.soul.get_wire_or_none", lambda: _RecordingWire(captured))

    result = await agent_tool(
        agent_tool.params(
            description="policy denied",
            prompt="look into parser issue",
            subagent_type="mocker",
        )
    )

    assert result.is_error
    assert captured == [
        SubagentToolFallback(
            reason="policy_denied",
            requested_type="mocker",
            available_types=("mocker",),
        )
    ]


async def test_agent_tool_keeps_result_when_summary_continuation_hits_max_steps(
    agent_tool, runtime, monkeypatch
):
    """The summary continuation is a nicety: a failure there must not convert
    the agent's completed work into a hard failure — the existing (short)
    summary is returned and the instance stays resumable."""
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

    call_count = 0

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await soul.context.append_message(
                Message(role="assistant", content=[TextPart(text="too short")])
            )
            return
        raise MaxStepsReached(10)

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    result = await agent_tool(
        agent_tool.params(
            description="investigate bug",
            prompt="look into parser issue",
        )
    )

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "too short" in result.output
    records = [
        record
        for record in runtime.subagent_store.list_instances()
        if record.description == "investigate bug"
    ]
    assert len(records) == 1
    assert records[0].status == "idle"


async def test_agent_tool_marks_instance_killed_when_summary_continuation_is_cancelled(
    agent_tool, runtime, monkeypatch
):
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

    call_count = 0

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await soul.context.append_message(
                Message(role="assistant", content=[TextPart(text="too short")])
            )
            return
        raise asyncio.CancelledError()

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    with pytest.raises(asyncio.CancelledError):
        await agent_tool(
            agent_tool.params(
                description="cancelled summary continuation",
                prompt="look into parser issue",
            )
        )

    records = [
        record
        for record in runtime.subagent_store.list_instances()
        if record.description == "cancelled summary continuation"
    ]
    assert len(records) == 1
    assert records[0].status == "killed"


async def test_agent_tool_marks_instance_failed_when_initial_run_raises(
    agent_tool, runtime, monkeypatch
):
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

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        raise RuntimeError("boom")

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    result = await agent_tool(
        agent_tool.params(
            description="initial failure",
            prompt="look into parser issue",
        )
    )

    assert result.is_error
    assert result.brief == "Agent run error"
    assert "boom" in result.message
    records = [
        record
        for record in runtime.subagent_store.list_instances()
        if record.description == "initial failure"
    ]
    assert len(records) == 1
    assert records[0].status == "failed"


async def test_agent_tool_marks_instance_killed_when_initial_run_is_cancelled(
    agent_tool, runtime, monkeypatch
):
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

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        raise asyncio.CancelledError()

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    with pytest.raises(asyncio.CancelledError):
        await agent_tool(
            agent_tool.params(
                description="cancelled run",
                prompt="look into parser issue",
            )
        )

    records = [
        record
        for record in runtime.subagent_store.list_instances()
        if record.description == "cancelled run"
    ]
    assert len(records) == 1
    assert records[0].status == "killed"


async def test_agent_tool_returns_rejected_by_user_when_tool_request_is_rejected(
    agent_tool, runtime, monkeypatch
):
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

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        # Simulate the subagent continuing after a tool rejection: the LLM sees the
        # rejection and produces an assistant response instead of stopping.
        await soul.context.append_message(
            Message(
                role="assistant",
                content=[TextPart(text="x" * 250)],
            )
        )

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    result = await agent_tool(
        agent_tool.params(
            description="rejected tool",
            prompt="look into parser issue",
        )
    )

    assert not result.is_error
    assert "status: completed" in result.output


async def test_agent_tool_rejects_subagent_runtime(agent_tool, runtime):
    runtime.role = "subagent"

    result = await agent_tool(
        agent_tool.params(
            description="delegate work",
            prompt="do something",
        )
    )

    assert result.is_error
    assert result.brief == "Agent unavailable"


async def test_agent_tool_starts_background_task(agent_tool, runtime, monkeypatch):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Good at general software engineering tasks.",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )

    def fake_create_agent_task(**kwargs):
        return SimpleNamespace(
            spec=SimpleNamespace(
                id="a-task-1",
                kind="agent",
                description=kwargs["description"],
            ),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="investigate bug",
                prompt="look into parser issue",
                run_in_background=True,
            )
        )

    assert not result.is_error
    assert "tool_status: launched" in result.output
    assert result.extras == {"status": "launched"}
    assert "task_id: a-task-1" in result.output
    assert "kind: agent" in result.output
    assert "automatic_notification: true" in result.output


async def test_run_agents_launches_background_children_with_base_prompt(runtime, monkeypatch):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="explore",
            description="Read-only exploration.",
            agent_file=runtime.subagent_store.root / "explore.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    created: list[dict[str, object]] = []

    def fake_create_agent_task(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(
            spec=SimpleNamespace(
                id=f"task-{len(created)}",
                kind="agent",
                description=kwargs["description"],
            ),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)
    tool = RunAgents(runtime)

    with tool_call_context("RunAgents"):
        result = await tool(
            tool.params(
                summary="parallel scouting",
                base_prompt="Shared context",
                agents=[
                    AgentRunConfig(
                        name="api-scout",
                        title="API scout",
                        subagent_type="explore",
                        prompt="Find API files",
                    ),
                    AgentRunConfig(
                        name="test-scout",
                        title="Test scout",
                        subagent_type="explore",
                        prompt="Find tests",
                    ),
                ],
            )
        )

    assert not result.is_error
    assert "tool_status: launched" in result.output
    assert "orchestration_approval: requested" in result.output
    assert result.extras == {"status": "launched"}
    assert "agent_count: 2" in result.output
    assert "task_id: task-1" in result.output
    assert "task_id: task-2" in result.output
    assert [item["description"] for item in created] == ["API scout", "Test scout"]
    assert [item["prompt"] for item in created] == [
        "Shared context\n\nFind API files",
        "Shared context\n\nFind tests",
    ]
    assert "scratchpad: appended" in result.output
    assert scratchpad.session_scratch_path(
        runtime.session.work_dir, session_id=runtime.session.id
    ).is_file()


async def test_run_agents_replaces_generic_names_with_codenames(runtime, monkeypatch):
    """Children named after their own type (`code-reviewer:code-reviewer`) get
    distinctive generated codenames; the type stays in its own field. Each
    child in the batch gets a unique codename, and with no caller title the
    description becomes `codename (type)` so notifications stay readable."""
    from pythinker_code.subagents.codenames import _ADJECTIVES, _NOUNS

    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="explore",
            description="Read-only exploration.",
            agent_file=runtime.subagent_store.root / "explore.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    created: list[dict[str, object]] = []

    def fake_create_agent_task(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(
            spec=SimpleNamespace(
                id=f"task-{len(created)}",
                kind="agent",
                description=kwargs["description"],
            ),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)
    tool = RunAgents(runtime)

    with tool_call_context("RunAgents"):
        result = await tool(
            tool.params(
                summary="parallel review",
                agents=[
                    AgentRunConfig(name="explore", subagent_type="explore", prompt="Scout A"),
                    AgentRunConfig(name="explore", subagent_type="explore", prompt="Scout B"),
                ],
            )
        )

    assert not result.is_error
    output = result.output if isinstance(result.output, str) else ""
    names = [
        line.removeprefix("- name: ").strip()
        for line in output.splitlines()
        if line.startswith("- name: ")
    ]
    assert len(names) == 2
    assert len(set(names)) == 2, "each child must get a unique codename"
    for name in names:
        assert name != "explore"
        adjective, _, noun = name.partition("-")
        assert adjective in _ADJECTIVES and noun.split("-")[0] in _NOUNS, name
    descriptions = [str(item["description"]) for item in created]
    assert descriptions == [f"{name} (explore)" for name in names]


async def test_run_agents_keeps_caller_chosen_names(runtime, monkeypatch):
    """Distinct caller-supplied names and titles pass through untouched."""
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="explore",
            description="Read-only exploration.",
            agent_file=runtime.subagent_store.root / "explore.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    created: list[dict[str, object]] = []

    def fake_create_agent_task(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(
            spec=SimpleNamespace(
                id=f"task-{len(created)}",
                kind="agent",
                description=kwargs["description"],
            ),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)
    tool = RunAgents(runtime)

    with tool_call_context("RunAgents"):
        result = await tool(
            tool.params(
                summary="parallel scouting",
                agents=[
                    AgentRunConfig(
                        name="api-scout",
                        title="API scout",
                        subagent_type="explore",
                        prompt="Find API files",
                    ),
                ],
            )
        )

    assert not result.is_error
    assert "- name: api-scout" in result.output
    assert [item["description"] for item in created] == ["API scout"]


async def test_run_agents_foreground_reports_completed_status(runtime):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="code-reviewer",
            description="Reviews diffs.",
            agent_file=runtime.subagent_store.root / "code-reviewer.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    calls = []

    class FakeAgentTool:
        def check_execution_policy(self, subagent_type):
            return None

        def check_required_mcp_servers(self, subagent_type):
            return None

        async def __call__(self, params):
            calls.append(params)
            return ToolOk(
                output=(
                    "agent_id: afocused1\n"
                    "resumed: false\n"
                    f"actual_subagent_type: {params.subagent_type}\n"
                    "status: completed\n"
                    "\n"
                    "[summary]\n"
                    f"Completed {params.description}."
                )
            )

    tool = RunAgents(runtime)
    tool._agent_tool = FakeAgentTool()  # type: ignore[assignment]

    with tool_call_context("RunAgents"):
        result = await tool(
            tool.params(
                summary="sequential review",
                agents=[
                    AgentRunConfig(
                        name="reviewer",
                        title="Reviewer",
                        subagent_type="code-reviewer",
                        prompt="Review the diff",
                    )
                ],
                run_in_background=False,
            )
        )

    assert not result.is_error
    assert result.message == "Agents completed."
    assert result.extras == {"status": "success"}
    assert "tool_status: success" in result.output
    assert "mode: foreground" in result.output
    assert "status: completed" in result.output
    assert calls and calls[0].run_in_background is False


async def test_run_agents_defers_background_batch_over_available_slots(runtime, monkeypatch):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="explore",
            description="Read-only exploration.",
            agent_file=runtime.subagent_store.root / "explore.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    created: list[dict[str, object]] = []
    approval_requests = 0

    async def fake_request(*args, **kwargs):
        nonlocal approval_requests
        approval_requests += 1
        return ApprovalResult(approved=True)

    def fake_create_agent_task(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(
            spec=SimpleNamespace(
                id=f"task-{len(created)}",
                kind="agent",
                description=kwargs["description"],
            ),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.approval, "request", fake_request)
    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)
    tool = RunAgents(runtime)

    with tool_call_context("RunAgents"):
        result = await tool(
            tool.params(
                summary="oversized scouting",
                base_prompt="Shared context",
                agents=[
                    AgentRunConfig(
                        name=f"scout-{idx}",
                        title=f"Scout {idx}",
                        subagent_type="explore",
                        prompt=f"Find area {idx}",
                    )
                    for idx in range(5)
                ],
            )
        )

    assert not result.is_error
    assert result.message == "Agents launched; 1 deferred by background capacity."
    assert "tool_status: launched" in result.output
    assert "requested_agent_count: 5" in result.output
    assert "agent_count: 4" in result.output
    assert "deferred_agent_count: 1" in result.output
    assert "capacity_limited: true" in result.output
    assert "name: scout-4" in result.output
    assert len(created) == 4
    assert approval_requests == 1


async def test_run_agents_defers_background_batch_when_some_slots_are_used(runtime, monkeypatch):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="explore",
            description="Read-only exploration.",
            agent_file=runtime.subagent_store.root / "explore.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    created: list[dict[str, object]] = []

    def fake_create_agent_task(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(
            spec=SimpleNamespace(
                id=f"task-{len(created)}",
                kind="agent",
                description=kwargs["description"],
            ),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "active_task_count", lambda: 3)
    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)
    tool = RunAgents(runtime)

    with tool_call_context("RunAgents"):
        result = await tool(
            tool.params(
                summary="capacity-aware scouting",
                agents=[
                    AgentRunConfig(
                        name="api-scout",
                        title="API scout",
                        subagent_type="explore",
                        prompt="Find API files",
                    ),
                    AgentRunConfig(
                        name="test-scout",
                        title="Test scout",
                        subagent_type="explore",
                        prompt="Find tests",
                    ),
                ],
            )
        )

    assert not result.is_error
    assert result.message == "Agents launched; 1 deferred by background capacity."
    assert "agent_count: 1" in result.output
    assert "deferred_agent_count: 1" in result.output
    assert "active_background_tasks: 3" in result.output
    assert "available_background_slots: 1" in result.output
    assert [item["description"] for item in created] == ["API scout"]


async def test_run_agents_rejects_background_batch_when_no_slots_are_available(
    runtime, monkeypatch
):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="explore",
            description="Read-only exploration.",
            agent_file=runtime.subagent_store.root / "explore.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    created: list[dict[str, object]] = []

    def fake_create_agent_task(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(
            spec=SimpleNamespace(id=f"task-{len(created)}", kind="agent"),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "active_task_count", lambda: 4)
    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)
    tool = RunAgents(runtime)

    with tool_call_context("RunAgents"):
        result = await tool(
            tool.params(
                summary="capacity-aware scouting",
                agents=[
                    AgentRunConfig(
                        name="api-scout",
                        title="API scout",
                        subagent_type="explore",
                        prompt="Find API files",
                    )
                ],
            )
        )

    assert result.is_error
    assert result.brief == "Background task limit"
    assert "no background task slots are available" in result.message
    assert "available_background_slots: 0" in result.output
    assert created == []


async def test_run_agents_reuses_approved_orchestration_fingerprint(runtime, monkeypatch):
    runtime.approval.set_yolo(False)
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="explore",
            description="Read-only exploration.",
            agent_file=runtime.subagent_store.root / "explore.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    created: list[dict[str, object]] = []
    approval_requests = 0

    async def fake_request(*args, **kwargs):
        nonlocal approval_requests
        approval_requests += 1
        return ApprovalResult(approved=True)

    def fake_create_agent_task(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(
            spec=SimpleNamespace(
                id=f"task-{len(created)}",
                kind="agent",
                description=kwargs["description"],
            ),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.approval, "request", fake_request)
    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)
    tool = RunAgents(runtime)
    params = tool.params(
        summary="parallel scouting",
        base_prompt="Shared context",
        agents=[
            AgentRunConfig(
                name="api-scout",
                title="API scout",
                subagent_type="explore",
                prompt="Find API files",
            ),
        ],
    )

    with tool_call_context("RunAgents"):
        first = await tool(params)
    with tool_call_context("RunAgents"):
        second = await tool(params)

    assert not first.is_error
    assert not second.is_error
    assert "orchestration_approval: requested" in first.output
    assert "orchestration_approval: reused" in second.output
    assert approval_requests == 1
    assert len(created) == 2


async def test_agent_tool_background_rejects_resume_when_instance_is_already_running(
    agent_tool, runtime, monkeypatch
):
    called = False

    def fake_create_agent_task(**kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(
            spec=SimpleNamespace(id="a-task-1", kind="agent", description=kwargs["description"]),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)

    runtime.subagent_store.create_instance(
        agent_id="abgrunning",
        description="running instance",
        launch_spec=AgentLaunchSpec(
            agent_id="abgrunning",
            subagent_type="coder",
            model_override=None,
            effective_model=None,
        ),
    )
    runtime.subagent_store.update_instance("abgrunning", status="running_background")

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="resume work",
                prompt="continue the previous work",
                resume="abgrunning",
                run_in_background=True,
            )
        )

    assert result.is_error
    assert result.brief == "Agent already running"
    assert "cannot be resumed concurrently" in result.message
    assert called is False


async def test_agent_tool_background_resume_rejection_names_task_and_retrieval_path(
    agent_tool, runtime, monkeypatch
):
    launched = False

    def fake_create_agent_task(**kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("must not launch while the instance is busy")

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)

    runtime.subagent_store.create_instance(
        agent_id="abusybg1",
        description="running instance",
        launch_spec=AgentLaunchSpec(
            agent_id="abusybg1",
            subagent_type="coder",
            model_override=None,
            effective_model=None,
        ),
    )
    runtime.subagent_store.update_instance("abusybg1", status="running_background")
    spec = TaskSpec(
        id="agent-busy1",
        kind="agent",
        session_id=runtime.session.id,
        description="busy agent task",
        tool_call_id="tool-busy",
        kind_payload={"agent_id": "abusybg1"},
    )
    runtime.background_tasks.store.create_task(spec)
    runtime.background_tasks.store.write_runtime(spec.id, TaskRuntime(status="running"))

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="resume work",
                prompt="report your findings",
                resume="abusybg1",
                run_in_background=True,
            )
        )

    assert result.is_error
    assert "cannot be resumed concurrently" in result.message
    assert "agent-busy1" in result.message
    assert "TaskOutput" in result.message
    assert launched is False


async def test_agent_tool_background_resume_reconciles_stale_record(
    agent_tool, runtime, monkeypatch
):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Good at general software engineering tasks.",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    created = []

    def fake_create_agent_task(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(
            spec=SimpleNamespace(id="a-task-2", kind="agent", description=kwargs["description"]),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)

    runtime.subagent_store.create_instance(
        agent_id="astalebg1",
        description="finished instance",
        launch_spec=AgentLaunchSpec(
            agent_id="astalebg1",
            subagent_type="coder",
            model_override=None,
            effective_model=None,
        ),
    )
    runtime.subagent_store.update_instance("astalebg1", status="running_background")
    spec = TaskSpec(
        id="agent-stale1",
        kind="agent",
        session_id=runtime.session.id,
        description="finished agent task",
        tool_call_id="tool-stale",
        kind_payload={"agent_id": "astalebg1"},
    )
    runtime.background_tasks.store.create_task(spec)
    runtime.background_tasks.store.write_runtime(spec.id, TaskRuntime(status="completed"))

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="follow-up work",
                prompt="summarize your findings",
                resume="astalebg1",
                run_in_background=True,
            )
        )

    assert not result.is_error
    assert len(created) == 1
    assert "agent_id: astalebg1" in result.output


async def test_agent_tool_foreground_resume_of_background_instance_names_task(agent_tool, runtime):
    runtime.subagent_store.create_instance(
        agent_id="abusybg2",
        description="running instance",
        launch_spec=AgentLaunchSpec(
            agent_id="abusybg2",
            subagent_type="coder",
            model_override=None,
            effective_model=None,
        ),
    )
    runtime.subagent_store.update_instance("abusybg2", status="running_background")
    spec = TaskSpec(
        id="agent-busy2",
        kind="agent",
        session_id=runtime.session.id,
        description="busy agent task",
        tool_call_id="tool-busy2",
        kind_payload={"agent_id": "abusybg2"},
    )
    runtime.background_tasks.store.create_task(spec)
    runtime.background_tasks.store.write_runtime(spec.id, TaskRuntime(status="running"))

    result = await agent_tool(
        agent_tool.params(
            description="resume work",
            prompt="report your findings",
            resume="abusybg2",
        )
    )

    assert result.is_error
    assert "cannot be resumed concurrently" in result.message
    assert "agent-busy2" in result.message
    assert "TaskOutput" in result.message


async def test_agent_tool_background_resume_marks_running_before_dispatch(
    agent_tool, runtime, monkeypatch
):
    """The instance must be running_background *before* create_agent_task returns
    so that a concurrent resume sees the guard immediately."""
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Good at general software engineering tasks.",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    runtime.subagent_store.create_instance(
        agent_id="aconcurr",
        description="concurrency test",
        launch_spec=AgentLaunchSpec(
            agent_id="aconcurr",
            subagent_type="coder",
            model_override=None,
            effective_model=None,
        ),
    )

    status_during_create: list[str] = []

    def fake_create_agent_task(**kwargs):
        # Capture instance status at the moment create_agent_task is called.
        record = runtime.subagent_store.require_instance("aconcurr")
        status_during_create.append(record.status)
        return SimpleNamespace(
            spec=SimpleNamespace(id="a-task-c", kind="agent", description=kwargs["description"]),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="concurrent resume",
                prompt="do work",
                resume="aconcurr",
                run_in_background=True,
            )
        )

    assert not result.is_error
    # Instance must already be running_background when create_agent_task is called
    assert status_during_create == ["running_background"]


async def test_agent_tool_background_new_instance_marks_running_before_dispatch(
    agent_tool, runtime, monkeypatch
):
    """A fresh (non-resume) background instance must also be running_background
    before create_agent_task is called."""
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Good at general software engineering tasks.",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )

    status_during_create: list[str] = []
    agent_ids_seen: list[str] = []

    def fake_create_agent_task(**kwargs):
        agent_id = kwargs["agent_id"]
        agent_ids_seen.append(agent_id)
        record = runtime.subagent_store.require_instance(agent_id)
        status_during_create.append(record.status)
        return SimpleNamespace(
            spec=SimpleNamespace(id="a-task-n", kind="agent", description=kwargs["description"]),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="fresh bg",
                prompt="do work",
                run_in_background=True,
            )
        )

    assert not result.is_error
    assert status_during_create == ["running_background"]


async def test_agent_tool_background_rolls_back_status_on_dispatch_failure(
    agent_tool, runtime, monkeypatch
):
    """If create_agent_task raises for a resumed instance, the instance status
    must be rolled back to idle (not left as running_background)."""
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Good at general software engineering tasks.",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    runtime.subagent_store.create_instance(
        agent_id="arollbk1",
        description="rollback test",
        launch_spec=AgentLaunchSpec(
            agent_id="arollbk1",
            subagent_type="coder",
            model_override=None,
            effective_model=None,
        ),
    )

    def fake_create_agent_task(**kwargs):
        raise RuntimeError("Too many background tasks are already running.")

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="rollback resume",
                prompt="continue work",
                resume="arollbk1",
                run_in_background=True,
            )
        )

    assert result.is_error
    # Instance must still exist (not deleted — it was a resume, not new)
    record = runtime.subagent_store.require_instance("arollbk1")
    # Status must be rolled back to idle, not stuck at running_background
    assert record.status == "idle"


async def test_agent_tool_background_rejects_missing_resume_instance(agent_tool, runtime):
    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="resume work",
                prompt="continue the previous work",
                resume="amissing",
                run_in_background=True,
            )
        )

    assert result.is_error
    assert result.brief == "Agent not found"
    assert "Subagent instance not found" in result.message


async def test_agent_tool_background_returns_tool_error_when_task_limit_is_hit(
    agent_tool, runtime, monkeypatch
):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Good at general software engineering tasks.",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )

    def fake_create_agent_task(**kwargs):
        raise RuntimeError("Too many background tasks are already running.")

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="investigate bug",
                prompt="look into parser issue",
                run_in_background=True,
            )
        )

    assert result.is_error
    assert result.brief == "Background start failed"
    assert "Too many background tasks are already running." in result.message


async def test_agent_tool_background_rolls_back_fresh_instance_when_task_start_fails(
    agent_tool, runtime, monkeypatch
):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Good at general software engineering tasks.",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )

    def fake_create_agent_task(**kwargs):
        raise RuntimeError("Too many background tasks are already running.")

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="rollback instance",
                prompt="look into parser issue",
                run_in_background=True,
            )
        )

    assert result.is_error
    assert result.brief == "Background start failed"
    assert all(
        record.description != "rollback instance"
        for record in runtime.subagent_store.list_instances()
    )


async def test_agent_tool_background_rejects_invalid_subagent_type(agent_tool, runtime):
    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="invalid type",
                prompt="do work",
                subagent_type="does-not-exist",
                run_in_background=True,
            )
        )

    assert result.is_error
    assert result.brief == "Invalid subagent type"
    assert "Builtin subagent type not found: does-not-exist" in result.message


async def test_agent_tool_background_rejects_invalid_model_alias_before_start(
    agent_tool, runtime, monkeypatch
):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Good at general software engineering tasks.",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )

    called = False

    def fake_create_agent_task(**kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(
            spec=SimpleNamespace(id="a-task-1", kind="agent", description=kwargs["description"]),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="invalid model",
                prompt="do work",
                subagent_type="coder",
                model="does-not-exist",
                run_in_background=True,
            )
        )

    assert result.is_error
    assert result.brief == "Invalid model alias"
    assert "Unknown model alias: does-not-exist" in result.message
    assert called is False


async def test_agent_tool_background_resume_rejects_invalid_model_alias_before_start(
    agent_tool, runtime, monkeypatch
):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Good at general software engineering tasks.",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    called = False

    def fake_create_agent_task(**kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(
            spec=SimpleNamespace(id="a-task-1", kind="agent", description=kwargs["description"]),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)

    runtime.subagent_store.create_instance(
        agent_id="aresumebadmodel",
        description="resume bad model",
        launch_spec=AgentLaunchSpec(
            agent_id="aresumebadmodel",
            subagent_type="coder",
            model_override=None,
            effective_model=None,
        ),
    )

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="resume bad model",
                prompt="continue work",
                resume="aresumebadmodel",
                model="does-not-exist",
                run_in_background=True,
            )
        )

    assert result.is_error
    assert result.brief == "Invalid model alias"
    assert "Unknown model alias: does-not-exist" in result.message
    assert called is False


async def test_agent_tool_background_resume_rejects_stale_effective_model_before_start(
    agent_tool, runtime, monkeypatch
):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Good at general software engineering tasks.",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    called = False

    def fake_create_agent_task(**kwargs):
        nonlocal called
        called = True
        return SimpleNamespace(
            spec=SimpleNamespace(id="a-task-1", kind="agent", description=kwargs["description"]),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)

    runtime.subagent_store.create_instance(
        agent_id="astalemodel",
        description="resume stale model",
        launch_spec=AgentLaunchSpec(
            agent_id="astalemodel",
            subagent_type="coder",
            model_override=None,
            effective_model="removed-model",
        ),
    )

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="resume stale model",
                prompt="continue work",
                resume="astalemodel",
                run_in_background=True,
            )
        )

    assert result.is_error
    assert result.brief == "Invalid model alias"
    assert "Unknown model alias: removed-model" in result.message
    assert called is False


async def test_agent_tool_background_agent_waits_for_approval(agent_tool, runtime, monkeypatch):
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

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        source = get_current_approval_source_or_none()
        assert source is not None
        request = soul.runtime.approval_runtime.create_request(
            request_id="req-bg-approval",
            tool_call_id="call-bg-approval",
            sender="WriteFile",
            action="edit file",
            description="Edit target file",
            display=[],
            source=source,
        )
        await soul.runtime.approval_runtime.wait_for_response(request.id)
        # Use a response >= SUMMARY_MIN_LENGTH to avoid triggering summary continuation.
        await soul.context.append_message(
            Message(role="assistant", content=[TextPart(text="x" * 250)])
        )

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    queue = runtime.root_wire_hub.subscribe()
    try:
        with tool_call_context("Agent"):
            result = await agent_tool(
                agent_tool.params(
                    description="investigate bug",
                    prompt="look into parser issue",
                    run_in_background=True,
                )
            )

        assert not result.is_error
        task_id = _extract_task_id(result.output)
        msg = await queue.get()
        assert isinstance(msg, ApprovalRequest)
        assert msg.id == "req-bg-approval"
        assert msg.source_kind == "background_agent"
        assert msg.source_id == task_id

        import asyncio

        view = None
        # Poll up to 2s (matches the wait timeout below); under parallel
        # suite load the background task can take longer than 200ms to
        # transition into awaiting_approval.
        for _ in range(200):
            view = runtime.background_tasks.get_task(task_id)
            assert view is not None
            if view.runtime.status == "awaiting_approval":
                break
            await asyncio.sleep(0.01)
        assert view is not None
        assert view.runtime.status == "awaiting_approval"

        assert runtime.approval_runtime is not None
        runtime.approval_runtime.resolve("req-bg-approval", "approve")
        waited = await runtime.background_tasks.wait(task_id, timeout_s=2)
        assert waited.runtime.status == "completed"
    finally:
        assert runtime.root_wire_hub is not None
        runtime.root_wire_hub.unsubscribe(queue)


async def test_task_stop_kills_background_agent_waiting_for_approval(
    agent_tool, runtime, monkeypatch, task_stop_tool
):
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

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        source = get_current_approval_source_or_none()
        assert source is not None
        soul.runtime.approval_runtime.create_request(
            request_id="req-bg-stop",
            tool_call_id="call-bg-stop",
            sender="WriteFile",
            action="edit file",
            description="Edit target file",
            display=[],
            source=source,
        )
        await soul.runtime.approval_runtime.wait_for_response("req-bg-stop")

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="investigate bug",
                prompt="look into parser issue",
                run_in_background=True,
            )
        )

    assert not result.is_error
    task_id = _extract_task_id(result.output)

    import asyncio

    view = None
    # Poll up to 2s; under parallel suite load the transition can exceed 200ms.
    for _ in range(200):
        view = runtime.background_tasks.get_task(task_id)
        assert view is not None
        if view.runtime.status == "awaiting_approval":
            break
        await asyncio.sleep(0.01)
    assert view is not None
    assert view.runtime.status == "awaiting_approval"

    stop_result = await task_stop_tool(task_stop_tool.params(task_id=task_id))

    assert not stop_result.is_error
    killed_view = runtime.background_tasks.get_task(task_id)
    assert killed_view is not None
    assert killed_view.runtime.status == "killed"
    assert runtime.approval_runtime.list_pending() == []


async def test_foreground_agent_explicit_timeout_returns_tool_error(
    agent_tool, runtime, monkeypatch
):
    """When the model passes an explicit timeout for a foreground agent and the
    subagent exceeds it, the tool should return a ToolError (not hang)."""
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

    async def fake_run_soul_hang(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        # Simulate a subagent that never finishes
        await asyncio.Event().wait()

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul_hang)

    import time

    params = agent_tool.params(
        description="slow task",
        prompt="do something slow",
        timeout=30,
    )
    # Override to a short timeout so the test doesn't actually wait 30s
    object.__setattr__(params, "timeout", 1)

    start = time.monotonic()
    result = await agent_tool(params)
    elapsed = time.monotonic() - start

    # Should be a ToolError, not a hang; and should finish quickly
    assert result.is_error
    assert "timed out" in result.message.lower()
    assert "1s" in result.message  # Verify the correct timeout value is reported
    assert elapsed < 5.0


async def test_foreground_agent_internal_timeout_with_explicit_deadline(
    agent_tool, runtime, monkeypatch
):
    """When an explicit timeout IS set but an internal TimeoutError fires first,
    it should still be reported as a generic failure, not 'Agent timed out after Xs'."""
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

    async def fake_run_soul_internal_timeout(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        raise TimeoutError("aiohttp sock_read timeout")

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul_internal_timeout)

    params = agent_tool.params(
        description="internal timeout with deadline",
        prompt="do something",
        timeout=600,
    )

    result = await agent_tool(params)

    assert result.is_error
    # Must be generic failure, NOT "Agent timed out after 600s"
    assert "agent timed out after" not in result.message.lower()
    assert "aiohttp sock_read timeout" in result.message


# ---------------------------------------------------------------------------
# run_soul_checked exception handling — ChatProviderError / APIStatusError
# are converted to SoulRunFailure with informative messages.
# ---------------------------------------------------------------------------


async def test_agent_tool_returns_informative_error_when_chat_provider_fails(
    agent_tool, runtime, monkeypatch
):
    """When run_soul raises ChatProviderError, the agent tool should return
    a ToolError with the original error message — not 'Failed to run agent: ...'
    with a potentially empty or cryptic str(exc)."""
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

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        raise ChatProviderError("Model overloaded, please retry")

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    result = await agent_tool(
        agent_tool.params(
            description="chat provider failure",
            prompt="investigate bug",
        )
    )

    assert result.is_error
    # The error message should contain the original ChatProviderError message,
    # and the brief should NOT be the generic "Agent failed".
    assert "Model overloaded" in result.message
    assert result.brief == "LLM provider error"

    # Instance should be marked as failed
    records = [
        r
        for r in runtime.subagent_store.list_instances()
        if r.description == "chat provider failure"
    ]
    assert len(records) == 1
    assert records[0].status == "failed"


async def test_agent_tool_returns_informative_error_when_api_status_error(
    agent_tool, runtime, monkeypatch
):
    """APIStatusError should include status_code in the error message."""
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

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        raise APIStatusError(429, "Rate limit exceeded")

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    result = await agent_tool(
        agent_tool.params(
            description="api status failure",
            prompt="investigate bug",
        )
    )

    assert result.is_error
    assert "429" in result.message
    assert "Rate limit exceeded" in result.message


# ---------------------------------------------------------------------------
# Defensive None check for final_response — returns ToolError instead of
# crashing with AssertionError.
# ---------------------------------------------------------------------------


async def test_agent_tool_returns_error_when_final_response_is_none(
    agent_tool, runtime, monkeypatch
):
    """If run_with_summary_continuation returns (None, None) — an
    impossible-in-theory but defensive scenario — the runner should return
    a ToolError instead of crashing with AssertionError."""
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

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)

    # Patch run_with_summary_continuation to return (None, None) — simulating
    # the defensive scenario where final_response is None but failure is also None.
    async def fake_run_with_summary(soul, prompt, ui_loop_fn, wire_path, **kwargs):
        return None, None

    monkeypatch.setattr(
        "pythinker_code.subagents.runner.run_with_summary_continuation",
        fake_run_with_summary,
    )

    result = await agent_tool(
        agent_tool.params(
            description="none response",
            prompt="investigate bug",
        )
    )

    assert result.is_error
    assert result.message.startswith("Agent completed but produced no output.")
    # subagent-2: a failed child still reports its accumulated (here zero) spend.
    assert "child_tokens:" in result.message


# ---------------------------------------------------------------------------
# RunCancelled sets killed status (not failed) — user Ctrl+C is a cancel,
# not a failure.
# ---------------------------------------------------------------------------


async def test_agent_tool_marks_instance_killed_when_run_cancelled(
    agent_tool, runtime, monkeypatch
):
    """When RunCancelled is raised (user Ctrl+C), the instance should be
    marked as 'killed', not 'failed'."""
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

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        raise RunCancelled()

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    # RunCancelled is caught by AgentTool and returned as ToolError,
    # but the instance must be marked as "killed" (not "failed").
    result = await agent_tool(
        agent_tool.params(
            description="user cancelled",
            prompt="investigate bug",
        )
    )

    assert result.is_error
    records = [
        r for r in runtime.subagent_store.list_instances() if r.description == "user cancelled"
    ]
    assert len(records) == 1
    assert records[0].status == "killed"


# ---------------------------------------------------------------------------
# APIConnectionError goes through ChatProviderError branch
# ---------------------------------------------------------------------------


async def test_agent_tool_returns_informative_error_when_api_connection_error(
    agent_tool, runtime, monkeypatch
):
    """APIConnectionError (a subclass of ChatProviderError) should be caught by
    the ChatProviderError handler in run_soul_checked and returned as a ToolError
    with an informative message."""
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

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        raise APIConnectionError("Connection refused")

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    result = await agent_tool(
        agent_tool.params(
            description="connection failure",
            prompt="investigate bug",
        )
    )

    assert result.is_error
    assert "Connection refused" in result.message
    assert result.brief == "LLM provider error"

    records = [
        r for r in runtime.subagent_store.list_instances() if r.description == "connection failure"
    ]
    assert len(records) == 1
    assert records[0].status == "failed"


# ---------------------------------------------------------------------------
# Background runner: final_response is None returns failure (not assertion crash)
# ---------------------------------------------------------------------------


async def test_background_agent_marks_failed_when_final_response_is_none(
    agent_tool, runtime, monkeypatch
):
    """When the background agent's run_with_summary_continuation returns
    (None, None), the task should be marked as failed — not crash with
    AssertionError."""
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

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)

    async def fake_run_with_summary(soul, prompt, ui_loop_fn, wire_path, **kwargs):
        return None, None

    monkeypatch.setattr(
        "pythinker_code.background.agent_runner.run_with_summary_continuation",
        fake_run_with_summary,
    )

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="bg none response",
                prompt="investigate bug",
                run_in_background=True,
            )
        )

    assert not result.is_error
    task_id = _extract_task_id(result.output)
    agent_id = _extract_agent_id(result.output)

    waited = await runtime.background_tasks.wait(task_id, timeout_s=5)
    assert waited.runtime.status == "failed"

    record = runtime.subagent_store.require_instance(agent_id)
    assert record.status == "failed"


# ---------------------------------------------------------------------------
# Hook trigger exception in foreground runner — try scope covers pre-run code
# ---------------------------------------------------------------------------


async def test_foreground_runner_hook_trigger_exception_marks_instance_failed(
    agent_tool, runtime, monkeypatch
):
    """When hook_engine.trigger(SubagentStart) raises inside the expanded try
    block, the instance should be marked as 'failed' and the finally block
    should not crash even if approval_source was already set."""
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

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)

    # Make subagent_start (called to build input_data for the SubagentStart hook)
    # raise an exception.  This triggers the except Exception branch inside the
    # expanded try block, BEFORE run_with_summary_continuation is reached.
    def exploding_subagent_start(**kwargs):
        raise RuntimeError("hook input builder failed")

    monkeypatch.setattr(
        "pythinker_code.hooks.events.subagent_start",
        exploding_subagent_start,
    )

    result = await agent_tool(
        agent_tool.params(
            description="hook failure",
            prompt="investigate bug",
        )
    )

    assert result.is_error
    # The exception propagates through ForegroundSubagentRunner.run()'s
    # except Exception → raise → AgentTool.__call__()'s except Exception.
    assert "hook input builder failed" in result.message

    # Instance must be marked as failed (not stuck in running_foreground).
    records = [
        r for r in runtime.subagent_store.list_instances() if r.description == "hook failure"
    ]
    assert len(records) == 1
    assert records[0].status == "failed"


# ---------------------------------------------------------------------------
# Background runner: RunCancelled sets killed status (not failed)
# ---------------------------------------------------------------------------


async def test_background_agent_marks_killed_when_run_cancelled(agent_tool, runtime, monkeypatch):
    """When RunCancelled propagates in the background runner, the instance
    should be marked as 'killed' and the task as 'killed' — not 'failed'."""
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

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)

    async def fake_run_with_summary(soul, prompt, ui_loop_fn, wire_path, **kwargs):
        raise RunCancelled()

    monkeypatch.setattr(
        "pythinker_code.background.agent_runner.run_with_summary_continuation",
        fake_run_with_summary,
    )

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="bg run cancelled",
                prompt="investigate bug",
                run_in_background=True,
            )
        )

    assert not result.is_error
    task_id = _extract_task_id(result.output)
    agent_id = _extract_agent_id(result.output)

    # Poll for task completion — RunCancelled is re-raised from run(),
    # so wait() may propagate the exception; use polling instead.
    view = None
    for _ in range(100):
        view = runtime.background_tasks.get_task(task_id)
        assert view is not None
        if view.runtime.status in ("killed", "failed", "completed"):
            break
        await asyncio.sleep(0.05)

    assert view is not None
    assert view.runtime.status == "killed"

    record = runtime.subagent_store.require_instance(agent_id)
    assert record.status == "killed"


# ---------------------------------------------------------------------------
# parent_agent_id propagation
# ---------------------------------------------------------------------------


async def test_agent_tool_records_parent_agent_id_as_none_for_root(
    agent_tool, runtime, monkeypatch
):
    """When the root agent launches a subagent, parent_agent_id is None (root has no ID)."""
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

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        await soul.context.append_message(
            Message(role="assistant", content=[TextPart(text="done")])
        )

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    # Root agent has subagent_id=None
    assert runtime.subagent_id is None

    result = await agent_tool(agent_tool.params(description="task", prompt="do it"))

    assert not result.is_error
    agent_id = _extract_agent_id(result.output)
    record = runtime.subagent_store.require_instance(agent_id)
    assert record.launch_spec.parent_agent_id is None


async def test_agent_tool_background_sets_parent_agent_id(agent_tool, runtime, monkeypatch):
    """When a background agent is launched from the root agent (subagent_id=None),
    the created AgentLaunchSpec must have parent_agent_id=None (root agent)."""
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Good at general software engineering tasks.",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )

    created_specs: list[AgentLaunchSpec] = []

    def fake_create_agent_task(**kwargs):
        # Capture the launch_spec from the subagent store at creation time.
        agent_id = kwargs["agent_id"]
        record = runtime.subagent_store.require_instance(agent_id)
        created_specs.append(record.launch_spec)
        return SimpleNamespace(
            spec=SimpleNamespace(id="a-task-pid", kind="agent", description=kwargs["description"]),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", fake_create_agent_task)

    with tool_call_context("Agent"):
        result = await agent_tool(
            agent_tool.params(
                description="parent id check",
                prompt="do work",
                run_in_background=True,
            )
        )

    assert not result.is_error
    assert len(created_specs) == 1
    # Root agent has subagent_id=None, so parent_agent_id should be None.
    assert created_specs[0].parent_agent_id is None


# ---------------------------------------------------------------------------
# GAP 2: isolation warning for foreground agents
# ---------------------------------------------------------------------------


async def test_agent_tool_rejects_isolation_for_foreground(agent_tool, runtime, monkeypatch):
    """isolation='worktree' on a foreground agent fails fast — proceeding
    unisolated would present degraded behavior as authoritative."""
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="coder",
            description="Coder",
            agent_file=runtime.subagent_store.root / "coder.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )

    async def fake_load_agent(agent_file, runtime, *, mcp_configs, start_mcp_loading=True):
        return SoulAgent(
            name=agent_file.stem, system_prompt="", toolset=EmptyToolset(), runtime=runtime
        )

    async def fake_run_soul(
        soul, user_input, ui_loop_fn, cancel_event, wire_file=None, runtime=None
    ):
        await soul.context.append_message(
            Message(role="assistant", content=[TextPart(text="done")])
        )

    monkeypatch.setattr("pythinker_code.subagents.builder.load_agent", fake_load_agent)
    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", fake_run_soul)

    result = await agent_tool(
        agent_tool.params(
            description="task",
            prompt="do it",
            run_in_background=False,
            isolation="worktree",
        )
    )

    assert result.is_error
    assert "run_in_background" in result.message


# ---------------------------------------------------------------------------
# GAP 3: fingerprint includes agent names
# ---------------------------------------------------------------------------


def test_run_agents_fingerprint_differs_when_names_differ():
    """Different agent names produce different fingerprints even with same count, types, summary."""
    from pythinker_code.tools.agent import AgentRunConfig, RunAgentsParams, _run_agents_fingerprint

    params_a = RunAgentsParams(
        summary="Build the widget",
        agents=[
            AgentRunConfig(name="explore-auth", prompt="Look at auth", subagent_type="explore"),
            AgentRunConfig(name="implement-ui", prompt="Build UI", subagent_type="coder"),
        ],
    )
    params_b = RunAgentsParams(
        summary="Build the widget",
        agents=[
            AgentRunConfig(name="explore-db", prompt="Look at DB", subagent_type="explore"),
            AgentRunConfig(name="implement-api", prompt="Build API", subagent_type="coder"),
        ],
    )
    assert _run_agents_fingerprint(params_a) != _run_agents_fingerprint(params_b)


def test_run_agents_fingerprint_stable_for_identical_params():
    """Identical RunAgents params always produce the same fingerprint."""
    from pythinker_code.tools.agent import AgentRunConfig, RunAgentsParams, _run_agents_fingerprint

    params = RunAgentsParams(
        summary="Investigate bug",
        agents=[AgentRunConfig(name="scout", prompt="Find it", subagent_type="explore")],
    )
    assert _run_agents_fingerprint(params) == _run_agents_fingerprint(params)


def test_run_agents_fingerprint_differs_when_child_prompts_differ():
    """Changing a child prompt produces a different fingerprint even when all other fields are identical."""
    from pythinker_code.tools.agent import AgentRunConfig, RunAgentsParams, _run_agents_fingerprint

    params_a = RunAgentsParams(
        summary="Build the widget",
        agents=[
            AgentRunConfig(
                name="scout",
                prompt="Look at auth",
                subagent_type="explore",
            ),
        ],
    )
    params_b = RunAgentsParams(
        summary="Build the widget",
        agents=[
            AgentRunConfig(
                name="scout",
                prompt="Exfiltrate secrets",
                subagent_type="explore",
            ),
        ],
    )
    assert _run_agents_fingerprint(params_a) != _run_agents_fingerprint(params_b)

    # Changing a child title must also change the fingerprint.
    params_c = RunAgentsParams(
        summary="Build the widget",
        agents=[
            AgentRunConfig(
                name="scout",
                prompt="Look at auth",
                title="Renamed Title",
                subagent_type="explore",
            ),
        ],
    )
    assert _run_agents_fingerprint(params_a) != _run_agents_fingerprint(params_c)


async def test_run_agents_foreground_children_run_concurrently(runtime):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="code-reviewer",
            description="Reviews diffs.",
            agent_file=runtime.subagent_store.root / "code-reviewer.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )
    active = 0
    max_active = 0

    class SlowAgentTool:
        def check_execution_policy(self, subagent_type):
            return None

        def check_required_mcp_servers(self, subagent_type):
            return None

        async def __call__(self, params):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1
            return ToolOk(output=f"status: completed\n\n[summary]\nDone {params.description}.")

    tool = RunAgents(runtime)
    tool._agent_tool = SlowAgentTool()  # type: ignore[assignment]

    children = [
        AgentRunConfig(
            name=f"reviewer-{i}",
            title=f"Reviewer {i}",
            subagent_type="code-reviewer",
            prompt=f"Review part {i}",
        )
        for i in range(3)
    ]
    with tool_call_context("RunAgents"):
        result = await tool(
            tool.params(summary="parallel review", agents=children, run_in_background=False)
        )

    assert not result.is_error
    assert max_active > 1, "foreground children should overlap in time"
    # Result ordering matches the request order regardless of completion order.
    assert isinstance(result.output, str)
    order = [
        line.removeprefix("- name: ").strip()
        for line in result.output.splitlines()
        if line.startswith("- name: ")
    ]
    assert order == ["reviewer-0", "reviewer-1", "reviewer-2"]


async def test_run_agents_foreground_one_failure_does_not_abort_siblings(runtime):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="code-reviewer",
            description="Reviews diffs.",
            agent_file=runtime.subagent_store.root / "code-reviewer.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )

    class FlakyAgentTool:
        def check_execution_policy(self, subagent_type):
            return None

        def check_required_mcp_servers(self, subagent_type):
            return None

        async def __call__(self, params):
            await asyncio.sleep(0.01)
            if "1" in params.description:
                raise RuntimeError("child exploded")
            return ToolOk(output=f"status: completed\n\n[summary]\nDone {params.description}.")

    tool = RunAgents(runtime)
    tool._agent_tool = FlakyAgentTool()  # type: ignore[assignment]

    children = [
        AgentRunConfig(
            name=f"reviewer-{i}",
            title=f"Reviewer {i}",
            subagent_type="code-reviewer",
            prompt=f"Review part {i}",
        )
        for i in range(3)
    ]
    with tool_call_context("RunAgents"):
        result = await tool(
            tool.params(summary="flaky batch", agents=children, run_in_background=False)
        )

    assert result.is_error  # batch reports failure overall
    # But both healthy siblings completed and are present in the report
    # (child entry status lines are indented two spaces).
    assert isinstance(result.output, str)
    child_statuses = [
        line.strip() for line in result.output.splitlines() if line.startswith("  status: ")
    ]
    assert child_statuses.count("status: completed") == 2
    assert child_statuses.count("status: error") == 1
    assert "child exploded" in result.output


async def test_run_agents_foreground_aggregates_child_risks_and_blockers(runtime):
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="code-reviewer",
            description="Reviews diffs.",
            agent_file=runtime.subagent_store.root / "code-reviewer.yaml",
            tool_policy=ToolPolicy(mode="inherit"),
        )
    )

    class ReportingAgentTool:
        def check_execution_policy(self, subagent_type):
            return None

        def check_required_mcp_servers(self, subagent_type):
            return None

        async def __call__(self, params):
            return ToolOk(
                output=(
                    "status: completed\n\n"
                    "### SUMMARY\nDone.\n\n"
                    "### RISKS\n- Shared cache key may collide.\n\n"
                    "### BLOCKERS\nNone\n"
                )
            )

    tool = RunAgents(runtime)
    tool._agent_tool = ReportingAgentTool()  # type: ignore[assignment]

    children = [
        AgentRunConfig(
            name=f"reviewer-{i}",
            title=f"Reviewer {i}",
            subagent_type="code-reviewer",
            prompt=f"Review part {i}",
        )
        for i in range(2)
    ]
    with tool_call_context("RunAgents"):
        result = await tool(
            tool.params(summary="risk batch", agents=children, run_in_background=False)
        )

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "batch_risks:" in result.output
    assert result.output.count("Shared cache key may collide.") >= 1
    assert "reviewer-0, reviewer-1" in result.output
    assert "batch_blockers:" not in result.output
