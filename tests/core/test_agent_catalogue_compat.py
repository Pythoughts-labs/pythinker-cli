from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import pytest
from pythinker_core.message import Message
from pythinker_core.tooling import ToolError

from pythinker_code.agentspec import DEFAULT_AGENT_FILE, load_agent_spec
from pythinker_code.soul.agent import (
    Agent as SoulAgent,
)
from pythinker_code.soul.agent import (
    Runtime,
    agent_type_definitions,
    get_agent_type_definition,
    load_agent,
)
from pythinker_code.soul.context import Context
from pythinker_code.soul.dynamic_injections.agent_list import AgentListInjectionProvider
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.subagents.models import AgentTypeDefinition, ToolPolicy
from pythinker_code.subagents.runner import ForegroundRunRequest, ForegroundSubagentRunner
from pythinker_code.tools.agent import AgentTool
from pythinker_code.ui.shell import Shell
from pythinker_code.wire.types import MCPServerSnapshot, MCPStatusSnapshot, TextPart


def _write_yaml_agent_pair(
    tmp_path: Path,
    *,
    default_model: str | None = "model-a",
) -> tuple[Path, Path]:
    (tmp_path / "root.md").write_text("root prompt", encoding="utf-8")
    (tmp_path / "child.md").write_text("child prompt", encoding="utf-8")
    child = tmp_path / "child.yaml"
    child.write_text(
        "version: 1\n"
        "agent:\n"
        "  name: child-runtime-name\n"
        "  system_prompt_path: ./child.md\n"
        "  when_to_use: Use for compatibility checks.\n"
        + (f"  model: {default_model}\n" if default_model is not None else "")
        + "  hidden: true\n"
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
    assert child.agent_type_projection is runtime.agent_type_projection
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


async def test_markdown_compatibility_projection_uses_global_display_name_order(
    runtime: Runtime,
) -> None:
    agents_dir = Path(str(runtime.work_dir)) / ".pythinker" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "a.md").write_text(
        "---\nname: Zed\ndescription: last by display name\n---\nZed prompt\n",
        encoding="utf-8",
    )
    (agents_dir / "b.md").write_text(
        "---\nname: Alpha\ndescription: first by display name\n---\nAlpha prompt\n",
        encoding="utf-8",
    )

    await load_agent(DEFAULT_AGENT_FILE, runtime, mcp_configs=[])

    projected_names = tuple(agent_type_definitions(runtime))
    assert projected_names[-2:] == ("Alpha", "Zed")
    assert tuple(runtime.labor_market.builtin_types)[-2:] == ("Alpha", "Zed")


async def test_catalogue_projection_is_immutable_when_labor_market_diverges(
    runtime: Runtime,
) -> None:
    await load_agent(DEFAULT_AGENT_FILE, runtime, mcp_configs=[])
    before = agent_type_definitions(runtime)
    coder = get_agent_type_definition(runtime, "CODER")
    replacement = AgentTypeDefinition(
        name="coder",
        description="mutated compatibility adapter",
        agent_file=Path("mutated.yaml"),
    )

    runtime.labor_market.add_builtin_type(replacement)
    runtime.labor_market.add_builtin_type(
        AgentTypeDefinition(
            name="late-only",
            description="late compatibility entry",
            agent_file=Path("late.yaml"),
        )
    )

    assert agent_type_definitions(runtime) is before
    assert get_agent_type_definition(runtime, "coder") is coder
    assert "late-only" not in agent_type_definitions(runtime)
    assert runtime.labor_market.require_builtin_type("coder") is replacement
    assert runtime.labor_market.require_builtin_type("late-only").name == "late-only"


