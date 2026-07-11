"""Discovery for markdown-defined subagents from Claude/Agents-style directories."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pythinker_host.path import HostPath

from pythinker_code.subagents.models import AgentTypeDefinition, ToolPolicy
from pythinker_code.utils.frontmatter import parse_frontmatter, strip_frontmatter
from pythinker_code.utils.logging import logger
from pythinker_code.utils.path import find_project_root

AgentScope = Literal["project", "plugin"]

CLAUDE_TOOL_MAP: dict[str, str] = {
    "Agent": "pythinker_code.tools.agent:Agent",
    "Bash": "pythinker_code.tools.shell:Shell",
    "Edit": "pythinker_code.tools.file:StrReplaceFile",
    "Fetch": "pythinker_code.tools.web:FetchURL",
    "Glob": "pythinker_code.tools.file:Glob",
    "Grep": "pythinker_code.tools.file:Grep",
    "Read": "pythinker_code.tools.file:ReadFile",
    "TodoWrite": "pythinker_code.tools.todo:SetTodoList",
    "WebFetch": "pythinker_code.tools.web:FetchURL",
    "WebSearch": "pythinker_code.tools.web:SearchWeb",
    "Write": "pythinker_code.tools.file:WriteFile",
}


@dataclass(frozen=True, slots=True)
class ScopedAgentRoot:
    root: HostPath
    scope: AgentScope
    precedence: int | None = None


@dataclass(frozen=True, slots=True)
class MarkdownAgentSpec:
    name: str
    description: str
    prompt_file: HostPath
    scope: AgentScope
    tools: tuple[str, ...] | None = None
    exclude_tools: tuple[str, ...] | None = None
    model: str | None = None
    when_to_use: str = ""
    required_mcp_servers: tuple[str, ...] = ()
    steps: int | None = None
    prompt_content: str | None = None


@dataclass(frozen=True, slots=True)
class MarkdownAgentSource:
    """One canonical markdown file with its trusted discovery-order metadata."""

    content: str
    prompt_file: HostPath
    scope: AgentScope
    root_ordinal: int
    safe_path: str


def _project_agent_dir_candidates(project_root: HostPath) -> tuple[HostPath, ...]:
    return (
        project_root / ".pythinker" / "agents",
        project_root / ".claude" / "agents",
        project_root / ".agents" / "agents",
        project_root / ".codex" / "agents",
    )


async def resolve_agent_roots(work_dir: HostPath) -> list[ScopedAgentRoot]:
    """Return existing markdown-agent roots in priority order."""
    project_root = await find_project_root(work_dir)
    roots: list[ScopedAgentRoot] = []
    seen: set[str] = set()

    async def add_existing(
        candidates: Iterable[HostPath], scope: AgentScope, precedence_base: int
    ) -> None:
        for offset, candidate in enumerate(candidates):
            try:
                if not await candidate.is_dir():
                    continue
                canon = candidate.canonical()
            except OSError as exc:
                logger.info("Skipping agent directory {path}: {error}", path=candidate, error=exc)
                continue
            key = str(canon)
            if key in seen:
                continue
            seen.add(key)
            roots.append(
                ScopedAgentRoot(
                    root=canon,
                    scope=scope,
                    precedence=precedence_base + offset,
                )
            )

    await add_existing(_project_agent_dir_candidates(project_root), "project", 1)

    # Enabled plugins (pythinker/Claude/Codex installs) contribute agent roots
    # below project scope, so a project-local agent of the same name wins.
    from pythinker_code.plugin.integration import plugin_agent_dirs

    plugin_roots = [HostPath.unsafe_from_local_path(d) for d in plugin_agent_dirs()]
    await add_existing(plugin_roots, "plugin", 1_000)
    return roots


async def discover_markdown_agents(roots: Iterable[ScopedAgentRoot]) -> list[MarkdownAgentSpec]:
    """Discover Claude/Agents-style ``*.md`` subagent definitions."""
    by_name: dict[str, MarkdownAgentSpec] = {}
    for source in await discover_markdown_agent_sources(roots):
        try:
            spec = parse_markdown_agent(
                source.content,
                prompt_file=source.prompt_file,
                scope=source.scope,
            )
        except ValueError as exc:
            logger.info(
                "Skipping invalid markdown agent {path}: {error}",
                path=source.prompt_file,
                error=exc,
            )
            continue
        by_name.setdefault(spec.name.casefold(), spec)
    return sorted(by_name.values(), key=lambda s: s.name)


async def discover_markdown_agent_sources(
    roots: Iterable[ScopedAgentRoot],
    *,
    on_error: Callable[[str, str], None] | None = None,
) -> tuple[MarkdownAgentSource, ...]:
    """Read canonical markdown sources in deterministic root and filename order."""
    sources: list[MarkdownAgentSource] = []
    seen: set[str] = set()
    ordered_roots = sorted(roots, key=_agent_root_order)
    for root_ordinal, scoped in enumerate(ordered_roots):
        entries: list[HostPath] = []
        try:
            async for entry in scoped.root.iterdir():
                if not entry.name.lower().endswith(".md"):
                    continue
                try:
                    if await entry.is_dir():
                        continue
                except OSError as exc:
                    if on_error is not None:
                        on_error(
                            f"{scoped.scope}[{root_ordinal}]/{entry.name}",
                            "unreadable_optional_source",
                        )
                    logger.info(
                        "Skipping unreadable markdown agent {path}: {error}",
                        path=entry,
                        error=exc,
                    )
                    continue
                entries.append(entry)
        except OSError as exc:
            if on_error is not None:
                on_error(
                    f"{scoped.scope}[{root_ordinal}]",
                    "unreadable_optional_root",
                )
            logger.warning(
                "Failed to iterate agent directory {path}: {error}",
                path=scoped.root,
                error=exc,
            )
            continue
        for entry in sorted(entries, key=lambda item: (item.name.casefold(), item.name)):
            try:
                canonical = str(entry.canonical())
                if canonical in seen:
                    continue
                content = await entry.read_text(encoding="utf-8")
            except OSError as exc:
                if on_error is not None:
                    on_error(
                        f"{scoped.scope}[{root_ordinal}]/{entry.name}",
                        "unreadable_optional_source",
                    )
                logger.info(
                    "Skipping unreadable markdown agent {path}: {error}",
                    path=entry,
                    error=exc,
                )
                continue
            seen.add(canonical)
            sources.append(
                MarkdownAgentSource(
                    content=content,
                    prompt_file=entry,
                    scope=scoped.scope,
                    root_ordinal=root_ordinal,
                    safe_path=f"{scoped.scope}[{root_ordinal}]/{entry.name}",
                )
            )
    return tuple(sources)


def _agent_root_order(scoped: ScopedAgentRoot) -> tuple[int, int, str]:
    if scoped.precedence is not None:
        return (scoped.precedence, 0, "")
    root_text = str(scoped.root.canonical())
    if scoped.scope == "plugin":
        return (1_000, 1, root_text)
    parent_name = Path(root_text).parent.name
    project_rank = {
        ".pythinker": 1,
        ".claude": 2,
        ".agents": 3,
        ".codex": 4,
    }.get(parent_name, 100)
    return (project_rank, 1, root_text)


def parse_markdown_agent(
    content: str,
    *,
    prompt_file: HostPath,
    scope: AgentScope,
) -> MarkdownAgentSpec:
    """Parse a Claude-style markdown agent file."""
    fm = parse_frontmatter(content) or {}
    default_name = Path(prompt_file.name).stem
    name = _as_nonempty_str(fm.get("name")) or default_name
    description = _as_nonempty_str(fm.get("description")) or _first_body_line(content) or ""
    model = _as_nonempty_str(fm.get("model"))
    when_to_use = _as_nonempty_str(fm.get("when_to_use")) or description
    tools = _map_tools(fm.get("tools"), source=prompt_file)
    exclude_source = fm.get("disallowed_tools")
    if exclude_source is None:
        exclude_source = fm.get("exclude_tools")
    exclude_tools = _map_tools(exclude_source, source=prompt_file)
    steps = _as_positive_int(fm.get("max_turns")) or _as_positive_int(fm.get("steps"))
    raw_required = fm.get("required_mcp_servers")
    if raw_required is not None and not isinstance(raw_required, list):
        logger.info(
            "Ignoring non-list required_mcp_servers field in markdown agent {path}",
            path=prompt_file,
        )
    required_mcp_servers: tuple[str, ...] = (
        tuple(
            s.strip() for s in cast(list[object], raw_required) if isinstance(s, str) and s.strip()
        )
        if isinstance(raw_required, list)
        else ()
    )
    return MarkdownAgentSpec(
        name=name,
        description=description,
        prompt_file=prompt_file,
        scope=scope,
        tools=tools,
        exclude_tools=exclude_tools,
        model=model,
        when_to_use=when_to_use,
        required_mcp_servers=required_mcp_servers,
        steps=steps,
        prompt_content=content,
    )


def materialize_markdown_agent_specs(
    agents: Iterable[MarkdownAgentSpec],
    *,
    output_dir: Path,
    available_models: set[str] | None = None,
) -> list[AgentTypeDefinition]:
    """Write small Pythinker YAML wrappers and return registered type definitions."""
    output_dir.mkdir(parents=True, exist_ok=True)
    type_defs: list[AgentTypeDefinition] = []
    seen_filenames: set[str] = set()
    for agent in agents:
        filename = _safe_filename(agent.name)
        # Compare casefolded: on case-insensitive filesystems (macOS default)
        # 'Coder-…' and 'coder-…' are the same file, so a case-sensitive set
        # would let the second write silently clobber the first.
        if filename.casefold() in seen_filenames:
            logger.warning(
                "Skipping agent {name!r}: filename {filename!r} collides with another agent",
                name=agent.name,
                filename=filename,
            )
            continue
        seen_filenames.add(filename.casefold())
        wrapper_path = output_dir / f"{filename}.yaml"
        prompt_path = output_dir / f"{filename}.system.md"
        if agent.prompt_content is not None:
            prompt_text = agent.prompt_content
        else:
            try:
                prompt_text = Path(str(agent.prompt_file)).read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "Failed to read markdown agent prompt {path}: {error}",
                    path=agent.prompt_file,
                    error=exc,
                )
                continue
        prompt_path.write_text(strip_frontmatter(prompt_text).strip(), encoding="utf-8")
        payload: dict[str, Any] = {
            "version": 1,
            "agent": {
                "extend": "default",
                "name": agent.name,
                "system_prompt_path": str(prompt_path),
                "when_to_use": agent.when_to_use,
            },
        }
        if agent.model and available_models is not None and agent.model not in available_models:
            logger.warning(
                "Markdown agent {name!r} specifies unknown model {model!r}; "
                "falling back to parent model. Known models: {known}",
                name=agent.name,
                model=agent.model,
                known=sorted(available_models),
            )
        model = agent.model if available_models is None or agent.model in available_models else None
        if model:
            payload["agent"]["model"] = model
        if agent.steps is not None:
            payload["agent"]["steps"] = agent.steps
        if agent.tools is not None:
            payload["agent"]["allowed_tools"] = list(agent.tools)
        if agent.exclude_tools is not None:
            payload["agent"]["exclude_tools"] = list(agent.exclude_tools)
        wrapper_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        policy = (
            ToolPolicy(mode="allowlist", tools=agent.tools)
            if agent.tools is not None
            else ToolPolicy(mode="inherit")
        )
        type_defs.append(
            AgentTypeDefinition(
                name=agent.name,
                description=agent.description,
                agent_file=wrapper_path,
                when_to_use=agent.when_to_use,
                default_model=model,
                tool_policy=policy,
                required_mcp_servers=agent.required_mcp_servers,
            )
        )
    return type_defs


def _map_tools(value: Any, *, source: HostPath) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        logger.info("Ignoring non-list tools field in markdown agent {path}", path=source)
        return None
    mapped: list[str] = []
    for item in cast(list[Any], value):
        if not isinstance(item, str):
            continue
        tool = CLAUDE_TOOL_MAP.get(item)
        if tool is None:
            logger.warning(
                "Ignoring unknown markdown-agent tool {tool} in {path}",
                tool=item,
                path=source,
            )
            continue
        if tool not in mapped:
            mapped.append(tool)
    return tuple(mapped)


def _as_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 1:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed >= 1 else None
    return None


def _as_nonempty_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _first_body_line(content: str) -> str | None:
    for line in strip_frontmatter(content).splitlines():
        stripped = line.strip()
        if stripped and stripped != "---":
            return stripped
    return None


def _safe_filename(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name).strip("._")
    digest = hashlib.sha256(name.casefold().encode("utf-8")).hexdigest()[:8]
    return f"{safe or 'agent'}-{digest}"
