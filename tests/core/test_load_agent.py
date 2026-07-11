"""Tests for agent loading functionality."""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from inline_snapshot import snapshot
from pythinker_host.path import HostPath

from pythinker_code.config import Config
from pythinker_code.exception import InvalidToolError, SystemPromptTemplateError
from pythinker_code.session import Session
from pythinker_code.soul.agent import (
    BuiltinSystemPromptArgs,
    Runtime,
    _load_system_prompt,
    load_agent,
)
from pythinker_code.soul.approval import Approval
from pythinker_code.soul.denwarenji import DenwaRenji
from pythinker_code.soul.toolset import PythinkerToolset
from pythinker_code.subagents.models import AgentTypeDefinition, ToolPolicy
from pythinker_code.utils.environment import Environment


def test_load_system_prompt(system_prompt_file: Path, builtin_args: BuiltinSystemPromptArgs):
    """Test loading system prompt with template substitution."""
    prompt = _load_system_prompt(system_prompt_file, {"CUSTOM_ARG": "test_value"}, builtin_args)

    assert "Test system prompt with " in prompt
    assert "1970-01-01" in prompt  # Should contain the actual timestamp
    assert builtin_args.PYTHINKER_NOW in prompt
    assert "test_value" in prompt


def test_system_prompt_contains_platform_info(builtin_args: BuiltinSystemPromptArgs):
    """System prompt should contain OS and shell information (issue #1649).

    On Windows, the model needs to know it's on Windows so it doesn't
    generate Linux commands. The platform info must be in the system prompt,
    not just in tool descriptions.
    """
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE

    prompt = _load_system_prompt(
        DEFAULT_AGENT_FILE.parent / "system.md",
        {"ROLE_ADDITIONAL": ""},
        builtin_args,
    )

    # System prompt must include OS kind and shell info
    assert builtin_args.PYTHINKER_OS in prompt
    assert builtin_args.PYTHINKER_SHELL in prompt


async def test_render_agent_system_prompt_builds_args_without_runtime(
    temp_work_dir: HostPath, config: Config
) -> None:
    """`render_agent_system_prompt` renders the real default-agent prompt with live
    args (work dir, OS, shell, now) substituted — read-only, with no Runtime, session,
    auth, or MCP. This is the core backing the `pythinker system-prompt` dump command.
    """
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE
    from pythinker_code.soul.agent import render_agent_system_prompt

    prompt = await render_agent_system_prompt(DEFAULT_AGENT_FILE, temp_work_dir, config)

    # Static section proves the template rendered.
    assert "## 1. Identity" in prompt
    # ${PYTHINKER_WORK_DIR} substitution proves the builtin args were built live.
    assert str(temp_work_dir) in prompt
    # StrictUndefined raises on any missing arg, so a clean render with no leftover
    # ${PYTHINKER_*} placeholder proves every dynamic section was supplied.
    assert "${PYTHINKER_" not in prompt


@pytest.mark.asyncio
async def test_system_prompt_skill_policy_is_static_and_omits_catalogue_paths(
    temp_work_dir: HostPath,
    config: Config,
) -> None:
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE
    from pythinker_code.soul.agent import render_agent_system_prompt

    first = await render_agent_system_prompt(DEFAULT_AGENT_FILE, temp_work_dir, config)
    second = await render_agent_system_prompt(DEFAULT_AGENT_FILE, temp_work_dir, config)

    first_skills = first.split("## 12. Skills", 1)[1]
    second_skills = second.split("## 12. Skills", 1)[1]
    assert first_skills == second_skills
    assert "Task-relevant skill candidates arrive with each request" in first_skills
    assert "Path:" not in first_skills


def test_render_agents_md_reminder_present(builtin_args: BuiltinSystemPromptArgs):
    """The merged AGENTS.md renders as an authoritative, fenced <system-reminder> body.

    AGENTS.md is delivered as a session-start preamble (a user-role system-reminder),
    not baked into the system prompt — see render_agents_md_reminder / RequestAssembler.
    """
    from pythinker_code.soul.agent import render_agents_md_reminder

    body = render_agents_md_reminder(builtin_args)
    assert body is not None
    # The merged content is carried verbatim inside the fence (never truncated).
    assert "Test agents content" in body
    assert builtin_args.PYTHINKER_AGENTS_MD_FENCE in body
    # Framed as authoritative so the model follows it like its system instructions.
    assert "authoritative" in body.lower()


