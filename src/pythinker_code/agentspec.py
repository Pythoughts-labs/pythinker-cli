from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple, cast

import yaml
from pydantic import BaseModel, Field, ValidationError

from pythinker_code.exception import AgentSpecError

DEFAULT_AGENT_SPEC_VERSION = "1"
SUPPORTED_AGENT_SPEC_VERSIONS = (DEFAULT_AGENT_SPEC_VERSION,)

type AgentMode = Literal["primary", "subagent", "all", "hidden"]


def get_agents_dir() -> Path:
    return Path(__file__).parent / "agents"


DEFAULT_AGENT_FILE = get_agents_dir() / "default" / "agent.yaml"
ASK_AGENT_FILE = get_agents_dir() / "default" / "ask.yaml"
DEBUG_AGENT_FILE = get_agents_dir() / "default" / "debug.yaml"
OKABE_AGENT_FILE = get_agents_dir() / "okabe" / "agent.yaml"


class Inherit(NamedTuple):
    """Marker class for inheritance in agent spec."""


inherit = Inherit()


class AgentSpec(BaseModel):
    """Agent specification."""

    extend: str | None = Field(default=None, description="Agent file to extend")
    name: str | Inherit = Field(default=inherit, description="Agent name")  # required
    system_prompt_path: Path | Inherit = Field(
        default=inherit, description="System prompt path"
    )  # required
    system_prompt_args: dict[str, str] = Field(
        default_factory=dict, description="System prompt arguments"
    )
    model: str | None = Field(default=None, description="Default model alias")
    mode: AgentMode | None = Field(
        default=None, description="Agent mode: primary, subagent, all, hidden"
    )
    hidden: bool | None = Field(default=None, description="Hide this agent from default selection")
    steps: int | None = Field(default=None, ge=1, description="Maximum steps per turn")
    temperature: float | None = Field(default=None, ge=0, le=2, description="Model temperature")
    top_p: float | None = Field(default=None, ge=0, le=1, description="Model top-p")
    when_to_use: str | None = Field(default=None, description="Usage guidance")
    tools: list[str] | None | Inherit = Field(default=inherit, description="Tools")  # required
    allowed_tools: list[str] | None | Inherit = Field(default=inherit, description="Allowed tools")
    exclude_tools: list[str] | None | Inherit = Field(
        default=inherit, description="Tools to exclude"
    )
    subagents: dict[str, SubagentSpec] | None | Inherit = Field(
        default=inherit, description="Subagents"
    )


class SubagentSpec(BaseModel):
    """Subagent specification."""

    path: Path = Field(description="Subagent file path")
    description: str = Field(description="Subagent description")


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedAgentSpec:
    """Resolved agent specification."""

    name: str
    system_prompt_path: Path
    system_prompt_args: dict[str, str]
    model: str | None
    mode: AgentMode
    hidden: bool
    steps: int | None
    temperature: float | None
    top_p: float | None
    when_to_use: str
    tools: list[str]
    allowed_tools: list[str] | None
    exclude_tools: list[str]
    subagents: dict[str, SubagentSpec]


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentSpecSourceValidation:
    """Unknown fields observed in one canonical YAML source before projection."""

    source_path: Path
    field_paths: tuple[str, ...]


def load_agent_spec(agent_file: Path) -> ResolvedAgentSpec:
    """
    Load agent specification from file.

    Raises:
        FileNotFoundError: If the agent spec file is not found.
        AgentSpecError: If the agent spec is not valid.
    """
    agent_spec, _ = load_agent_spec_validated(agent_file)
    return agent_spec


def load_agent_spec_validated(
    agent_file: Path,
    *,
    forbid_unknown_fields: bool = False,
) -> tuple[ResolvedAgentSpec, tuple[AgentSpecSourceValidation, ...]]:
    """Load a spec and report raw unknown fields from every inherited source."""
    validations: dict[Path, AgentSpecSourceValidation] = {}
    agent_spec = _load_agent_spec(
        agent_file,
        _validations=validations,
        _forbid_unknown_fields=forbid_unknown_fields,
    )
    assert agent_spec.extend is None, "agent extension should be recursively resolved"
    if isinstance(agent_spec.name, Inherit):
        raise AgentSpecError("Agent name is required")
    if isinstance(agent_spec.system_prompt_path, Inherit):
        raise AgentSpecError("System prompt path is required")
    if isinstance(agent_spec.tools, Inherit):
        raise AgentSpecError("Tools are required")
    if isinstance(agent_spec.allowed_tools, Inherit):
        agent_spec.allowed_tools = None
    if isinstance(agent_spec.exclude_tools, Inherit):
        agent_spec.exclude_tools = []
    if isinstance(agent_spec.subagents, Inherit):
        agent_spec.subagents = {}
    resolved = ResolvedAgentSpec(
        name=agent_spec.name,
        system_prompt_path=agent_spec.system_prompt_path,
        system_prompt_args=agent_spec.system_prompt_args,
        model=agent_spec.model,
        mode=agent_spec.mode or "primary",
        hidden=bool(agent_spec.hidden),
        steps=agent_spec.steps,
        temperature=agent_spec.temperature,
        top_p=agent_spec.top_p,
        when_to_use=agent_spec.when_to_use or "",
        tools=agent_spec.tools or [],
        allowed_tools=agent_spec.allowed_tools,
        exclude_tools=agent_spec.exclude_tools or [],
        subagents=agent_spec.subagents or {},
    )
    return resolved, tuple(validations.values())


