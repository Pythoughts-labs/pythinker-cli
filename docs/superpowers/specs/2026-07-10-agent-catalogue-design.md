# Resolved agent catalogue design

**Status:** Approved as phase 5 of the agent core deepening program

## Problem

Pythinker resolves built-in and configured YAML agents through recursive `AgentSpec` inheritance, while external Markdown agents use separate discovery, frontmatter parsing, materialized wrapper files, and `AgentTypeDefinition` construction. The two paths converge late through mutable `LaborMarket` registration. Validation, identity normalization, source precedence, collision handling, and diagnostics therefore lack one owner.

Unknown YAML and Markdown fields are currently ignored. This hides misspellings and stale configuration while making an immediate strict transition risky for existing custom agents.

## Goals

- Resolve YAML and Markdown sources into one immutable catalogue.
- Keep source-specific parsing behind adapters.
- Centralize identity, precedence, provenance, collision policy, and diagnostics.
- Warn for unknown fields during one compatibility release and support strict rejection through the same implementation.
- Preserve launch behavior through `AgentTypeDefinition`, `LaborMarket`, and generated-wrapper compatibility adapters.
- Keep runnable `Agent` loading outside the catalogue.

## Non-goals

- Introducing remote or plugin-defined catalogue provider protocols.
- Removing generated Markdown wrappers before direct launch parity is proven.
- Moving Runtime, MCP connection, tool construction, or prompt rendering into the catalogue.
- Changing tool-policy semantics or built-in agent names.
- Making optional malformed Markdown agents fatal to startup.

## Module and interface

`pythinker_code.subagents.catalogue` becomes the deep module.

```python
class UnknownFieldPolicy(StrEnum):
    WARN = "warn"
    FORBID = "forbid"

@dataclass(frozen=True, slots=True)
class AgentProvenance:
    source_kind: str
    source_id: str
    scope: str
    precedence: int

@dataclass(frozen=True, slots=True)
class AgentDiagnostic:
    source_kind: str
    safe_path: str
    field_path: str | None
    severity: str
    reason_code: str
    message: str

@dataclass(frozen=True, slots=True)
class ResolvedAgentEntry:
    name: str
    normalized_name: str
    description: str
    launch_spec: ResolvedAgentSpec
    required_mcp_servers: tuple[str, ...]
    supports_background: bool
    provenance: AgentProvenance
    legacy_agent_file: Path | None

@dataclass(frozen=True, slots=True)
class ResolvedAgentCatalogue:
    entries: Mapping[str, ResolvedAgentEntry]
    diagnostics: tuple[AgentDiagnostic, ...]

    def get(self, name: str) -> ResolvedAgentEntry | None: ...
    def require(self, name: str) -> ResolvedAgentEntry: ...
    def values(self) -> tuple[ResolvedAgentEntry, ...]: ...
```

The concrete field names may adapt during implementation planning, but the catalogue must preserve immutable resolved entries and source-aware diagnostics. `source_id` is a safe unique identifier formed from source kind, scope, a trusted logical root label or resolution-order ordinal, and a root-relative path or basename. Raw source-discovery provenance paths are not stored in `AgentProvenance`. Launch paths required by `ResolvedAgentSpec` and `legacy_agent_file` remain internal execution fields on the entry, but no raw launch or provenance path is projected into model context, UI output, telemetry, or diagnostics.

## Source adapters

### YAML adapter

The YAML adapter retains recursive inheritance, cycle detection, relative path rebasing, child overrides, merged system-prompt arguments, subagent merging, and existing required-field validation.

Before Pydantic projection, it inspects raw top-level, `agent`, and nested subagent mappings for unknown fields. Diagnostics use stable field paths such as `agent.unknown_key` and `agent.subagents.coder.unknown_key`. Raw values are never logged.

Referenced YAML is required. Missing, malformed, cyclic, unsupported-version, or invalid known fields remain fail-closed with an actionable `AgentSpecError` causal chain.

### Markdown adapter

The Markdown adapter preserves documented root precedence, canonical path de-duplication, frontmatter aliases, tool mapping, required MCP servers, model selection, and fail-soft file isolation.

It inspects frontmatter for unknown fields before projection. Malformed optional Markdown files produce diagnostics and are skipped without failing the entire runtime. Unexpected programming errors are not mislabeled as harmless configuration when they cannot be handled correctly.

The Markdown adapter constructs a complete `ResolvedAgentSpec`, including system-prompt path and arguments, model, mode, steps, temperature, top-p, tools, allowed tools, excluded tools, hidden state, usage guidance, and nested subagents. Generated YAML and prompt files remain a launch compatibility adapter during the warning release and are referenced only through `legacy_agent_file`. They are not the canonical catalogue representation.

## Identity and precedence

Catalogue identity uses one named function, `normalize_agent_name(name)`, whose warning-release implementation is exactly `name.casefold()`. Display names remain unchanged. `ResolvedAgentCatalogue.get` uses normalized lookup, while the `LaborMarket` compatibility adapter preserves current exact-key behavior until its removal window.

Precedence is explicit:

1. YAML-declared built-in and configured subagents.
2. Project Markdown roots in this order: `.pythinker/agents`, `.claude/agents`, `.agents/agents`, then `.codex/agents`.
3. Enabled plugin Markdown roots in the deterministic order returned by plugin integration, below every project root.

An entry at higher precedence wins over the same normalized name at lower precedence and produces a diagnostic identifying the shadowed source. Two entries with the same normalized name at the same precedence do not silently overwrite:

