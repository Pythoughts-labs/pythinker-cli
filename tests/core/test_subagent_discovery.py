from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pythinker_host.path import HostPath

from pythinker_code.agentspec import DEFAULT_AGENT_FILE
from pythinker_code.soul.agent import load_agent
from pythinker_code.subagents.discovery import (
    MarkdownAgentSpec,
    discover_markdown_agents,
    materialize_markdown_agent_specs,
    parse_markdown_agent,
    resolve_agent_roots,
)


def _write_agent(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_parse_markdown_agent_maps_max_turns_and_disallowed_tools(tmp_path: Path) -> None:
    path = tmp_path / "worker.md"
    spec = parse_markdown_agent(
        """---
name: worker
description: Scoped worker
max_turns: 12
disallowed_tools: ["Write", "Bash"]
---
Body
""",
        prompt_file=HostPath.unsafe_from_local_path(path),
        scope="project",
    )
    assert spec.steps == 12
    assert spec.exclude_tools == (
        "pythinker_code.tools.file:WriteFile",
        "pythinker_code.tools.shell:Shell",
    )
    [type_def] = materialize_markdown_agent_specs([spec], output_dir=tmp_path / "out2")
    payload = yaml.safe_load(type_def.agent_file.read_text(encoding="utf-8"))
    assert payload["agent"]["steps"] == 12
    assert payload["agent"]["exclude_tools"] == list(spec.exclude_tools or ())


@pytest.mark.asyncio
async def test_parse_markdown_agent_maps_claude_tools(tmp_path: Path) -> None:
    path = tmp_path / "planner.md"
    spec = parse_markdown_agent(
        """---
name: planner
description: Planning helper
tools: ["Read", "Grep", "Glob", "Bash", "UnknownThing"]
model: opus
---
Body prompt
""",
        prompt_file=HostPath.unsafe_from_local_path(path),
        scope="project",
    )

    assert spec.name == "planner"
    assert spec.description == "Planning helper"
    assert spec.model == "opus"
    assert spec.tools == (
        "pythinker_code.tools.file:ReadFile",
        "pythinker_code.tools.file:Grep",
        "pythinker_code.tools.file:Glob",
        "pythinker_code.tools.shell:Shell",
    )


def test_parse_and_materialize_required_mcp_servers(tmp_path: Path) -> None:
    """A `required_mcp_servers` frontmatter list flows through parse -> materialize onto the
    AgentTypeDefinition the spawn gate reads; an absent value yields an empty tuple."""
    spec = parse_markdown_agent(
        '---\nname: dba\ndescription: DB agent\nrequired_mcp_servers: ["postgres", "redis"]\n---\nBody\n',
        prompt_file=HostPath.unsafe_from_local_path(tmp_path / "dba.md"),
        scope="project",
    )
    assert spec.required_mcp_servers == ("postgres", "redis")
    [type_def] = materialize_markdown_agent_specs([spec], output_dir=tmp_path / "out")
    assert type_def.required_mcp_servers == ("postgres", "redis")

    plain = parse_markdown_agent(
        "---\nname: plain\ndescription: x\n---\nBody\n",
        prompt_file=HostPath.unsafe_from_local_path(tmp_path / "plain.md"),
        scope="project",
    )
    assert plain.required_mcp_servers == ()


def test_required_mcp_servers_drops_non_string_values(tmp_path: Path) -> None:
    """Non-string YAML values (int, bool, null) must be silently dropped; only real strings
    are retained.  Previously str(s) coerced them into names like '1', 'False', 'None'."""
    spec = parse_markdown_agent(
        "---\nname: mixed\ndescription: mixed types\nrequired_mcp_servers: [my-server, 1, false, null]\n---\nBody\n",
        prompt_file=HostPath.unsafe_from_local_path(tmp_path / "mixed.md"),
        scope="project",
    )
    assert spec.required_mcp_servers == ("my-server",)


@pytest.mark.asyncio
async def test_discover_project_claude_agents_from_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "pkg" / "src"
    (repo / ".git").mkdir(parents=True)
    nested.mkdir(parents=True)
    _write_agent(
        repo / ".claude" / "agents" / "observer.md",
        "---\nname: observer\ndescription: Observe code\n---\nPrompt",
    )

    roots = await resolve_agent_roots(HostPath.unsafe_from_local_path(nested))
    agents = await discover_markdown_agents(roots)

    assert [agent.name for agent in agents] == ["observer"]
    assert agents[0].scope == "project"


@pytest.mark.asyncio
async def test_project_markdown_agent_root_priority_same_name(tmp_path: Path) -> None:
    work = tmp_path / "repo"
    (work / ".git").mkdir(parents=True)
    _write_agent(
        work / ".agents" / "agents" / "helper.md",
        "---\nname: helper\ndescription: generic helper\n---\nGeneric",
    )
    _write_agent(
        work / ".claude" / "agents" / "helper.md",
        "---\nname: helper\ndescription: claude helper\n---\nClaude",
    )

    agents = await discover_markdown_agents(
        await resolve_agent_roots(HostPath.unsafe_from_local_path(work))
    )

    assert len(agents) == 1
    assert agents[0].description == "claude helper"
    assert agents[0].scope == "project"


@pytest.mark.asyncio
async def test_materialize_markdown_agent_specs_creates_agent_type(tmp_path: Path) -> None:
    prompt = tmp_path / "agent.md"
    _write_agent(prompt, "---\nname: local-plan\ndescription: plan things\n---\nPrompt")
    spec = parse_markdown_agent(
        prompt.read_text(encoding="utf-8"),
        prompt_file=HostPath.unsafe_from_local_path(prompt),
        scope="project",
    )

    [type_def] = materialize_markdown_agent_specs([spec], output_dir=tmp_path / "out")

    assert type_def.name == "local-plan"
    assert type_def.description == "plan things"
    assert type_def.agent_file.is_file()
    wrapper = type_def.agent_file.read_text(encoding="utf-8")
    assert "system_prompt_path" in wrapper
    system_md = type_def.agent_file.with_name(type_def.agent_file.stem + ".system.md")
    assert system_md.read_text(encoding="utf-8") == "Prompt"


@pytest.mark.asyncio
async def test_load_agent_registers_project_markdown_agents(runtime) -> None:
    work = Path(str(runtime.session.work_dir))
    _write_agent(
        work / ".claude" / "agents" / "planner.md",
        '---\nname: markdown-planner\ndescription: Markdown planner\ntools: ["Read", "Grep"]\n---\nPlan carefully.',
    )

    agent = await load_agent(DEFAULT_AGENT_FILE, runtime, mcp_configs=[])
    type_def = runtime.labor_market.require_builtin_type("markdown-planner")
    agent_tool = next(tool for tool in agent.toolset.tools if tool.name == "Agent")

    assert "`markdown-planner`: Markdown planner" in agent_tool.description
    assert type_def.description == "Markdown planner"
    assert type_def.tool_policy.mode == "allowlist"
    assert type_def.tool_policy.tools == (
        "pythinker_code.tools.file:ReadFile",
        "pythinker_code.tools.file:Grep",
    )


def test_materialize_warns_on_unknown_model(tmp_path: Path) -> None:
    """Unknown model in markdown agent frontmatter logs a warning and falls back."""
    agent = MarkdownAgentSpec(
        name="my-agent",
        description="Does stuff",
        prompt_file=HostPath.unsafe_from_local_path(tmp_path / "my-agent.md"),
        scope="project",
        tools=None,
        model="nonexistent-model-xyz",
        when_to_use="When needed",
    )
    (tmp_path / "my-agent.md").write_text("---\nname: my-agent\n---\nDo the thing.")

    with patch("pythinker_code.subagents.discovery.logger") as mock_logger:
        type_defs = materialize_markdown_agent_specs(
            [agent],
            output_dir=tmp_path / "out",
            available_models={"claude-sonnet", "claude-haiku"},
        )

    assert type_defs[0].default_model is None
    mock_logger.warning.assert_called_once()
    warning_call_str = str(mock_logger.warning.call_args)
    assert "nonexistent-model-xyz" in warning_call_str


def test_materialize_distinct_names_do_not_collide_on_filename(tmp_path: Path) -> None:
    """'a b' and 'a/b' formerly both mapped to 'a_b.yaml'; with hash suffix they must differ."""
    prompt1 = tmp_path / "agent1.md"
    prompt2 = tmp_path / "agent2.md"
    prompt1.write_text("Prompt for agent one", encoding="utf-8")
    prompt2.write_text("Prompt for agent two", encoding="utf-8")

    spec1 = MarkdownAgentSpec(
        name="a b",
        description="Agent A B",
        prompt_file=HostPath.unsafe_from_local_path(prompt1),
        scope="project",
    )
    spec2 = MarkdownAgentSpec(
        name="a/b",
        description="Agent A/B",
        prompt_file=HostPath.unsafe_from_local_path(prompt2),
        scope="project",
    )

    type_defs = materialize_markdown_agent_specs([spec1, spec2], output_dir=tmp_path / "out")

    assert len(type_defs) == 2
    assert type_defs[0].agent_file != type_defs[1].agent_file

    system_md_1 = (
        type_defs[0]
        .agent_file.with_suffix("")
        .with_suffix("")
        .with_name(type_defs[0].agent_file.stem + ".system.md")
    )
    system_md_2 = (
        type_defs[1]
        .agent_file.with_suffix("")
        .with_suffix("")
        .with_name(type_defs[1].agent_file.stem + ".system.md")
    )
    assert system_md_1.exists()
    assert system_md_2.exists()
    assert system_md_1.read_text(encoding="utf-8") == "Prompt for agent one"
    assert system_md_2.read_text(encoding="utf-8") == "Prompt for agent two"


def test_materialize_no_warning_when_model_is_valid(tmp_path: Path) -> None:
    """Valid model in markdown agent frontmatter does not log a warning."""
    agent = MarkdownAgentSpec(
        name="my-agent",
        description="Does stuff",
        prompt_file=HostPath.unsafe_from_local_path(tmp_path / "my-agent.md"),
        scope="project",
        tools=None,
        model="claude-sonnet",
        when_to_use="When needed",
    )
    (tmp_path / "my-agent.md").write_text("---\nname: my-agent\n---\nDo the thing.")

    with patch("pythinker_code.subagents.discovery.logger") as mock_logger:
        type_defs = materialize_markdown_agent_specs(
            [agent],
            output_dir=tmp_path / "out",
            available_models={"claude-sonnet", "claude-haiku"},
        )

    assert type_defs[0].default_model == "claude-sonnet"
    mock_logger.warning.assert_not_called()
