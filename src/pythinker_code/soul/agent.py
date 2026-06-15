from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pydantic
from jinja2 import FileSystemLoader, StrictUndefined, TemplateError, UndefinedError
from jinja2.sandbox import SandboxedEnvironment as JinjaEnvironment
from pythinker_core.tooling import Toolset
from pythinker_host.path import HostPath

from pythinker_code.agentspec import load_agent_spec
from pythinker_code.approval_runtime import ApprovalRuntime
from pythinker_code.auth.oauth import OAuthManager
from pythinker_code.background import BackgroundTaskManager
from pythinker_code.config import Config
from pythinker_code.exception import MCPConfigError, SystemPromptTemplateError
from pythinker_code.llm import LLM
from pythinker_code.notifications import NotificationManager
from pythinker_code.prompt_templates import PromptTemplate, discover_prompt_templates
from pythinker_code.scratchpad import DEFAULT_SCRATCHPAD_SECTION
from pythinker_code.session import Session
from pythinker_code.skill import (
    Skill,
    discover_skills_from_roots,
    format_skills_for_prompt,
    index_skills,
    resolve_skills_roots,
)
from pythinker_code.soul.approval import Approval, ApprovalState
from pythinker_code.soul.denwarenji import DenwaRenji
from pythinker_code.soul.message import system_reminder
from pythinker_code.soul.toolset import PythinkerToolset, ToolType
from pythinker_code.subagents.discovery import (
    discover_markdown_agents,
    materialize_markdown_agent_specs,
    resolve_agent_roots,
)
from pythinker_code.subagents.models import AgentTypeDefinition, ToolPolicy
from pythinker_code.subagents.registry import LaborMarket
from pythinker_code.subagents.store import SubagentStore
from pythinker_code.utils.environment import Environment
from pythinker_code.utils.file_read_cache import FileReadCache
from pythinker_code.utils.logging import logger
from pythinker_code.utils.path import find_project_root, is_within_directory, list_directory
from pythinker_code.utils.trust import strip_invisible_chars
from pythinker_code.wire.root_hub import RootWireHub

if TYPE_CHECKING:
    from fastmcp.mcp_config import MCPConfig

    from pythinker_code.wire.types import MCPStatusSnapshot


@dataclass(frozen=True, slots=True, kw_only=True)
class BuiltinSystemPromptArgs:
    """Builtin system prompt arguments."""

    PYTHINKER_NOW: str
    """The current datetime."""
    PYTHINKER_WORK_DIR: HostPath
    """The absolute path of current working directory."""
    PYTHINKER_WORK_DIR_LS: str
    """The directory listing of current working directory."""
    PYTHINKER_AGENTS_MD: str
    """The merged content of AGENTS.md files (from project root to work_dir). Delivered as a
    session-start <system-reminder> preamble (see render_agents_md_reminder), not interpolated
    into the system prompt, so it survives compaction and is never budget-truncated."""
    PYTHINKER_SKILLS: str
    """Formatted information about available skills."""
    PYTHINKER_ADDITIONAL_DIRS_INFO: str
    """Formatted information about additional directories in the workspace."""
    PYTHINKER_OS: str
    """The operating system kind, e.g. 'Windows', 'macOS', 'Linux'."""
    PYTHINKER_SHELL: str
    """The shell executable used by the Shell tool, e.g. 'bash (`/bin/bash`)'."""
    PYTHINKER_SCRATCHPAD_SECTION: str = DEFAULT_SCRATCHPAD_SECTION
    """The rendered session-scratchpad prompt section (available or unavailable guard)."""
    PYTHINKER_AGENTS_MD_FENCE: str = "`" * 9
    """Code-fence delimiter for the AGENTS.md block, sized to exceed any backtick run in it."""


_AGENTS_MD_MAX_BYTES = 32 * 1024  # 32 KiB


def _agents_md_fence(content: str) -> str:
    """Return a backtick fence longer than any backtick run inside *content*."""
    longest = max((len(m.group()) for m in re.finditer(r"`+", content)), default=0)
    return "`" * max(9, longest + 1)