def test_render_agents_md_reminder_absent_returns_none(builtin_args: BuiltinSystemPromptArgs):
    """No AGENTS.md between project root and work dir → no reminder (preamble omitted)."""
    from dataclasses import replace

    from pythinker_code.soul.agent import render_agents_md_reminder

    empty = replace(builtin_args, PYTHINKER_AGENTS_MD="")
    assert render_agents_md_reminder(empty) is None


def test_system_prompt_does_not_embed_agents_md(builtin_args: BuiltinSystemPromptArgs):
    """The merged AGENTS.md is delivered as a separate session-start reminder, so §11 of the
    system prompt explains AGENTS.md but no longer interpolates the merged block itself."""
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE

    prompt = _load_system_prompt(
        DEFAULT_AGENT_FILE.parent / "system.md",
        {"ROLE_ADDITIONAL": ""},
        builtin_args,
    )
    # The merged content itself is no longer baked into the system prompt.
    assert "Test agents content" not in prompt
    # §11 still orients the agent: it names AGENTS.md and points at the separate delivery.
    assert "AGENTS.md" in prompt
    assert "delivered as a separate" in prompt.lower()
    # Deeper-directory guidance survives so the agent still seeks more-specific files.
    assert "below the working directory" in prompt


async def test_render_agent_system_prompt_appends_agents_md_reminder(
    temp_work_dir: HostPath, config: Config
) -> None:
    """The dump stays faithful: when an AGENTS.md applies, `pythinker system-prompt` shows
    BOTH the system prompt and the session-start AGENTS.md reminder, labeled as separate."""
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE
    from pythinker_code.soul.agent import render_agent_system_prompt

    await (temp_work_dir / "AGENTS.md").write_text("PROJECT_RULE: always lint before commit.")

    dump = await render_agent_system_prompt(DEFAULT_AGENT_FILE, temp_work_dir, config)

    # The system-prompt portion is present...
    assert "## 1. Identity" in dump
    # ...and the AGENTS.md content is appended as the session-start reminder.
    assert "PROJECT_RULE: always lint before commit." in dump
    assert "<system-reminder>" in dump
    # The appended block is labeled as a separate message, not part of the system prompt.
    assert "not part of the system prompt" in dump.lower()


async def test_render_agent_system_prompt_no_agents_md_no_reminder(
    temp_work_dir: HostPath, config: Config
) -> None:
    """With no AGENTS.md, the dump is just the system prompt — no empty reminder section."""
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE
    from pythinker_code.soul.agent import render_agent_system_prompt

    dump = await render_agent_system_prompt(DEFAULT_AGENT_FILE, temp_work_dir, config)

    assert "## 1. Identity" in dump
    assert "not part of the system prompt" not in dump.lower()


def test_system_prompt_explains_adding_mcp_servers(builtin_args: BuiltinSystemPromptArgs):
    """The agent must know it can set up a *new* MCP server itself, in Pythinker.

    Without this, the model falls back on the MCP hosts in its training data
    (Claude Code / Claude Desktop), cites `~/.claude.json`, and wrongly refuses
    — claiming it "has no tool to edit" the config. The prompt must ground it in
    Pythinker's real MCP config files and the `pythinker mcp add` CLI, while
    keeping the honest "restart to load" caveat.
    """
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE

    prompt = _load_system_prompt(
        DEFAULT_AGENT_FILE.parent / "system.md",
        {"ROLE_ADDITIONAL": ""},
        builtin_args,
    )

    # Grounded in Pythinker's real config + CLI, not a host from training data.
    assert ".pythinker/mcp.json" in prompt
    assert "pythinker mcp add" in prompt
    # The honest caveat survives: a new server loads on restart, not mid-session.
    assert "restart" in prompt.lower()
    # Explicitly steers off the Claude-host hallucination seen in the wild.
    assert "Never reference `~/.claude.json`" in prompt


