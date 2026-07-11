from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pythinker_code.agentspec import DEFAULT_AGENT_FILE, load_agent_spec
from pythinker_code.soul.agent import Runtime, load_agent
from pythinker_code.subagents.models import AgentTypeDefinition, ToolPolicy


def _write_yaml_agent_pair(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "root.md").write_text("root prompt", encoding="utf-8")
    (tmp_path / "child.md").write_text("child prompt", encoding="utf-8")
    child = tmp_path / "child.yaml"
    child.write_text(
        "version: 1\n"
        "agent:\n"
        "  name: child-runtime-name\n"
        "  system_prompt_path: ./child.md\n"
        "  when_to_use: Use for compatibility checks.\n"
        "  model: model-a\n"
        "  hidden: true\n"
        "  tools: []\n"
        "  allowed_tools: [pythinker_code.tools.think:Think]\n",
        encoding="utf-8",
    )
    root = tmp_path / "root.yaml"
    root.write_text(
        "version: 1\n"
        "agent:\n"
        "  name: root\n"
        "  system_prompt_path: ./root.md\n"
        "  tools: []\n"
        "  subagents:\n"
        "    Analyst:\n"
        "      path: ./child.yaml\n"
        "      description: Compatibility analyst\n",
        encoding="utf-8",
    )
    return root, child


async def test_load_agent_publishes_exact_warn_catalogue_projection(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    root, child = _write_yaml_agent_pair(tmp_path)

    await load_agent(root, runtime, mcp_configs=[])

    catalogue = runtime.agent_catalogue
    assert catalogue is not None
    entry = catalogue.require("analyst")
    assert catalogue.require("ANALYST") is entry
    assert runtime.labor_market.require_builtin_type("Analyst") == AgentTypeDefinition(
        name="Analyst",
        description="Compatibility analyst",
        agent_file=child,
        when_to_use="Use for compatibility checks.",
        default_model="model-a",
        tool_policy=ToolPolicy(
            mode="allowlist",
            tools=("pythinker_code.tools.think:Think",),
        ),
        supports_background=False,
        required_mcp_servers=(),
    )
    assert runtime.labor_market.get_builtin_type("analyst") is None


async def test_root_and_child_runtimes_share_catalogue_identity(
    runtime: Runtime,
) -> None:
    await load_agent(DEFAULT_AGENT_FILE, runtime, mcp_configs=[])

    child = runtime.copy_for_subagent(agent_id="a1", subagent_type="coder")

    assert child.agent_catalogue is runtime.agent_catalogue
    assert child.labor_market is runtime.labor_market


async def test_empty_markdown_discovery_preserves_wrapper_directory(runtime: Runtime) -> None:
    await load_agent(DEFAULT_AGENT_FILE, runtime, mcp_configs=[])

    assert (runtime.session.dir / "external_agents").is_dir()


async def test_markdown_catalogue_projection_keeps_generated_wrapper_parity(
    runtime: Runtime,
) -> None:
    work_dir = Path(str(runtime.work_dir))
    markdown = work_dir / ".pythinker" / "agents" / "worker.md"
    markdown.parent.mkdir(parents=True)
    markdown.write_text(
        "---\n"
        "name: Worker\n"
        "description: Compatibility worker\n"
        "model: model-a\n"
        "when_to_use: Use for wrapper parity.\n"
        "tools: [Read]\n"
        "required_mcp_servers: [database]\n"
        "---\n"
        "Follow the compatibility prompt.\n",
        encoding="utf-8",
    )

    await load_agent(DEFAULT_AGENT_FILE, runtime, mcp_configs=[])

    catalogue = runtime.agent_catalogue
    assert catalogue is not None
    entry = catalogue.require("worker")
    type_def = runtime.labor_market.require_builtin_type("Worker")
    assert type_def.agent_file == entry.legacy_agent_file
    wrapper_spec = load_agent_spec(type_def.agent_file)
    assert wrapper_spec.name == entry.launch_spec.name
    assert wrapper_spec.system_prompt_path == entry.launch_spec.system_prompt_path
    assert wrapper_spec.system_prompt_args == entry.launch_spec.system_prompt_args
    assert tuple(wrapper_spec.tools) == tuple(entry.launch_spec.tools)
    assert tuple(wrapper_spec.allowed_tools or ()) == tuple(entry.launch_spec.allowed_tools or ())
    assert tuple(wrapper_spec.exclude_tools) == tuple(entry.launch_spec.exclude_tools)
    assert wrapper_spec.model == entry.launch_spec.model
    assert wrapper_spec.steps == entry.launch_spec.steps
    assert type_def.required_mcp_servers == ("database",)
    assert type_def.tool_policy == ToolPolicy(
        mode="allowlist",
        tools=("pythinker_code.tools.file:ReadFile",),
    )


async def test_warn_diagnostics_surface_once_without_raw_source_details(
    runtime: Runtime,
    tmp_path: Path,
) -> None:
    root, _ = _write_yaml_agent_pair(tmp_path)
    text = root.read_text(encoding="utf-8")
    root.write_text(
        text.replace("  tools: []\n", "  tools: []\n  future_option: SECRET\n"), encoding="utf-8"
    )

    with patch("pythinker_code.soul.agent.logger") as mocked_logger:
        await load_agent(root, runtime, mcp_configs=[])
        await load_agent(root, runtime, mcp_configs=[])

    catalogue = runtime.agent_catalogue
    assert catalogue is not None
    assert [diagnostic.severity for diagnostic in catalogue.diagnostics] == ["warning"]
    warnings = [
        call
        for call in mocked_logger.warning.call_args_list
        if call.args and str(call.args[0]).startswith("Agent definition")
    ]
    assert len(warnings) == 1
    rendered = repr(warnings)
    assert "future_option" in rendered
    assert "SECRET" not in rendered
    assert str(tmp_path) not in rendered