- Required YAML collision: fail catalogue resolution.
- Optional Markdown collision: keep deterministic first-wins behavior during compatibility rollout and emit a warning diagnostic.
- Strict rollout may reject same-precedence Markdown collisions after documentation and tests establish the migration.

Enumeration order is deterministic by normalized name after resolution. Lookup is case-insensitive and preserves current aliases where documented.

## Unknown-field rollout

The validation implementation supports both `WARN` and `FORBID` from its first release. Production composition uses `WARN` for the first release containing this catalogue, changes to `FORBID` in the immediately following minor release, and cannot remove compatibility adapters before one additional minor release has begun.

During the warning release:

- Emit one aggregated warning per canonical source.
- List stable sorted field paths, not values.
- Preserve current ignore behavior after warning.
- Deduplicate inherited-source warnings by canonical path and field path.
- Make warning diagnostics visible through existing startup/log surfaces.
- Add release notes with the exact strict transition.

In the following release, production composition changes to `FORBID`:

- Required YAML unknown fields raise `AgentSpecError`.
- Optional Markdown entries with unknown fields are rejected as invalid entries and reported through diagnostics.
- No hidden environment variable or local bypass disables strict production validation.

Tests for `FORBID` ship during the warning release, so strict behavior is already exercised before the default changes.

## Compatibility adapters

`ResolvedAgentEntry` projects to existing `AgentTypeDefinition` while launch callers still require it. The projection uses `legacy_agent_file` during the wrapper-compatibility window and uses `launch_spec` for full parity assertions. `LaborMarket` preserves its exact current interface: `builtin_types`, `add_builtin_type`, `get_builtin_type`, and `require_builtin_type`.

`Runtime.labor_market` remains through the warning release and the following strict-default release. Internal callers migrate to `runtime.agent_catalogue`; the earliest adapter removal is the next minor release after strict validation becomes the default. Generated wrappers follow the same minimum window and remain longer if direct-launch parity is not green.

Generated Markdown wrappers remain until `load_agent` can consume a resolved entry or prompt source directly with equivalent prompt, model, tool policy, and required-MCP behavior. Their removal is a separate deletion step, not bundled into initial catalogue resolution.

The catalogue does not construct runnable `Agent` instances. Runtime/model/tool dependencies remain in `soul.agent`, preserving a clean seam between declarative definition and active execution.

## Diagnostics and error contract

Diagnostics distinguish:

- Unknown field warning or rejection.
- Shadowed lower-precedence entry.
- Same-precedence collision.
- Invalid known field.
- Missing required source.
- Unsupported schema version.
- Inheritance cycle.
- Unreadable optional source.
- Materialization failure.

Required source errors stop runtime creation. Optional-source errors remain visible but do not claim the optional entry was loaded. Messages use `source_id` or an existing safe-path renderer and never include raw absolute provenance paths, frontmatter values, prompt contents, credentials, or stack traces.

## Test design

Tests are written before implementation and must first fail for the intended missing behavior.

### Validation

- Unknown top-level YAML field warns exactly once under `WARN`.
- Unknown `agent` field warns exactly once.
- Unknown nested subagent field reports its stable field path.
- Inherited unknown fields warn once for the canonical defining file.
- Unknown Markdown frontmatter warns exactly once.
- The same fixtures reject under `FORBID`.
- Raw unknown values and absolute provenance paths never appear in diagnostics, catalogue entries, UI projections, model context, or telemetry.

### Resolution

- YAML inheritance produces the same resolved behavior as current snapshots.
- Markdown roots preserve documented precedence.
- Required YAML beats project and plugin Markdown.
- Case-only collisions follow the approved required/optional policy.
- Reversed discovery input produces identical catalogue ordering.
- Required MCP servers, model, tools, exclusions, background support, and prompt paths survive projection.
- Malformed optional Markdown is skipped with a diagnostic.
- Malformed required YAML remains fatal.

### Compatibility

- `AgentTypeDefinition` projection matches existing launch fixtures.
- `LaborMarket` lookup and enumeration remain compatible.
- Root and subagent runtimes share the resolved catalogue.
- Agent-list injection and `/agents` output remain stable.
- Markdown launch continues through generated wrappers during the compatibility release.

Focused agent-spec, discovery, load-agent, agent-list, agent-tool, slash, and snapshot tests run before the full Pythinker Code gate.

## Migration and deletion

1. Characterize current YAML and Markdown projections, precedence, and launch behavior.
2. Add unknown-field inspection with `WARN` and `FORBID` policies.
3. Introduce immutable entries, diagnostics, and catalogue resolution.
4. Add YAML and Markdown source adapters using existing parsers.
5. Project catalogue entries to `AgentTypeDefinition` and `LaborMarket`.
6. Publish the catalogue on Runtime and migrate internal readers.
7. Ship the warning release and documentation.
8. Change production policy to `FORBID` in the following release.
9. Teach launch to consume resolved entries directly.
10. Delete generated-wrapper materialization and duplicate DTOs only after parity tests.
11. Remove `LaborMarket` after the documented compatibility window and repository-wide call-site check.

The deletion test passes when removing `ResolvedAgentCatalogue` would spread validation, precedence, normalized identity, collision, provenance, and diagnostics back across YAML loading, Markdown discovery, runtime composition, and the registry.

## Rollback

The warning release is additive and can be reverted without invalidating existing specs. Strict rollout is reverted by restoring the previous released code, not by a hidden bypass. Catalogue projection remains compatible through the warning release and the following strict-default release. The earliest removal release is the next minor after strict becomes default, and removal still requires direct-launch parity and migration documentation.