def test_system_prompt_explains_removing_and_rejects_yaml_mcp_config(
    builtin_args: BuiltinSystemPromptArgs,
):
    """The agent must also know how to *remove* a server, and must be steered off
    the real-world failure of writing `mcpServers` into `config.yaml` (YAML),
    which Pythinker never parses for MCP — the entry is silently dropped and the
    server never shows in `/mcp`.
    """
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE

    prompt = _load_system_prompt(
        DEFAULT_AGENT_FILE.parent / "system.md",
        {"ROLE_ADDITIONAL": ""},
        builtin_args,
    )

    # Removal is documented, not just add.
    assert "pythinker mcp remove" in prompt
    # Hard steer away from the config.yaml / YAML misplacement seen in the wild.
    assert "config.yaml" in prompt
    assert "silently dropped" in prompt


def test_system_prompt_treats_injected_date_as_authoritative(
    builtin_args: BuiltinSystemPromptArgs,
):
    """The injected date must be framed as authoritative so the model anchors
    its sense of 'now' to it instead of a training-era year."""
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE

    prompt = _load_system_prompt(
        DEFAULT_AGENT_FILE.parent / "system.md",
        {"ROLE_ADDITIONAL": ""},
        builtin_args,
    )

    assert builtin_args.PYTHINKER_NOW in prompt
    assert "authoritative present" in prompt
    assert "never fall back to a year assumed from training" in prompt


def test_system_prompt_enforces_context_first_orchestration(
    builtin_args: BuiltinSystemPromptArgs,
):
    """Default prompt should require evidence before codebase judgment."""
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE

    prompt = _load_system_prompt(
        DEFAULT_AGENT_FILE.parent / "system.md",
        {"ROLE_ADDITIONAL": ""},
        builtin_args,
    )

    assert "## 3. Operating Loop" in prompt
    assert "Gather — no context, no judgment" in prompt
    assert "Minimum packet before any codebase judgment" in prompt
    assert "Plan from evidence" in prompt
    assert "Treat subagent claims as leads, not proof" in prompt


def test_system_prompt_includes_markdown_table_formatting_guidance(
    builtin_args: BuiltinSystemPromptArgs,
):
    """Default prompt must reach the model with table-formatting rules so it
    stops emitting headers glued to prose (which render as raw text)."""
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE

    prompt = _load_system_prompt(
        DEFAULT_AGENT_FILE.parent / "system.md",
        {"ROLE_ADDITIONAL": ""},
        builtin_args,
    )

    assert "**Terminal Markdown.**" in prompt
    assert "never glued to prose" in prompt
    # Reports must not be wrapped in code fences (that is what preserves raw
    # emoji and breaks column alignment), and status icons should be sparing.
    assert "Code fences are for code only" in prompt
    assert "Status icons sparingly" in prompt


def test_default_subagent_prompts_keep_robust_contracts():
    """Specialist subagents should retain evidence, planning, and verification gates."""
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE, load_agent_spec

    root_spec = load_agent_spec(DEFAULT_AGENT_FILE)
    prompts = {
        name: load_agent_spec(subagent.path).system_prompt_args["ROLE_ADDITIONAL"]
        for name, subagent in root_spec.subagents.items()
    }

    assert "### CONTEXT PACKET" in prompts["explore"]
    assert "Do not provide architecture judgment" in prompts["explore"]
    assert "### TASK DEPENDENCY GRAPH" in prompts["plan"]
    assert "### PARALLEL EXECUTION GRAPH" in prompts["plan"]
    assert "Context gate before editing" in prompts["coder"]
    assert "After edits, inspect the diff/changed files" in prompts["implementer"]
    assert "PASS / FAIL / FLAKY" in prompts["verifier"]
    assert "independent LLM-as-judge quality gate" in prompts["judge"]
    assert "Reproduction protocol" in prompts["debugger"]
    assert "Evidence gate" in prompts["review"]
    assert "Every finding must cite concrete evidence" in prompts["code-reviewer"]
    assert "Build a threat context before judging" in prompts["security-reviewer"]


