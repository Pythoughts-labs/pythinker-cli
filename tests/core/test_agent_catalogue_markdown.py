from __future__ import annotations

from pathlib import Path

import pytest
from pythinker_host.path import HostPath

from pythinker_code.subagents.catalogue import UnknownFieldPolicy, resolve_agent_catalogue
from pythinker_code.subagents.discovery import AgentScope, ScopedAgentRoot


def _root_agent(tmp_path: Path) -> Path:
    root = tmp_path / "root.yaml"
    (tmp_path / "system.md").write_text("root", encoding="utf-8")
    root.write_text(
        "version: 1\nagent:\n  name: root\n  system_prompt_path: ./system.md\n  tools: []\n",
        encoding="utf-8",
    )
    return root


def _markdown_root(path: Path, scope: AgentScope = "project") -> ScopedAgentRoot:
    path.mkdir(parents=True, exist_ok=True)
    return ScopedAgentRoot(
        root=HostPath.unsafe_from_local_path(path),
        scope=scope,
    )


def _write_markdown(path: Path, *, name: str, description: str, extra: str = "") -> None:
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\nPrompt for {name}\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_markdown_warns_once_then_preserves_resolved_launch_semantics(tmp_path: Path) -> None:
    agents = _markdown_root(tmp_path / ".pythinker" / "agents")
    _write_markdown(
        Path(str(agents.root)) / "worker.md",
        name="Worker",
        description="worker",
        extra=(
            "tools: [Read, Bash]\n"
            "disallowed_tools: [Bash]\n"
            "required_mcp_servers: [db]\n"
            "max_turns: 4\n"
            "unknown_beta: SECRET_VALUE\n"
            "unknown_alpha: other\n"
        ),
    )

    catalogue = await resolve_agent_catalogue(
        agent_file=_root_agent(tmp_path),
        markdown_roots=(agents,),
        materialized_dir=tmp_path / "generated",
        unknown_field_policy=UnknownFieldPolicy.WARN,
    )

    entry = catalogue.require("worker")
    assert entry.required_mcp_servers == ("db",)
    assert entry.launch_spec.steps == 4
    assert tuple(entry.launch_spec.allowed_tools or ()) == (
        "pythinker_code.tools.file:ReadFile",
        "pythinker_code.tools.shell:Shell",
    )
    assert tuple(entry.launch_spec.exclude_tools) == ("pythinker_code.tools.shell:Shell",)
    assert [d.field_path for d in catalogue.diagnostics] == ["unknown_alpha, unknown_beta"]
    rendered = repr(catalogue.diagnostics)
    assert "SECRET_VALUE" not in rendered
    assert str(tmp_path) not in rendered


@pytest.mark.asyncio
async def test_markdown_forbid_skips_only_invalid_optional_entry(tmp_path: Path) -> None:
    agents = _markdown_root(tmp_path / "agents")
    local = Path(str(agents.root))
    _write_markdown(local / "bad.md", name="bad", description="bad", extra="typo: secret\n")
    _write_markdown(local / "good.md", name="good", description="good")

    catalogue = await resolve_agent_catalogue(
        agent_file=_root_agent(tmp_path),
        markdown_roots=(agents,),
        materialized_dir=tmp_path / "generated",
        unknown_field_policy=UnknownFieldPolicy.FORBID,
    )

    assert [entry.name for entry in catalogue.values()] == ["good"]
    assert [(d.field_path, d.severity) for d in catalogue.diagnostics] == [("typo", "error")]


@pytest.mark.asyncio
async def test_markdown_same_precedence_is_deterministic_first_wins_with_warning(
    tmp_path: Path,
) -> None:
    agents = _markdown_root(tmp_path / "agents")
    local = Path(str(agents.root))
    _write_markdown(local / "z.md", name="helper", description="second")
    _write_markdown(local / "a.md", name="HELPER", description="first")

    catalogue = await resolve_agent_catalogue(
        agent_file=_root_agent(tmp_path),
        markdown_roots=(agents,),
        materialized_dir=tmp_path / "generated",
    )

    assert catalogue.require("helper").description == "first"
    assert [d.reason_code for d in catalogue.diagnostics] == ["same_precedence_collision"]