def render_agents_md_reminder(builtin_args: BuiltinSystemPromptArgs) -> str | None:
    """Render the merged ``AGENTS.md`` as an authoritative ``<system-reminder>`` body.

    Returns ``None`` when no ``AGENTS.md`` applies between the project root and the
    working directory. Otherwise returns the framing + fenced merged content that is
    delivered as a session-start, user-role reminder prepended to every model request
    (see :func:`pythinker_code.soul.pythinkersoul._with_agents_md_preamble`), rather than
    baked into the immutable system prompt.

    Delivering it this way keeps the project instructions out of the system prompt while
    making them immune to the two failure modes a system-prompt move would otherwise hit:
    they never enter the persisted history, so context compaction cannot summarize them
    away; and they are not a budgeted dynamic injection, so the injection token ceiling
    cannot truncate them. The content is therefore always present, in full, and verbatim.
    """
    agents_md = builtin_args.PYTHINKER_AGENTS_MD
    if not agents_md:
        return None
    fence = builtin_args.PYTHINKER_AGENTS_MD_FENCE
    return (
        "The merged `AGENTS.md` project instructions below are authoritative and already "
        "assembled: every file from the project root down to the working directory, deeper "
        "(more specific) files overriding shallower ones, each governing its own directory "
        "and everything beneath it. Treat them with the same authority as your system "
        "instructions.\n\n"
        f"{fence}\n{agents_md}\n{fence}"
    )


async def _dirs_root_to_leaf(work_dir: HostPath, project_root: HostPath) -> list[HostPath]:
    """Return the list of directories from *project_root* down to *work_dir* (inclusive)."""
    dirs: list[HostPath] = []
    current = work_dir
    while True:
        dirs.append(current)
        if current == project_root:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    dirs.reverse()  # root → leaf
    return dirs


async def load_agents_md(work_dir: HostPath) -> str | None:
    """Discover and merge ``AGENTS.md`` files from the project root down to *work_dir*.

    For each directory on the path, ``AGENTS.md`` is checked first, then the
    lowercase ``agents.md`` variant. The two names are mutually exclusive within
    a directory: uppercase wins.

    All discovered files are concatenated root→leaf, separated by ``\\n\\n``, with
    source annotations.  Total size is capped at :data:`_AGENTS_MD_MAX_BYTES`.
    Budget is allocated leaf-first so deeper (more specific) files are never
    truncated in favour of shallower ones.
    """
    project_root = await find_project_root(work_dir)
    dirs = await _dirs_root_to_leaf(work_dir, project_root)

    # Phase 1: collect all candidate files (root → leaf order)
    discovered: list[tuple[HostPath, str]] = []  # (path, content)
    for d in dirs:
        # AGENTS.md and agents.md are mutually exclusive (uppercase wins)
        for path in (d / "AGENTS.md", d / "agents.md"):
            if not await path.is_file():
                continue
            # AGENTS.md is merged verbatim into the system prompt, so neutralize the
            # invisible-unicode smuggling vector on ingestion (injdef-4). Visible prose
            # is kept — AGENTS.md is user-authored project config, not blocked.
            raw = await path.read_text(encoding="utf-8", errors="replace")
            content = strip_invisible_chars(raw).strip()
            if content:
                discovered.append((path, content))
                logger.info("Loaded agents.md: {path}", path=path)
            break

    if not discovered:
        logger.info(
            "No AGENTS.md found from {root} to {cwd}",
            root=project_root,
            cwd=work_dir,
        )
        return None

    # Phase 2: allocate budget leaf-first so deeper (more specific) files
    # are never truncated in favour of shallower ones.
    # The annotation overhead (<!-- From: ... -->\n and \n\n separators)
    # is included in the budget so the final output never exceeds the limit.
    remaining = _AGENTS_MD_MAX_BYTES
    budgeted: list[tuple[HostPath, str] | None] = [None] * len(discovered)
    for i in reversed(range(len(discovered))):
        path, content = discovered[i]
        annotation = f"<!-- From: {path} -->\n"
        # Reserve space for the annotation and the \n\n separator between parts
        separator_cost = len(b"\n\n") if i < len(discovered) - 1 else 0
        overhead = len(annotation.encode("utf-8")) + separator_cost
        remaining -= overhead
        if remaining <= 0:
            budgeted[i] = (path, "")
            remaining = 0
            continue
        encoded = content.encode("utf-8")
        if len(encoded) > remaining:
            content = encoded[:remaining].decode("utf-8", errors="ignore").strip()
            logger.warning("AGENTS.md truncated due to size limit: {path}", path=path)
        remaining -= len(content.encode("utf-8"))
        budgeted[i] = (path, content)

    # Phase 3: assemble in root → leaf order, skipping entries emptied by truncation
    parts: list[str] = []
    for item in budgeted:
        if item is None:
            continue
        path, content = item
        if content:
            parts.append(f"<!-- From: {path} -->\n{content}")

    return "\n\n".join(parts) if parts else None