@pytest.mark.parametrize(
    "os_kind, shell, expect_windows_warning",
    [
        ("Windows", "Windows PowerShell (`powershell.exe`)", True),
        ("macOS", "bash (`/bin/bash`)", False),
        ("Linux", "bash (`/usr/bin/bash`)", False),
    ],
    ids=["windows", "macos", "linux"],
)
def test_system_prompt_platform_warning(temp_work_dir, os_kind, shell, expect_windows_warning):
    """System prompt should include Windows command warning only on Windows."""
    from pythinker_code.agentspec import DEFAULT_AGENT_FILE

    args = BuiltinSystemPromptArgs(
        PYTHINKER_NOW="1970-01-01T00:00:00+00:00",
        PYTHINKER_WORK_DIR=temp_work_dir,
        PYTHINKER_WORK_DIR_LS="Test ls content",
        PYTHINKER_AGENTS_MD="Test agents content",
        PYTHINKER_SKILLS="No skills found.",
        PYTHINKER_ADDITIONAL_DIRS_INFO="",
        PYTHINKER_OS=os_kind,
        PYTHINKER_SHELL=shell,
    )
    prompt = _load_system_prompt(
        DEFAULT_AGENT_FILE.parent / "system.md",
        {"ROLE_ADDITIONAL": ""},
        args,
    )

    assert os_kind in prompt
    assert shell in prompt
    if expect_windows_warning:
        assert "Many common Unix commands are unavailable" in prompt
    else:
        assert "Many common Unix commands are unavailable" not in prompt


def test_load_system_prompt_allows_literal_dollar(builtin_args: BuiltinSystemPromptArgs):
    """System prompt should allow literal $ without template errors."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        system_md = tmpdir / "system.md"
        system_md.write_text("Price is $100, path $PATH, time ${PYTHINKER_NOW}.")
        prompt = _load_system_prompt(system_md, {}, builtin_args)

    assert "$100" in prompt
    assert "$PATH" in prompt
    assert builtin_args.PYTHINKER_NOW in prompt


def test_load_system_prompt_include(builtin_args: BuiltinSystemPromptArgs):
    """System prompt should support {% include "file.md" %} directives."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        included = tmpdir / "extra.md"
        included.write_text("Included content here")
        system_md = tmpdir / "system.md"
        system_md.write_text('Main prompt. {% include "extra.md" %} End.')
        prompt = _load_system_prompt(system_md, {}, builtin_args)

    assert "Main prompt." in prompt
    assert "Included content here" in prompt
    assert "End." in prompt


