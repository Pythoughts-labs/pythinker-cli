"""Immutable resolution of required YAML and optional markdown agent definitions."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import cast

from pydantic import ConfigDict

from pythinker_code.agentspec import (
    AgentSpecSourceValidation,
    ResolvedAgentSpec,
    SubagentSpec,
    load_agent_spec_validated,
    render_agent_field_segment,
)
from pythinker_code.exception import AgentSpecError
from pythinker_code.subagents.discovery import (
    MarkdownAgentSource,
    ScopedAgentRoot,
    discover_markdown_agent_sources,
    materialize_markdown_agent_specs,
    parse_markdown_agent,
)
from pythinker_code.utils.frontmatter import parse_frontmatter

_MARKDOWN_FIELDS = frozenset(
    {
        "description",
        "disallowed_tools",
        "exclude_tools",
        "max_turns",
        "model",
        "name",
        "required_mcp_servers",
        "steps",
        "tools",
        "when_to_use",
    }
)


class UnknownFieldPolicy(StrEnum):
    WARN = "warn"
    FORBID = "forbid"


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentProvenance:
    source_kind: str
    source_id: str
    scope: str
    precedence: int


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentDiagnostic:
    source_kind: str
    safe_path: str
    field_path: str | None
    severity: str
    reason_code: str
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedAgentEntry:
    name: str
    normalized_name: str
    description: str
    launch_spec: ResolvedAgentSpec
    required_mcp_servers: tuple[str, ...]
    supports_background: bool
    provenance: AgentProvenance
    legacy_agent_file: Path | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedAgentCatalogue:
    entries: Mapping[str, ResolvedAgentEntry]
    diagnostics: tuple[AgentDiagnostic, ...] = ()
    _index: Mapping[str, ResolvedAgentEntry] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        copied: dict[str, ResolvedAgentEntry] = {}
        for entry in self.entries.values():
            normalized = normalize_agent_name(entry.name)
            if normalized in copied:
                raise ValueError("Agent catalogue contains a normalized name collision")
            copied[normalized] = replace(
                entry,
                normalized_name=normalized,
                launch_spec=_freeze_launch_spec(entry.launch_spec),
                required_mcp_servers=tuple(entry.required_mcp_servers),
            )
        copied = dict(sorted(copied.items()))
        frozen = MappingProxyType(copied)
        object.__setattr__(self, "entries", frozen)
        object.__setattr__(self, "_index", frozen)
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

    def get(self, name: str) -> ResolvedAgentEntry | None:
        return self._index.get(normalize_agent_name(name))

    def require(self, name: str) -> ResolvedAgentEntry:
        entry = self.get(name)
        if entry is None:
            raise KeyError(name)
        return entry

    def values(self) -> tuple[ResolvedAgentEntry, ...]:
        return tuple(self._index.values())


class _FrozenSubagentSpec(SubagentSpec):
    model_config = ConfigDict(frozen=True)


def normalize_agent_name(name: str) -> str:
    return name.casefold()


async def resolve_agent_catalogue(
    *,
    agent_file: Path,
    markdown_roots: Iterable[ScopedAgentRoot],
    materialized_dir: Path,
    available_models: set[str] | None = None,
    unknown_field_policy: UnknownFieldPolicy = UnknownFieldPolicy.WARN,
) -> ResolvedAgentCatalogue:
    """Resolve required YAML subagents and optional markdown agents once."""
    root_spec, root_validations = load_agent_spec_validated(
        agent_file,
        forbid_unknown_fields=unknown_field_policy is UnknownFieldPolicy.FORBID,
    )
    entries: dict[str, ResolvedAgentEntry] = {}
    diagnostics: list[AgentDiagnostic] = []
    reported_yaml_fields: set[tuple[Path, tuple[str, ...]]] = set()
    _append_yaml_diagnostics(diagnostics, root_validations, reported_yaml_fields)

    for declared_name, declared_spec in root_spec.subagents.items():
        normalized = normalize_agent_name(declared_name)
        if normalized in entries:
            raise AgentSpecError("Required YAML agent name collision after normalization")
        launch_spec, validations = load_agent_spec_validated(
            declared_spec.path,
            forbid_unknown_fields=unknown_field_policy is UnknownFieldPolicy.FORBID,
        )
        _append_yaml_diagnostics(diagnostics, validations, reported_yaml_fields)
        entries[normalized] = ResolvedAgentEntry(
            name=declared_name,
            normalized_name=normalized,
            description=declared_spec.description,
            launch_spec=_freeze_launch_spec(launch_spec),
            required_mcp_servers=(),
            supports_background=not launch_spec.hidden,
            provenance=AgentProvenance(
                source_kind="yaml",
                source_id=f"yaml:required:{_safe_source_token(declared_spec.path)}",
                scope="required",
                precedence=0,
            ),
            legacy_agent_file=declared_spec.path,
        )

    discovery_errors: list[tuple[str, str]] = []
    sources = await discover_markdown_agent_sources(
        markdown_roots,
        on_error=lambda safe_path, reason: discovery_errors.append((safe_path, reason)),
    )
    diagnostics.extend(
        AgentDiagnostic(
            source_kind="markdown",
            safe_path=safe_path,
            field_path=None,
            severity="warning",
            reason_code=reason,
            message="Optional markdown source could not be read",
        )
        for safe_path, reason in discovery_errors
    )
    for source in sources:
        _resolve_markdown_source(
            source=source,
            entries=entries,
            diagnostics=diagnostics,
            materialized_dir=materialized_dir,
            available_models=available_models,
            unknown_field_policy=unknown_field_policy,
        )

    return ResolvedAgentCatalogue(entries=entries, diagnostics=tuple(diagnostics))


def _append_yaml_diagnostics(
    diagnostics: list[AgentDiagnostic],
    validations: tuple[AgentSpecSourceValidation, ...],
    reported: set[tuple[Path, tuple[str, ...]]],
) -> None:
    for validation in validations:
        identity = (validation.source_path, validation.field_paths)
        if identity in reported:
            continue
        reported.add(identity)
        diagnostics.append(
            AgentDiagnostic(
                source_kind="yaml",
                safe_path=f"required/{_safe_source_token(validation.source_path)}",
                field_path=", ".join(validation.field_paths),
                severity="warning",
                reason_code="unknown_field",
                message="Required agent source contains unknown fields",
            )
        )


def _resolve_markdown_source(
    *,
    source: MarkdownAgentSource,
    entries: dict[str, ResolvedAgentEntry],
    diagnostics: list[AgentDiagnostic],
    materialized_dir: Path,
    available_models: set[str] | None,
    unknown_field_policy: UnknownFieldPolicy,
) -> None:
    try:
        frontmatter = parse_frontmatter(source.content) or {}
        unknown_fields: list[str] = []
        has_invalid_key = False
        for key in cast("dict[object, object]", frontmatter):
            if isinstance(key, str) and key in _MARKDOWN_FIELDS:
                continue
            segment, is_unsafe = render_agent_field_segment(key)
            unknown_fields.append(segment)
            has_invalid_key = has_invalid_key or is_unsafe
        unknown_fields.sort()
        if unknown_fields:
            diagnostics.append(
                _unknown_markdown_diagnostic(
                    source,
                    tuple(unknown_fields),
                    unknown_field_policy,
                    invalid_key=has_invalid_key,
                )
            )
            if has_invalid_key or unknown_field_policy is UnknownFieldPolicy.FORBID:
                return
        spec = parse_markdown_agent(
            source.content,
            prompt_file=source.prompt_file,
            scope=source.scope,
        )
    except ValueError:
        diagnostics.append(
            _source_diagnostic(
                source,
                severity="warning",
                reason_code="invalid_known_field",
                message="Optional markdown agent is invalid and was skipped",
            )
        )
        return

    normalized = normalize_agent_name(spec.name)
    precedence = source.root_ordinal + 1
    existing = entries.get(normalized)
    if existing is not None:
        reason_code = (
            "same_precedence_collision"
            if existing.provenance.precedence == precedence
            else "shadowed_source"
        )
        diagnostics.append(
            _source_diagnostic(
                source,
                severity="warning",
                reason_code=reason_code,
                message="Optional markdown agent was skipped by catalogue precedence",
            )
        )
        return

    try:
        definitions = materialize_markdown_agent_specs(
            (spec,),
            output_dir=materialized_dir,
            available_models=available_models,
        )
        if not definitions:
            diagnostics.append(
                _source_diagnostic(
                    source,
                    severity="warning",
                    reason_code="materialization_failure",
                    message="Optional markdown agent could not be materialized",
                )
            )
            return
        definition = definitions[0]
        launch_spec, _ = load_agent_spec_validated(definition.agent_file)
    except (AgentSpecError, OSError):
        diagnostics.append(
            _source_diagnostic(
                source,
                severity="warning",
                reason_code="materialization_failure",
                message="Optional markdown agent could not be materialized",
            )
        )
        return

    entries[normalized] = ResolvedAgentEntry(
        name=spec.name,
        normalized_name=normalized,
        description=spec.description,
        launch_spec=_freeze_launch_spec(launch_spec),
        required_mcp_servers=spec.required_mcp_servers,
        supports_background=not launch_spec.hidden,
        provenance=AgentProvenance(
            source_kind="markdown",
            source_id=(f"markdown:{source.scope}:{source.root_ordinal}:{source.prompt_file.name}"),
            scope=source.scope,
            precedence=precedence,
        ),
        legacy_agent_file=definition.agent_file,
    )


def _unknown_markdown_diagnostic(
    source: MarkdownAgentSource,
    field_paths: tuple[str, ...],
    policy: UnknownFieldPolicy,
    *,
    invalid_key: bool = False,
) -> AgentDiagnostic:
    return AgentDiagnostic(
        source_kind="markdown",
        safe_path=source.safe_path,
        field_path=", ".join(field_paths),
        severity=("error" if invalid_key or policy is UnknownFieldPolicy.FORBID else "warning"),
        reason_code="invalid_field_key" if invalid_key else "unknown_field",
        message=(
            "Optional markdown agent contains an invalid field key"
            if invalid_key
            else "Optional markdown agent contains unknown fields"
        ),
    )


def _source_diagnostic(
    source: MarkdownAgentSource,
    *,
    severity: str,
    reason_code: str,
    message: str,
) -> AgentDiagnostic:
    return AgentDiagnostic(
        source_kind="markdown",
        safe_path=source.safe_path,
        field_path=None,
        severity=severity,
        reason_code=reason_code,
        message=message,
    )


def _freeze_launch_spec(spec: ResolvedAgentSpec) -> ResolvedAgentSpec:
    prompt_args = MappingProxyType(dict(spec.system_prompt_args))
    subagents = MappingProxyType(
        {
            name: _FrozenSubagentSpec(
                path=value.path,
                description=value.description,
            )
            for name, value in spec.subagents.items()
        }
    )
    return replace(
        spec,
        system_prompt_args=cast("dict[str, str]", prompt_args),
        tools=cast("list[str]", tuple(spec.tools)),
        allowed_tools=(
            None if spec.allowed_tools is None else cast("list[str]", tuple(spec.allowed_tools))
        ),
        exclude_tools=cast("list[str]", tuple(spec.exclude_tools)),
        subagents=cast("dict[str, SubagentSpec]", subagents),
    )


def _safe_source_token(path: Path) -> str:
    resolved_path = str(path.resolve())
    digest = hashlib.sha256(resolved_path.encode(encoding="utf-8")).hexdigest()[:12]
    return f"{digest}:{path.name}"