@dataclass(slots=True, kw_only=True)
class Runtime:
    """Agent runtime."""

    config: Config
    oauth: OAuthManager
    llm: LLM | None  # we do not freeze the `Runtime` dataclass because LLM can be changed
    session: Session
    builtin_args: BuiltinSystemPromptArgs
    denwa_renji: DenwaRenji
    approval: Approval
    labor_market: LaborMarket
    environment: Environment
    notifications: NotificationManager
    background_tasks: BackgroundTaskManager
    skills: dict[str, Skill]
    additional_dirs: list[HostPath]
    skills_dirs: list[HostPath]
    prompt_templates: dict[str, PromptTemplate] = field(default_factory=dict[str, PromptTemplate])
    mcp_tools: dict[str, ToolType] = field(default_factory=dict[str, ToolType])
    """Connected MCP tools, keyed `mcp__<server>__<tool>`, shared with subagent allowlists."""
    mcp_status: Callable[[], MCPStatusSnapshot | None] | None = None
    """Root-only accessor for live MCP startup state, wired from the root toolset. Used by
    the subagent-spawn gate to reject an agent whose required MCP servers are absent."""
    file_read_cache: FileReadCache = field(default_factory=FileReadCache)
    """Per-agent record of when each file was last read, backing read-before-write
    enforcement and stale-overwrite detection in the file tools."""
    subagent_store: SubagentStore | None = None
    approval_runtime: ApprovalRuntime | None = None
    root_wire_hub: RootWireHub | None = None
    subagent_id: str | None = None
    subagent_type: str | None = None
    role: Literal["root", "subagent"] = "root"
    ui_mode: str = "shell"
    resumed: bool = False
    hook_engine: Any = None
    """HookEngine instance, set by PythinkerCLI after soul creation."""
    rearm_injection: Callable[[str], None] | None = None
    """Callback set by PythinkerSoul so tools can refresh dynamic injections."""
    work_dir_override: HostPath | None = None
    """Operational working directory override (e.g. a per-child git worktree).

    The session object stays shared for persistence paths; only the
    operational cwd/path-resolution surface reads ``work_dir``."""

    @property
    def work_dir(self) -> HostPath:
        """The operational working directory (override, else the session's)."""
        return self.work_dir_override or self.session.work_dir

    def __post_init__(self) -> None:
        if self.subagent_store is None:
            self.subagent_store = SubagentStore(self.session)
        if self.root_wire_hub is None:
            self.root_wire_hub = RootWireHub()
        if self.approval_runtime is None:
            self.approval_runtime = ApprovalRuntime()
        self.approval_runtime.bind_root_wire_hub(self.root_wire_hub)
        self.approval.set_runtime(self.approval_runtime)
        self.background_tasks.bind_runtime(self)

    @staticmethod
    async def create(
        config: Config,
        oauth: OAuthManager,
        llm: LLM | None,
        session: Session,
        yolo: bool,
        auto: bool = False,
        runtime_auto: bool = False,
        no_yolo: bool = False,
        skills_dirs: list[HostPath] | None = None,
        scratchpad_section: str | None = None,
    ) -> Runtime:
        ls_output, agents_md, environment = await asyncio.gather(
            list_directory(session.work_dir),
            load_agents_md(session.work_dir),
            Environment.detect(),
        )

        # Discover and format skills (grouped by scope for the system prompt).
        scoped_roots = await resolve_skills_roots(
            session.work_dir,
            skills_dirs=skills_dirs,
            merge_brands=config.merge_all_available_skills,
            extra_skill_dirs=config.extra_skill_dirs or None,
        )
        # Canonicalize so symlinked skill directories match resolved paths
        skills_roots_canonical = [s.root.canonical() for s in scoped_roots]
        skills = await discover_skills_from_roots(scoped_roots)
        skills_by_name = index_skills(skills)
        logger.info("Discovered {count} skill(s)", count=len(skills))
        skills_formatted = format_skills_for_prompt(skills)

        prompt_templates = await discover_prompt_templates(session.work_dir)
        logger.info("Discovered {count} prompt template(s)", count=len(prompt_templates))

        # Restore additional directories from session state, pruning stale entries
        additional_dirs: list[HostPath] = []
        pruned = False
        valid_dir_strs: list[str] = []
        for dir_str in session.state.additional_dirs:
            d = HostPath(dir_str).canonical()
            if await d.is_dir():
                additional_dirs.append(d)
                valid_dir_strs.append(dir_str)
            else:
                logger.warning(
                    "Additional directory no longer exists, removing from state: {dir}",
                    dir=dir_str,
                )
                pruned = True
        if pruned:
            session.state.additional_dirs = valid_dir_strs
            session.save_state()

        # Format additional dirs info for system prompt
        additional_dirs_info = ""
        if additional_dirs:
            parts: list[str] = []
            for d in additional_dirs:
                try:
                    dir_ls = await list_directory(d)
                except OSError:
                    logger.warning(
                        "Cannot list additional directory, skipping listing: {dir}", dir=d
                    )
                    dir_ls = "[directory not readable]"
                parts.append(f"### `{d}`\n\n```\n{dir_ls}\n```")
            additional_dirs_info = "\n\n".join(parts)

        # Merge invocation flags with persisted session state. ``--no-yolo`` is an explicit
        # force-off that beats the flag, config ``default_yolo``, and persisted state.
        original_persisted_yolo = session.state.approval.yolo
        effective_yolo = (yolo or original_persisted_yolo) and not no_yolo
        # Do NOT force safe_mode off under yolo: yolo already bypasses safe mode in the
        # decision path (is_auto_approve / _unattended_denial_feedback short-circuit on
        # yolo before reading safe_mode), so there is no deadlock to avoid — and forcing it
        # False here used to get persisted back to trust state, silently downgrading the
        # workspace's trust posture.
        effective_safe_mode = session.state.trust.safe_mode
        if auto and not session.state.approval.auto:
            session.state.approval.auto = True
            session.save_state()
        saved_actions = set(session.state.approval.auto_approve_actions)

        def _on_approval_change() -> None:
            if not no_yolo:
                session.state.approval.yolo = approval_state.yolo
            else:
                session.state.approval.yolo = original_persisted_yolo
            session.state.approval.auto = approval_state.auto
            session.state.approval.auto_approve_actions = set(approval_state.auto_approve_actions)
            session.state.trust.safe_mode = approval_state.safe_mode
            session.save_state()

        approval_state = ApprovalState(
            yolo=effective_yolo,
            auto=session.state.approval.auto,
            runtime_auto=runtime_auto,
            safe_mode=effective_safe_mode,
            auto_deliberate=(
                config.auto_deliberate_destructive_actions
                or config.ask_user_question_policy == "auto_deliberate"
            ),
            auto_approve_actions=saved_actions,
            on_change=_on_approval_change,
        )
        notifications = NotificationManager(
            session.context_file.parent / "notifications",
            config.notifications,
        )

        return Runtime(
            config=config,
            oauth=oauth,
            llm=llm,
            session=session,
            builtin_args=BuiltinSystemPromptArgs(
                PYTHINKER_NOW=datetime.now().astimezone().isoformat(),
                PYTHINKER_WORK_DIR=session.work_dir,
                PYTHINKER_WORK_DIR_LS=ls_output,
                PYTHINKER_AGENTS_MD=agents_md or "",
                PYTHINKER_AGENTS_MD_FENCE=_agents_md_fence(agents_md or ""),
                PYTHINKER_SKILLS=skills_formatted or "No skills found.",
                PYTHINKER_ADDITIONAL_DIRS_INFO=additional_dirs_info,
                PYTHINKER_OS=environment.os_kind,
                PYTHINKER_SHELL=f"{environment.shell_name} (`{environment.shell_path}`)",
                PYTHINKER_SCRATCHPAD_SECTION=scratchpad_section or DEFAULT_SCRATCHPAD_SECTION,
            ),
            denwa_renji=DenwaRenji(),
            approval=Approval(state=approval_state),
            labor_market=LaborMarket(),
            environment=environment,
            notifications=notifications,
            background_tasks=BackgroundTaskManager(
                session,
                config.background,
                notifications=notifications,
            ),
            skills=skills_by_name,
            prompt_templates=prompt_templates,
            additional_dirs=additional_dirs,
            # Only expose skills roots outside the workspace for Glob access;
            # project-level roots are already within work_dir.
            skills_dirs=[
                r for r in skills_roots_canonical if not is_within_directory(r, session.work_dir)
            ],
            subagent_store=SubagentStore(session),
            approval_runtime=ApprovalRuntime(),
            root_wire_hub=RootWireHub(),
            role="root",
        )

    def copy_for_subagent(
        self,
        *,
        agent_id: str,
        subagent_type: str,
        llm_override: LLM | None = None,
        work_dir_override: HostPath | None = None,
        work_dir_ls: str | None = None,
        work_dir_agents_md: str | None = None,
    ) -> Runtime:
        """Clone runtime for a subagent.

        ``work_dir_override`` points the child's operational surface (and its
        system-prompt work-dir args) at another directory, e.g. an isolation
        worktree; the shared session keeps owning persistence paths.
        """
        builtin_args = self.builtin_args
        if work_dir_override is not None:
            # An explicit value (including "") replaces the payload for the
            # child's work dir; None means "not provided" and keeps the parent's
            # so inherited policy/instruction context is never silently dropped.
            agents_md = (
                work_dir_agents_md
                if work_dir_agents_md is not None
                else builtin_args.PYTHINKER_AGENTS_MD
            )
            builtin_args = replace(
                builtin_args,
                PYTHINKER_WORK_DIR=work_dir_override,
                PYTHINKER_WORK_DIR_LS=work_dir_ls or "",
                PYTHINKER_AGENTS_MD=agents_md,
                PYTHINKER_AGENTS_MD_FENCE=_agents_md_fence(agents_md),
            )
        return Runtime(
            config=self.config,
            oauth=self.oauth,
            llm=llm_override if llm_override is not None else self.llm,
            session=self.session,
            builtin_args=builtin_args,
            denwa_renji=DenwaRenji(),  # subagent must have its own DenwaRenji
            approval=self.approval.share(),
            labor_market=self.labor_market,
            environment=self.environment,
            notifications=self.notifications,
            background_tasks=self.background_tasks.copy_for_role("subagent"),
            skills=self.skills,
            prompt_templates=self.prompt_templates,
            # Share the same list reference so /add-dir mutations propagate to all agents
            additional_dirs=self.additional_dirs,
            skills_dirs=self.skills_dirs,
            # Share the parent's connected MCP tools so allowlisted subagents can attach them
            mcp_tools=self.mcp_tools,
            subagent_store=self.subagent_store,
            approval_runtime=self.approval_runtime,
            root_wire_hub=self.root_wire_hub,
            subagent_id=agent_id,
            subagent_type=subagent_type,
            role="subagent",
            work_dir_override=work_dir_override or self.work_dir_override,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Agent:
    """The loaded agent."""

    name: str
    system_prompt: str
    toolset: Toolset
    runtime: Runtime
    """Each agent has its own runtime, which should be derived from its main agent."""
    mode: str = "primary"
    hidden: bool = False
    steps: int | None = None
    temperature: float | None = None
    top_p: float | None = None


async def load_agent(
    agent_file: Path,
    runtime: Runtime,
    *,
    mcp_configs: list[MCPConfig] | list[dict[str, Any]],
    start_mcp_loading: bool = True,
) -> Agent:
    """
    Load agent from specification file.

    Raises:
        FileNotFoundError: When the agent file is not found.
        AgentSpecError(PythinkerCLIException, ValueError): When the agent specification is invalid.
        SystemPromptTemplateError(PythinkerCLIException, ValueError): When the system prompt
            template is invalid.
        InvalidToolError(PythinkerCLIException, ValueError): When any tool cannot be loaded.
        MCPConfigError(PythinkerCLIException, ValueError): When any MCP configuration is invalid.
        MCPRuntimeError(PythinkerCLIException, RuntimeError): When any MCP server cannot be
            connected.
    """
    logger.info("Loading agent: {agent_file}", agent_file=agent_file)
    agent_spec = load_agent_spec(agent_file)

    system_prompt = _load_system_prompt(
        agent_spec.system_prompt_path,
        agent_spec.system_prompt_args,
        runtime.builtin_args,
    )

    # Register built-in subagent types before loading tools because some tools render
    # descriptions from the labor market on initialization.
    for subagent_name, subagent_spec in agent_spec.subagents.items():
        logger.debug(
            "Registering builtin subagent type: {subagent_name}", subagent_name=subagent_name
        )
        builtin_spec = load_agent_spec(subagent_spec.path)
        tool_policy = (
            ToolPolicy(mode="allowlist", tools=tuple(builtin_spec.allowed_tools))
            if builtin_spec.allowed_tools is not None
            else ToolPolicy(mode="inherit")
        )
        runtime.labor_market.add_builtin_type(
            AgentTypeDefinition(
                name=subagent_name,
                description=subagent_spec.description,
                agent_file=subagent_spec.path,
                when_to_use=builtin_spec.when_to_use,
                default_model=builtin_spec.model,
                tool_policy=tool_policy,
                supports_background=not builtin_spec.hidden,
            )
        )

    external_agents = await discover_markdown_agents(await resolve_agent_roots(runtime.work_dir))
    for type_def in materialize_markdown_agent_specs(
        external_agents,
        output_dir=runtime.session.dir / "external_agents",
        available_models=set(runtime.config.models),
    ):
        if runtime.labor_market.get_builtin_type(type_def.name) is not None:
            logger.warning(
                "Skipping external markdown agent {name}: would override a built-in subagent type",
                name=type_def.name,
            )
            continue
        logger.debug("Registering external markdown agent type: {name}", name=type_def.name)
        runtime.labor_market.add_builtin_type(type_def)

    toolset = PythinkerToolset(runtime)
    # Wire the live MCP startup state so the subagent-spawn gate can reject an agent whose
    # required MCP servers are absent (root only — subagents never spawn other agents).
    runtime.mcp_status = toolset.mcp_status_snapshot
    tool_deps = {
        PythinkerToolset: toolset,
        Runtime: runtime,
        # TODO: remove all the following dependencies and use Runtime instead
        Config: runtime.config,
        BuiltinSystemPromptArgs: runtime.builtin_args,
        Session: runtime.session,
        DenwaRenji: runtime.denwa_renji,
        Approval: runtime.approval,
        LaborMarket: runtime.labor_market,
        Environment: runtime.environment,
    }
    tools = agent_spec.allowed_tools if agent_spec.allowed_tools is not None else agent_spec.tools
    if agent_spec.exclude_tools:
        logger.debug("Excluding tools: {tools}", tools=agent_spec.exclude_tools)
        tools = [tool for tool in tools if tool not in agent_spec.exclude_tools]
    named_tools = [tool for tool in tools if ":" not in tool]
    toolset.load_tools([tool for tool in tools if ":" in tool], tool_deps)
    if named_tools:
        toolset.add_shared_tools(named_tools, runtime.mcp_tools)

    # Load plugin tools
    from pythinker_code.plugin.manager import get_plugins_dir
    from pythinker_code.plugin.tool import load_plugin_tools

    plugin_tools = load_plugin_tools(get_plugins_dir(), runtime.config, approval=runtime.approval)
    for plugin_tool in plugin_tools:
        if toolset.find(plugin_tool.name) is not None:
            logger.warning(
                "Plugin tool '{name}' conflicts with an existing tool, skipping",
                name=plugin_tool.name,
            )
            continue
        toolset.add(plugin_tool)

    if mcp_configs:
        validated_mcp_configs: list[MCPConfig] = []
        if mcp_configs:
            from fastmcp.mcp_config import MCPConfig

            for mcp_config in mcp_configs:
                try:
                    if isinstance(mcp_config, dict):
                        raw_config = mcp_config
                    else:
                        raw_config = mcp_config.model_dump(mode="json")
                    from pythinker_code.cli.mcp import prepare_mcp_config_dict

                    prepare_mcp_config_dict(raw_config)
                    validated_mcp_configs.append(MCPConfig.model_validate(raw_config))
                except pydantic.ValidationError as e:
                    raise MCPConfigError(f"Invalid MCP config: {e}") from e
        if start_mcp_loading:
            await toolset.load_mcp_tools(validated_mcp_configs, runtime, in_background=True)
        else:
            toolset.defer_mcp_tool_loading(validated_mcp_configs, runtime)

    return Agent(
        name=agent_spec.name,
        system_prompt=system_prompt,
        toolset=toolset,
        runtime=runtime,
        mode=agent_spec.mode,
        hidden=agent_spec.hidden,
        steps=agent_spec.steps,
        temperature=agent_spec.temperature,
        top_p=agent_spec.top_p,
    )


def _load_system_prompt(
    path: Path, args: dict[str, str], builtin_args: BuiltinSystemPromptArgs
) -> str:
    logger.info("Loading system prompt: {path}", path=path)
    system_prompt = path.read_text(encoding="utf-8").strip()
    logger.debug(
        "Substituting system prompt with builtin args: {builtin_args}, spec args: {spec_args}",
        builtin_args=builtin_args,
        spec_args=args,
    )
    env = JinjaEnvironment(
        loader=FileSystemLoader(path.parent),
        keep_trailing_newline=True,
        lstrip_blocks=True,
        trim_blocks=True,
        variable_start_string="${",
        variable_end_string="}",
        undefined=StrictUndefined,
    )
    try:
        template = env.from_string(system_prompt)
        return template.render(asdict(builtin_args), **args)
    except UndefinedError as exc:
        raise SystemPromptTemplateError(f"Missing system prompt arg in {path}: {exc}") from exc
    except TemplateError as exc:
        raise SystemPromptTemplateError(f"Invalid system prompt template: {path}: {exc}") from exc


async def build_builtin_system_prompt_args(
    work_dir: HostPath,
    config: Config,
    *,
    scratchpad_section: str | None = None,
) -> BuiltinSystemPromptArgs:
    """Build system-prompt args from the filesystem + environment only.

    Read-only: constructs no Runtime, session, LLM, auth, or MCP connection. Mirrors
    the arg assembly in :meth:`Runtime.create` so a dumped prompt matches what an agent
    would actually receive. ``PYTHINKER_ADDITIONAL_DIRS_INFO`` is empty because additional
    directories are session state, which inspection does not load.
    """
    ls_output, agents_md, environment = await asyncio.gather(
        list_directory(work_dir),
        load_agents_md(work_dir),
        Environment.detect(),
    )
    scoped_roots = await resolve_skills_roots(
        work_dir,
        merge_brands=config.merge_all_available_skills,
        extra_skill_dirs=config.extra_skill_dirs or None,
    )
    skills_formatted = format_skills_for_prompt(await discover_skills_from_roots(scoped_roots))
    return BuiltinSystemPromptArgs(
        PYTHINKER_NOW=datetime.now().astimezone().isoformat(),
        PYTHINKER_WORK_DIR=work_dir,
        PYTHINKER_WORK_DIR_LS=ls_output,
        PYTHINKER_AGENTS_MD=agents_md or "",
        PYTHINKER_AGENTS_MD_FENCE=_agents_md_fence(agents_md or ""),
        PYTHINKER_SKILLS=skills_formatted or "No skills found.",
        PYTHINKER_ADDITIONAL_DIRS_INFO="",
        PYTHINKER_OS=environment.os_kind,
        PYTHINKER_SHELL=f"{environment.shell_name} (`{environment.shell_path}`)",
        PYTHINKER_SCRATCHPAD_SECTION=scratchpad_section or DEFAULT_SCRATCHPAD_SECTION,
    )


# Separates the system prompt from the appended AGENTS.md reminder in dump output, making
# clear the reminder is delivered as its own message and is not part of the system prompt.
_AGENTS_MD_DUMP_HEADER = (
    "================================================================================\n"
    "The block below is NOT part of the system prompt. The merged AGENTS.md is delivered\n"
    "as a session-start, user-role <system-reminder> message prepended to every model\n"
    "request (see PythinkerSoul._step). It is shown here so this dump is faithful.\n"
    "================================================================================"
)


async def render_agent_system_prompt(agent_file: Path, work_dir: HostPath, config: Config) -> str:
    """Render an agent's assembled system prompt for inspection (read-only).

    Resolves the agent spec, builds live builtin args from the filesystem + environment,
    and renders the template — with no Runtime, session, auth, or MCP. Backs the
    ``pythinker system-prompt`` command. When an ``AGENTS.md`` applies, the session-start
    reminder that carries it (delivered as a separate message, not baked into the system
    prompt) is appended below a labeled divider so the dump reflects what the agent receives.
    """
    agent_spec = load_agent_spec(agent_file)
    builtin_args = await build_builtin_system_prompt_args(work_dir, config)
    prompt = _load_system_prompt(
        agent_spec.system_prompt_path,
        agent_spec.system_prompt_args,
        builtin_args,
    )
    reminder = render_agents_md_reminder(builtin_args)
    if reminder is None:
        return prompt
    return f"{prompt}\n\n{_AGENTS_MD_DUMP_HEADER}\n\n{system_reminder(reminder).text}"