def test_load_system_prompt_missing_arg_raises(builtin_args: BuiltinSystemPromptArgs):
    """Missing template args should raise a dedicated error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        system_md = tmpdir / "system.md"
        system_md.write_text("Missing ${UNKNOWN_ARG}.")
        with pytest.raises(SystemPromptTemplateError):
            _load_system_prompt(system_md, {}, builtin_args)


def test_load_tools_valid(runtime: Runtime):
    """Test loading valid tools."""
    tool_paths = ["pythinker_code.tools.think:Think", "pythinker_code.tools.shell:Shell"]
    toolset = PythinkerToolset()
    toolset.load_tools(
        tool_paths,
        {
            Runtime: runtime,
            Config: runtime.config,
            BuiltinSystemPromptArgs: runtime.builtin_args,
            Session: runtime.session,
            DenwaRenji: runtime.denwa_renji,
            Approval: runtime.approval,
            Environment: runtime.environment,
        },
    )
    assert len(toolset.tools) == snapshot(2)


def test_load_tools_invalid(runtime: Runtime):
    """Test loading with invalid tool paths.

    Regression for the cryptic "Invalid tools: [...]" message that hid the
    actual reason (module not found, class not found, constructor error)
    from the user. The error must now name each failing tool and its reason
    so a stale-binary or typo case is diagnosable from the traceback alone.
    """
    tool_paths = ["pythinker_code.tools.nonexistent:Tool", "pythinker_code.tools.think:Think"]
    toolset = PythinkerToolset()
    try:
        toolset.load_tools(
            tool_paths,
            {
                Runtime: runtime,
                Config: runtime.config,
                BuiltinSystemPromptArgs: runtime.builtin_args,
                Session: runtime.session,
                DenwaRenji: runtime.denwa_renji,
                Approval: runtime.approval,
            },
        )
        raise AssertionError("should fail to load non-existing tool")
    except InvalidToolError as e:
        msg = str(e)
        assert "pythinker_code.tools.nonexistent:Tool" in msg
        # Aggregated error must name the reason, not just the path. The exact
        # wording ("class or module not found") matches _load_tool's known
        # miss-path placeholder.
        assert "class or module not found" in msg


def test_load_tools_aggregates_constructor_errors(runtime: Runtime, monkeypatch):
    """A constructor exception on one tool must surface as a per-tool reason
    in the aggregated InvalidToolError, not a bare traceback out of agent load.

    Regression for the model-switch flow: when a PyInstaller binary is built
    before a new tool is added to the source, the bundled module imports OK
    but the class is missing — the loader returns None and (now) records a
    reason. The same per-tool-catch path also covers the rarer "constructor
    raised" case (e.g. a tool that requires a dep the toolset doesn't carry);
    both should land in the aggregated error message.
    """
    real_load_tool = PythinkerToolset._load_tool

    def boom(tool_path, dependencies):
        if tool_path.endswith(":Shell"):
            raise RuntimeError("simulated constructor failure")
        return real_load_tool(tool_path, dependencies)

    monkeypatch.setattr(PythinkerToolset, "_load_tool", staticmethod(boom))

    tool_paths = ["pythinker_code.tools.shell:Shell", "pythinker_code.tools.think:Think"]
    toolset = PythinkerToolset()
    with pytest.raises(InvalidToolError) as excinfo:
        toolset.load_tools(
            tool_paths,
            {
                Runtime: runtime,
                Config: runtime.config,
                BuiltinSystemPromptArgs: runtime.builtin_args,
                Session: runtime.session,
                DenwaRenji: runtime.denwa_renji,
                Approval: runtime.approval,
            },
        )
    msg = str(excinfo.value)
    # Per-tool reason attached; whole load still aborts (fail-fast preserved).
    assert "pythinker_code.tools.shell:Shell" in msg
    assert "RuntimeError: simulated constructor failure" in msg


async def test_load_agent_invalid_tools(agent_file_invalid_tools: Path, runtime: Runtime):
    """Test loading agent with invalid tools raises ValueError."""
    with pytest.raises(ValueError, match="Invalid tools"):
        await load_agent(agent_file_invalid_tools, runtime, mcp_configs=[])


async def test_load_agent_exposes_agent_metadata(runtime: Runtime):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        (tmpdir / "system.md").write_text("Main agent prompt")
        agent_yaml = tmpdir / "agent.yaml"
        agent_yaml.write_text(
            'version: 1\nagent:\n  name: "Main"\n'
            "  system_prompt_path: ./system.md\n"
            '  tools: ["pythinker_code.tools.think:Think"]\n'
            "  mode: all\n"
            "  hidden: true\n"
            "  steps: 5\n"
            "  temperature: 0.3\n"
            "  top_p: 0.9\n"
        )

        agent = await load_agent(agent_yaml, runtime, mcp_configs=[])

    assert agent.mode == "all"
    assert agent.hidden is True
    assert agent.steps == 5
    assert agent.temperature == 0.3
    assert agent.top_p == 0.9


async def test_load_agent_registers_builtin_subagent_types(runtime: Runtime):
    """Agent loading should register builtin subagent types without instantiating them."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create system prompts
        (tmpdir / "system.md").write_text("Main agent prompt")
        (tmpdir / "sub_system.md").write_text("Sub agent prompt")

        # Create builtin subagent type YAML (no nested subagents, minimal tools)
        builtin_type_yaml = tmpdir / "child.yaml"
        builtin_type_yaml.write_text(
            'version: 1\nagent:\n  name: "Sub"\n'
            "  system_prompt_path: ./sub_system.md\n"
            '  tools: ["pythinker_code.tools.think:Think"]\n'
        )

        # Create main agent YAML that registers one builtin subagent type
        agent_yaml = tmpdir / "agent.yaml"
        agent_yaml.write_text(
            'version: 1\nagent:\n  name: "Main"\n'
            "  system_prompt_path: ./system.md\n"
            '  tools: ["pythinker_code.tools.think:Think"]\n'
            "  subagents:\n"
            "    coder:\n"
            "      path: ./child.yaml\n"
            '      description: "A sub agent"\n'
        )

        agent = await load_agent(agent_yaml, runtime, mcp_configs=[])

        builtin_type = agent.runtime.labor_market.require_builtin_type("coder")
        assert builtin_type.name == "coder"
        assert builtin_type.description == "A sub agent"
        assert builtin_type.agent_file.samefile(builtin_type_yaml)