def _load_agent_spec(
    agent_file: Path,
    _visited: set[Path] | None = None,
    *,
    _validations: dict[Path, AgentSpecSourceValidation] | None = None,
    _forbid_unknown_fields: bool = False,
) -> AgentSpec:
    resolved = agent_file.resolve()
    if _visited is None:
        _visited = set()
    if resolved in _visited:
        raise AgentSpecError(f"Cyclic agent extend chain detected at {agent_file}")
    _visited.add(resolved)
    if not agent_file.exists():
        raise AgentSpecError(f"Agent spec file not found: {agent_file}")
    if not agent_file.is_file():
        raise AgentSpecError(f"Agent spec path is not a file: {agent_file}")
    try:
        with open(agent_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise AgentSpecError(f"Invalid YAML in agent spec file: {e}") from e
    if not isinstance(data, dict):
        raise AgentSpecError(f"Agent spec file must contain a mapping: {agent_file}")
    data = cast("dict[str, Any]", data)

    unknown_fields = _unknown_agent_spec_fields(data)
    if unknown_fields:
        if _forbid_unknown_fields:
            fields = ", ".join(unknown_fields)
            raise AgentSpecError(f"Unknown fields in required agent source: {fields}")
        if _validations is not None and resolved not in _validations:
            _validations[resolved] = AgentSpecSourceValidation(
                source_path=resolved,
                field_paths=unknown_fields,
            )

    version = str(data.get("version", DEFAULT_AGENT_SPEC_VERSION))
    if version not in SUPPORTED_AGENT_SPEC_VERSIONS:
        raise AgentSpecError(f"Unsupported agent spec version: {version}")

    try:
        agent_spec = AgentSpec(**data.get("agent", {}))
    except (TypeError, ValidationError) as exc:
        raise AgentSpecError("Agent spec contains an invalid known field") from exc
    if isinstance(agent_spec.system_prompt_path, Path):
        agent_spec.system_prompt_path = (
            agent_file.parent / agent_spec.system_prompt_path
        ).absolute()
    if isinstance(agent_spec.subagents, dict):
        for v in agent_spec.subagents.values():
            v.path = (agent_file.parent / v.path).absolute()
    if agent_spec.extend:
        if agent_spec.extend == "default":
            base_agent_file = DEFAULT_AGENT_FILE
        else:
            base_agent_file = (agent_file.parent / agent_spec.extend).absolute()
        base_agent_spec = _load_agent_spec(
            base_agent_file,
            _visited,
            _validations=_validations,
            _forbid_unknown_fields=_forbid_unknown_fields,
        )
        if not isinstance(agent_spec.name, Inherit):
            base_agent_spec.name = agent_spec.name
        if not isinstance(agent_spec.system_prompt_path, Inherit):
            base_agent_spec.system_prompt_path = agent_spec.system_prompt_path
        for k, v in agent_spec.system_prompt_args.items():
            # system prompt args should be merged instead of overwritten
            base_agent_spec.system_prompt_args[k] = v
        if agent_spec.model is not None:
            base_agent_spec.model = agent_spec.model
        if agent_spec.mode is not None:
            base_agent_spec.mode = agent_spec.mode
        if agent_spec.hidden is not None:
            base_agent_spec.hidden = agent_spec.hidden
        if agent_spec.steps is not None:
            base_agent_spec.steps = agent_spec.steps
        if agent_spec.temperature is not None:
            base_agent_spec.temperature = agent_spec.temperature
        if agent_spec.top_p is not None:
            base_agent_spec.top_p = agent_spec.top_p
        if agent_spec.when_to_use is not None:
            base_agent_spec.when_to_use = agent_spec.when_to_use
        if not isinstance(agent_spec.tools, Inherit):
            base_agent_spec.tools = agent_spec.tools
        if not isinstance(agent_spec.allowed_tools, Inherit):
            base_agent_spec.allowed_tools = agent_spec.allowed_tools
        if not isinstance(agent_spec.exclude_tools, Inherit):
            base_agent_spec.exclude_tools = agent_spec.exclude_tools
        if not isinstance(agent_spec.subagents, Inherit):
            if isinstance(agent_spec.subagents, dict) and isinstance(
                base_agent_spec.subagents, dict
            ):
                # Child entries WIN on key conflicts; base entries fill the rest.
                base_agent_spec.subagents = {
                    **base_agent_spec.subagents,
                    **agent_spec.subagents,
                }
            else:
                base_agent_spec.subagents = agent_spec.subagents
        agent_spec = base_agent_spec
    return agent_spec


def _unknown_agent_spec_fields(data: dict[str, Any]) -> tuple[str, ...]:
    unknown = [key for key in data if key not in {"version", "agent"}]
    raw_agent = data.get("agent")
    if not isinstance(raw_agent, dict):
        return tuple(sorted(unknown))
    agent = cast("dict[str, Any]", raw_agent)
    known_agent_fields = set(AgentSpec.model_fields)
    unknown.extend(f"agent.{key}" for key in agent if key not in known_agent_fields)
    raw_subagents = agent.get("subagents")
    if isinstance(raw_subagents, dict):
        known_subagent_fields = set(SubagentSpec.model_fields)
        for name, raw_subagent in cast("dict[object, object]", raw_subagents).items():
            if not isinstance(name, str) or not isinstance(raw_subagent, dict):
                continue
            subagent = cast("dict[str, object]", raw_subagent)
            unknown.extend(
                f"agent.subagents.{name}.{key}"
                for key in subagent
                if key not in known_subagent_fields
            )
    return tuple(sorted(unknown))
