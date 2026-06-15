from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pythinker_code.soul.agent import Runtime
from pythinker_code.subagents.models import AgentTypeDefinition, ToolPolicy
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