@pytest.fixture
def agent_projection_files(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "root-system.md").write_text("Root prompt", encoding="utf-8")
    (tmp_path / "child-system.md").write_text("Child prompt", encoding="utf-8")
    child_file = tmp_path / "child.yaml"
    child_file.write_text(
        "version: 1\n"
        "agent:\n"
        '  name: "Child"\n'
        "  system_prompt_path: ./child-system.md\n"
        '  tools: ["pythinker_code.tools.think:Think"]\n'
        '  allowed_tools: ["pythinker_code.tools.think:Think"]\n'
        '  model: "characterized-model"\n'
        '  when_to_use: "Use for exact contract tests."\n'
        "  hidden: true\n",
        encoding="utf-8",
    )
    root_file = tmp_path / "root.yaml"
    root_file.write_text(
        "version: 1\n"
        "agent:\n"
        '  name: "Root"\n'
        "  system_prompt_path: ./root-system.md\n"
        '  tools: ["pythinker_code.tools.think:Think"]\n'
        "  subagents:\n"
        "    analyst:\n"
        "      path: ./child.yaml\n"
        '      description: "Literal projected agent"\n',
        encoding="utf-8",
    )
    return root_file, child_file


async def test_load_agent_preserves_literal_type_projection_and_toolset_facade(
    runtime: Runtime,
    agent_projection_files: tuple[Path, Path],
) -> None:
    root_file, child_file = agent_projection_files

    agent = await load_agent(root_file, runtime, mcp_configs=[])

    assert runtime.labor_market.require_builtin_type("analyst") == AgentTypeDefinition(
        name="analyst",
        description="Literal projected agent",
        agent_file=child_file,
        when_to_use="Use for exact contract tests.",
        default_model="characterized-model",
        tool_policy=ToolPolicy(
            mode="allowlist",
            tools=("pythinker_code.tools.think:Think",),
        ),
        supports_background=False,
        required_mcp_servers=(),
    )
    assert isinstance(agent.toolset, PythinkerToolset)
    assert runtime.mcp_status == agent.toolset.mcp_status_snapshot
    assert agent.toolset.find("Think") is not None


async def test_load_agent_starts_mcp_in_background(runtime: Runtime, monkeypatch):
    called: dict[str, bool] = {}

    async def fake_load_mcp_tools(self, mcp_configs, runtime, in_background: bool = True):
        called["in_background"] = in_background

    monkeypatch.setattr(PythinkerToolset, "load_mcp_tools", fake_load_mcp_tools)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        (tmpdir / "system.md").write_text("Main agent prompt")
        agent_yaml = tmpdir / "agent.yaml"
        agent_yaml.write_text(
            'version: 1\nagent:\n  name: "Main"\n'
            "  system_prompt_path: ./system.md\n"
            '  tools: ["pythinker_code.tools.think:Think"]\n'
        )

        await load_agent(agent_yaml, runtime, mcp_configs=[{"mcpServers": {}}])

    assert called == {"in_background": True}


