from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest
from pythinker_core.tooling.empty import EmptyToolset

from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.dynamic_injection import DynamicInjection
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.subagents.models import AgentTypeDefinition, ToolPolicy
from pythinker_code.subagents.registry import LaborMarket
from pythinker_code.wire.types import AgentListDelta


def _type(
    name: str,
    when: str = "",
    tools: tuple[str, ...] = (),
) -> AgentTypeDefinition:
    return AgentTypeDefinition(
        name=name,
        description=f"{name} agent",
        agent_file=Path(f"/tmp/{name}.yaml"),
        when_to_use=when,
        tool_policy=ToolPolicy(mode="allowlist", tools=tools)
        if tools
        else ToolPolicy(mode="inherit"),
    )


def test_format_agent_line_allowlist_only() -> None:
    from pythinker_code.soul.dynamic_injections.agent_list import format_agent_line

    line = format_agent_line(
        _type("explore", "Use for reconnaissance", ("pkg.tools:ReadFile", "pkg.tools:Glob"))
    )

    assert "`explore`" in line
    assert "Use for reconnaissance" in line
    assert "Tools: ReadFile, Glob" in line


def test_format_agent_line_no_restrictions() -> None:
    from pythinker_code.soul.dynamic_injections.agent_list import format_agent_line

    line = format_agent_line(_type("coder", "Use for implementation"))

    assert "Tools: *" in line


def test_agent_type_projects_to_literal_prompt_and_wire_contract(tmp_path: Path) -> None:
    from pythinker_code.soul.dynamic_injections.agent_list import format_agent_line

    type_definition = AgentTypeDefinition(
        name="reviewer",
        description="Checks compatibility",
        agent_file=tmp_path / "reviewer.yaml",
        when_to_use="  Use   after changes.  ",
        default_model="characterized-model",
        tool_policy=ToolPolicy(
            mode="allowlist",
            tools=(
                "package.alpha:ReadFile",
                "package.beta:ReadFile",
                "package.beta:Glob",
            ),
        ),
        supports_background=False,
        required_mcp_servers=("context7",),
    )

    assert format_agent_line(type_definition) == (
        "- `reviewer`: Checks compatibility (Tools: ReadFile, Glob). "
        "When to use: Use after changes."
    )


async def test_provider_projects_literal_agent_type_without_field_drift(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pythinker_code.soul.dynamic_injections.agent_list import AgentListInjectionProvider

    labor_market = LaborMarket()
    labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="reviewer",
            description="Checks compatibility",
            agent_file=tmp_path / "reviewer.yaml",
            when_to_use="Use after changes.",
            default_model="characterized-model",
            tool_policy=ToolPolicy(
                mode="allowlist",
                tools=("package.alpha:ReadFile", "package.beta:Glob"),
            ),
            supports_background=False,
            required_mcp_servers=("context7",),
        )
    )
    runtime = dataclasses.replace(runtime, labor_market=labor_market)
    agent = Agent(
        name="Agent List Contract",
        system_prompt="Agent list prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = PythinkerSoul(
        agent,
        context=Context(file_backend=tmp_path / "agent-list-context.jsonl"),
    )
    captured: list[object] = []
    monkeypatch.setattr(
        "pythinker_code.soul.dynamic_injections.agent_list.wire_send",
        lambda message: captured.append(message),
    )
    injections = await AgentListInjectionProvider().get_injections([], soul)

    line = "- `reviewer`: Checks compatibility (Tools: ReadFile, Glob). When to use: Use after changes."
    assert injections == [
        DynamicInjection(
            type="agent_list",
            content="Available agent types (regenerated when subagent specs change):\n" + line,
        )
    ]
    assert captured == [AgentListDelta(items=(line,), complete=True)]


async def test_provider_emits_root_agent_list_and_wire_delta(runtime: Runtime, monkeypatch) -> None:
    from pythinker_code.soul.dynamic_injections.agent_list import AgentListInjectionProvider

    runtime.labor_market.add_builtin_type(_type("explore", "Use for reconnaissance"))
    captured: list[object] = []
    monkeypatch.setattr(
        "pythinker_code.soul.dynamic_injections.agent_list.wire_send",
        lambda msg: captured.append(msg),
    )
    soul = SimpleNamespace(runtime=runtime, is_subagent=False)

    injections = await AgentListInjectionProvider().get_injections([], soul)  # type: ignore[arg-type]

    assert len(injections) == 1
    assert injections[0].type == "agent_list"
    assert "`explore`" in injections[0].content
    assert captured and isinstance(captured[0], AgentListDelta)


async def test_provider_is_root_only(runtime: Runtime) -> None:
    from pythinker_code.soul.dynamic_injections.agent_list import AgentListInjectionProvider

    soul = SimpleNamespace(runtime=runtime, is_subagent=True)

    assert await AgentListInjectionProvider().get_injections([], soul) == []  # type: ignore[arg-type]