async def test_populated_catalogue_drives_casefolded_foreground_launch(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, child_file = _write_yaml_agent_pair(tmp_path, default_model=None)
    await load_agent(root, runtime, mcp_configs=[])

    async def complete_soul(
        soul: PythinkerSoul,
        _prompt: str,
        _ui_loop_fn: object,
        _cancel_event: object,
        **_kwargs: object,
    ) -> None:
        await soul.context.append_message(
            Message(role="assistant", content=[TextPart(text="completed catalogue launch")])
        )

    monkeypatch.setattr("pythinker_code.subagents.runner.run_soul", complete_soul)
    result = await ForegroundSubagentRunner(runtime).run(
        ForegroundRunRequest(
            description="launch analyst",
            prompt="perform compatibility analysis",
            requested_type="aNaLySt",
            model=None,
            resume=None,
        )
    )

    assert not result.is_error
    assert "completed catalogue launch" in result.output
    projected = get_agent_type_definition(runtime, "ANALYST")
    assert projected is not None
    assert projected.agent_file == child_file
    assert projected.supports_background is False
    assert projected.tool_policy.tools == ("pythinker_code.tools.think:Think",)


async def test_populated_catalogue_drives_agent_list_injection(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _write_yaml_agent_pair(tmp_path, default_model=None)
    agent = await load_agent(root, runtime, mcp_configs=[])
    soul = PythinkerSoul(
        agent,
        context=Context(file_backend=tmp_path / "catalogue-agent-list.jsonl"),
    )
    monkeypatch.setattr(
        "pythinker_code.soul.dynamic_injections.agent_list.wire_send",
        lambda _message: None,
    )
    injections = await AgentListInjectionProvider().get_injections([], soul)
    assert len(injections) == 1
    assert "`Analyst`: Compatibility analyst" in injections[0].content


async def _load_markdown_worker(runtime: Runtime) -> SoulAgent:
    work_dir = Path(str(runtime.work_dir))
    markdown = work_dir / ".pythinker" / "agents" / "worker.md"
    markdown.parent.mkdir(parents=True)
    markdown.write_text(
        "---\n"
        "name: Worker\n"
        "description: Catalogue worker\n"
        "tools: [Read]\n"
        "required_mcp_servers: [database]\n"
        "---\n"
        "Use the generated wrapper.\n",
        encoding="utf-8",
    )
    return await load_agent(DEFAULT_AGENT_FILE, runtime, mcp_configs=[])


async def test_populated_markdown_catalogue_drives_required_mcp_gate(runtime: Runtime) -> None:
    await _load_markdown_worker(runtime)
    tool = AgentTool(runtime)
    runtime.mcp_status = lambda: MCPStatusSnapshot(
        loading=False,
        connected=0,
        total=1,
        tools=0,
        servers=(MCPServerSnapshot(name="database", status="failed"),),
    )

    mcp_error = tool.check_required_mcp_servers("wOrKeR")
    assert isinstance(mcp_error, ToolError)
    assert "database" in mcp_error.message


async def test_populated_markdown_catalogue_drives_casefolded_background_launch(
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _load_markdown_worker(runtime)
    tool = AgentTool(runtime)
    runtime.mcp_status = lambda: MCPStatusSnapshot(
        loading=False,
        connected=1,
        total=1,
        tools=1,
        servers=(MCPServerSnapshot(name="database", status="connected"),),
    )
    created: list[dict[str, object]] = []

    def create_agent_task(**kwargs: object) -> SimpleNamespace:
        created.append(kwargs)
        return SimpleNamespace(
            spec=SimpleNamespace(id="catalogue-task", kind="agent", description="worker"),
            runtime=SimpleNamespace(status="starting"),
        )

    monkeypatch.setattr(runtime.background_tasks, "create_agent_task", create_agent_task)
    from pythinker_code.soul.toolset import current_tool_call
    from pythinker_code.wire.types import ToolCall

    token = current_tool_call.set(
        ToolCall(id="test", function=ToolCall.FunctionBody(name="Agent", arguments="{}"))
    )
    try:
        result = await tool(
            tool.params(
                description="launch worker",
                prompt="use the generated wrapper",
                subagent_type="wOrKeR",
                run_in_background=True,
            )
        )
    finally:
        current_tool_call.reset(token)

    assert not result.is_error
    assert created and created[0]["subagent_type"] == "wOrKeR"


async def test_populated_markdown_catalogue_keeps_wrapper_and_tool_policy(runtime: Runtime) -> None:
    await _load_markdown_worker(runtime)
    worker = get_agent_type_definition(runtime, "worker")
    assert worker is not None
    assert worker.agent_file.name.endswith(".yaml")
    assert worker.tool_policy == ToolPolicy(
        mode="allowlist",
        tools=("pythinker_code.tools.file:ReadFile",),
    )


async def test_populated_markdown_catalogue_drives_agents_slash_output(
    runtime: Runtime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent = await _load_markdown_worker(runtime)
    from pythinker_code.ui.shell.slash import registry as shell_slash_registry

    command = shell_slash_registry.find_command("agents")
    assert command is not None
    slash_soul = PythinkerSoul(
        agent,
        context=Context(file_backend=Path(str(runtime.work_dir)) / "catalogue-slash-context.jsonl"),
    )
    command.func(cast("Shell", SimpleNamespace(soul=slash_soul)), "")
    rendered = capsys.readouterr().out
    assert "Worker" in rendered
    assert "allow 1" in rendered


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