@pytest.mark.asyncio
async def test_higher_precedence_markdown_shadows_lower_source(tmp_path: Path) -> None:
    high = _markdown_root(tmp_path / "high")
    low = _markdown_root(tmp_path / "low", "plugin")
    _write_markdown(Path(str(high.root)) / "helper.md", name="helper", description="project")
    _write_markdown(Path(str(low.root)) / "helper.md", name="helper", description="plugin")

    catalogue = await resolve_agent_catalogue(
        agent_file=_root_agent(tmp_path),
        markdown_roots=(high, low),
        materialized_dir=tmp_path / "generated",
    )

    assert catalogue.require("HELPER").description == "project"
    assert [d.reason_code for d in catalogue.diagnostics] == ["shadowed_source"]


@pytest.mark.asyncio
async def test_reversed_roots_keep_project_precedence_and_catalogue_order(tmp_path: Path) -> None:
    project = _markdown_root(tmp_path / ".claude" / "agents")
    plugin = _markdown_root(tmp_path / "plugin" / "agents", "plugin")
    _write_markdown(
        Path(str(project.root)) / "helper.md",
        name="helper",
        description="project",
    )
    _write_markdown(
        Path(str(plugin.root)) / "helper.md",
        name="HELPER",
        description="plugin",
    )

    forward = await resolve_agent_catalogue(
        agent_file=_root_agent(tmp_path),
        markdown_roots=(project, plugin),
        materialized_dir=tmp_path / "forward",
    )
    reversed_catalogue = await resolve_agent_catalogue(
        agent_file=_root_agent(tmp_path),
        markdown_roots=(plugin, project),
        materialized_dir=tmp_path / "reversed",
    )

    assert forward.require("helper").description == "project"
    assert reversed_catalogue.require("helper").description == "project"
    assert [entry.normalized_name for entry in forward.values()] == [
        entry.normalized_name for entry in reversed_catalogue.values()
    ]


@pytest.mark.asyncio
async def test_all_supported_markdown_fields_and_aliases_survive_launch_projection(
    tmp_path: Path,
) -> None:
    agents = _markdown_root(tmp_path / "agents")
    _write_markdown(
        Path(str(agents.root)) / "worker.md",
        name="worker",
        description="worker",
        extra=(
            "model: model-a\n"
            "when_to_use: use deliberately\n"
            "tools: [Read]\n"
            "exclude_tools: [Write]\n"
            "steps: 6\n"
            "required_mcp_servers: [db]\n"
        ),
    )

    catalogue = await resolve_agent_catalogue(
        agent_file=_root_agent(tmp_path),
        markdown_roots=(agents,),
        materialized_dir=tmp_path / "generated",
        available_models={"model-a"},
    )

    entry = catalogue.require("worker")
    assert catalogue.diagnostics == ()
    assert entry.launch_spec.model == "model-a"
    assert entry.launch_spec.when_to_use == "use deliberately"
    assert entry.launch_spec.steps == 6
    assert tuple(entry.launch_spec.allowed_tools or ()) == ("pythinker_code.tools.file:ReadFile",)
    assert tuple(entry.launch_spec.exclude_tools) == ("pythinker_code.tools.file:WriteFile",)
    assert entry.required_mcp_servers == ("db",)


@pytest.mark.asyncio
async def test_malformed_markdown_isolated_with_safe_diagnostic(tmp_path: Path) -> None:
    agents = _markdown_root(tmp_path / "agents")
    local = Path(str(agents.root))
    (local / "bad.md").write_text("---\nname: [unterminated\n---\n", encoding="utf-8")
    _write_markdown(local / "good.md", name="good", description="good")

    catalogue = await resolve_agent_catalogue(
        agent_file=_root_agent(tmp_path),
        markdown_roots=(agents,),
        materialized_dir=tmp_path / "generated",
    )

    assert [entry.name for entry in catalogue.values()] == ["good"]
    assert [d.reason_code for d in catalogue.diagnostics] == ["invalid_known_field"]
    assert str(tmp_path) not in repr(catalogue.diagnostics)