async def test_load_agent_can_defer_mcp_loading(runtime: Runtime, monkeypatch):
    called: dict[str, bool] = {}

    async def fake_load_mcp_tools(self, mcp_configs, runtime, in_background: bool = True):
        called["load_called"] = True

    def fake_defer_mcp_tool_loading(self, mcp_configs, runtime):
        called["defer_called"] = True

    monkeypatch.setattr(PythinkerToolset, "load_mcp_tools", fake_load_mcp_tools)
    monkeypatch.setattr(PythinkerToolset, "defer_mcp_tool_loading", fake_defer_mcp_tool_loading)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        (tmpdir / "system.md").write_text("Main agent prompt")
        agent_yaml = tmpdir / "agent.yaml"
        agent_yaml.write_text(
            'version: 1\nagent:\n  name: "Main"\n'
            "  system_prompt_path: ./system.md\n"
            '  tools: ["pythinker_code.tools.think:Think"]\n'
        )

        await load_agent(
            agent_yaml,
            runtime,
            mcp_configs=[{"mcpServers": {}}],
            start_mcp_loading=False,
        )

    assert called == {"defer_called": True}


@pytest.fixture
def agent_file_invalid_tools() -> Generator[Path, Any, Any]:
    """Create an agent configuration file with invalid tools."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        # Create system.md
        system_md = tmpdir / "system.md"
        system_md.write_text("You are a test agent")

        # Create agent.yaml with invalid tools
        agent_yaml = tmpdir / "agent.yaml"
        agent_yaml.write_text("""
version: 1
agent:
  name: "Test Agent"
  system_prompt_path: ./system.md
  tools: ["pythinker_code.tools.nonexistent:Tool"]
""")

        yield agent_yaml


@pytest.fixture
def system_prompt_file() -> Generator[Path, Any, Any]:
    """Create a system prompt file with template variables."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        system_md = tmpdir / "system.md"
        system_md.write_text("Test system prompt with ${PYTHINKER_NOW} and ${CUSTOM_ARG}")

        yield system_md


def test_extend_escaping_agent_roots_is_rejected(tmp_path: Path) -> None:
    # An `extend:` that traverses outside the spec's own directory (and the
    # built-in agents dir) is rejected fail-closed as defense-in-depth, since
    # the resolved path is otherwise loaded directly.
    from pythinker_code.agentspec import AgentSpecError, load_agent_spec

    (tmp_path / "outside-system.md").write_text("outside", encoding="utf-8")
    (tmp_path / "outside.yaml").write_text(
        "version: 1\nagent:\n  name: outside\n"
        "  system_prompt_path: ./outside-system.md\n  tools: []\n",
        encoding="utf-8",
    )
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "system.md").write_text("child", encoding="utf-8")
    (agents / "child.yaml").write_text(
        "version: 1\nagent:\n  name: child\n"
        "  system_prompt_path: ./system.md\n  tools: []\n"
        "  extend: ../outside.yaml\n",
        encoding="utf-8",
    )

    with pytest.raises(AgentSpecError, match="outside the permitted"):
        load_agent_spec(agents / "child.yaml")


def test_subagent_path_escaping_agent_roots_is_rejected(tmp_path: Path) -> None:
    from pythinker_code.agentspec import AgentSpecError, load_agent_spec

    (tmp_path / "outside.yaml").write_text("version: 1\nagent:\n  name: x\n", encoding="utf-8")
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "system.md").write_text("root", encoding="utf-8")
    (agents / "root.yaml").write_text(
        "version: 1\nagent:\n  name: root\n"
        "  system_prompt_path: ./system.md\n  tools: []\n"
        "  subagents:\n    analyst:\n      path: ../outside.yaml\n"
        '      description: "d"\n',
        encoding="utf-8",
    )

    with pytest.raises(AgentSpecError, match="outside the permitted"):
        load_agent_spec(agents / "root.yaml")


def test_sibling_extend_within_agent_root_still_loads(tmp_path: Path) -> None:
    # Regression guard: the containment check must not reject the normal
    # `./sibling.yaml` shape every shipped spec uses.
    from pythinker_code.agentspec import load_agent_spec

    (tmp_path / "base-system.md").write_text("base", encoding="utf-8")
    (tmp_path / "base.yaml").write_text(
        "version: 1\nagent:\n  name: base\n  system_prompt_path: ./base-system.md\n  tools: []\n",
        encoding="utf-8",
    )
    (tmp_path / "child.yaml").write_text(
        "version: 1\nagent:\n  name: child\n  extend: ./base.yaml\n",
        encoding="utf-8",
    )

    resolved = load_agent_spec(tmp_path / "child.yaml")
    assert resolved.name == "child"
