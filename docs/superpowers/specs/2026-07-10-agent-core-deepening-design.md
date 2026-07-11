# Agent core deepening program design

**Status:** Approved

**Date:** 2026-07-10

## Purpose

Pythinker Code currently assembles model requests, skill metadata, dynamic guidance, persisted context, agent definitions, and tool execution through several high-capability modules. The audit found that some of those modules are shallow at their current seams: policy and state are reconstructed across callers, failure outcomes disappear, or large internal catalogues leak into model context.

This program deepens those modules without replacing the agent loop or provider layer. Each phase is independently reviewable and releasable. Compatibility adapters remain only for a named migration window and are deleted after callers move to the new interface.

## Goals

- Reduce the default skill catalogue from hundreds of thousands of prompt characters to at most 8,000 characters while preserving deterministic discovery and recall.
- Give every provider request one observable assembly path with provenance, authority, ordering, persistence, budget, and degradation outcomes.
- Make context history replacement atomic from the caller's perspective and make normal appends disk-first.
- Resolve YAML and Markdown agent definitions through one immutable catalogue with source-aware diagnostics.
- Characterize Toolset execution and MCP lifecycle before deciding whether private extraction is justified.
- Preserve existing provider interfaces, JSONL records, tool names, slash behavior, agent precedence, and public configuration unless a phase explicitly documents a migration.

## Non-goals

- Replacing `PythinkerSoul`, `pythinker_core.step`, or provider adapters.
- Adding third-party runtime dependencies.
- Adding cross-process writes to one session file.
- Persisting raw model prompts or request manifests.
- Introducing a generic contribution framework shared by unrelated domains.
- Splitting `PythinkerToolset` because of file length alone.
- Removing generated Markdown agent wrappers before direct launch parity is proven.

## Architecture

The program uses contract-first staged deepening. Each deep module owns the policy that belongs at its seam, exposes a narrow interface, and keeps compatibility projections outside its implementation.

1. `SkillCatalog` owns skill precedence, normalized indexing, deterministic task search, exact resolution, and bounded prompt projection.
2. `RequestAssembler` owns request fragments, requiredness, ordering, budgeting, persistence policy, sanitized provenance, and provider-ready history.
3. `Context` owns atomic history replacement, rotation archives, disk-first append semantics, and coherent in-memory state.
4. `ResolvedAgentCatalogue` owns source adapters, validation, precedence, normalized identity, collisions, provenance, and diagnostics.
5. `PythinkerToolset` remains the external interface. Benchmarks and deterministic fault tests decide whether private execution or MCP lifecycle extraction would increase depth and locality.

The deletion test governs every phase. Once migrated, deleting a deep module must cause its policy and state logic to reappear across several callers. A compatibility adapter that only forwards calls does not count as the new module and must have a removal condition.

## Approved defaults

### Delivery

The work ships as independently reviewable phases on one umbrella branch. Every phase has focused red-green-refactor cycles, its own review gate, documentation, and changelog entry when shipped code changes.

### Skill discovery

- Search the complete in-memory catalogue deterministically and return a bounded task-relevant view.
- Preserve exhaustive exact-name resolution and scope precedence.
- Preserve an exhaustive compatibility mapping while callers migrate.
- Require 100 percent expected recall on the approved representative fixture.
- Cap rendered candidate content at 8,000 characters.
- Require warm in-memory retrieval median below 100 ms on the 1,000-entry benchmark fixture.
- Keep an exhaustive fallback and do not remove it until recall parity tests pass.

### Request assembly

- Keep the sanitized manifest in memory only.
- Expose the latest manifest through `/prompt-manifest`.
- Treat AGENTS.md as authoritative and non-budgeted.
- Treat permissions and applicable model-defense guidance as required.
- Treat other current dynamic providers as best-effort unless a separate invariant proves requiredness.
- Required fragments fail closed and are never truncated.
- Optional failure produces explicit degradation.

### Context persistence

- Use same-directory temporary output and atomic replacement for full history rewrites.
- Preserve numbered rotation archives.
- Serialize in-process writes with one lock.
- Persist normal appends before mutating memory.
- Preserve current JSONL records and torn-tail restoration.
- Keep Context on its current local `Path` seam, do not claim cross-process safety, and distinguish POSIX directory synchronization from platforms that provide visibility atomicity only.

### Agent definitions

- Warn once per source for unknown YAML and Markdown fields during one compatibility release.
- Support strict rejection in the same validation module and test it during the warning release.
- Change production to strict rejection in the following release.
- Preserve required YAML fail-closed behavior and optional Markdown fail-soft behavior.

### Toolset

- Add no split before measurements and deterministic fault characterization.
- Treat a no-go decision as a valid completed phase.
- Keep `PythinkerToolset.handle` and existing MCP methods compatible if private modules are extracted.

## Phase sequence

### Phase 1: Characterization and compatibility contracts

Capture the current provider handoff, static prompt behavior, dynamic reminder ordering, context JSONL records, agent projections, and Toolset lifecycle. Add reusable fixtures and benchmarks before changing implementation behavior.

### Phase 2: Bounded skill discovery

Introduce `SkillCatalog`, preserve `Runtime.skills` as a compatibility adapter, and move live and prompt-inspection rendering onto one catalogue implementation. A narrow soul adapter projects bounded task-relevant candidates into provider-visible history without persistence until Phase 3 absorbs that projection.

Detailed design: [Bounded skill catalogue](./2026-07-10-skill-catalogue-design.md).

### Phase 3: Observable request assembly

Introduce typed request fragments and outcomes, reserve required guidance, produce a sanitized manifest, preserve provider handoff bytes under compatibility conditions, absorb and delete the Phase 2 skill-projection adapter, and add `/prompt-manifest`.

Detailed design: [Observable request assembly](./2026-07-10-request-assembly-design.md).

### Phase 4: Transactional context persistence

Add semantic full-history replacement, migrate pruning, compaction, revert, and clear flows, make appends disk-first, and delete compensating clear/rebuild rollback logic.

Detailed design: [Transactional context persistence](./2026-07-10-context-transactions-design.md).

### Phase 5: Resolved agent catalogue

Unify source resolution and diagnostics while retaining `LaborMarket`, `AgentTypeDefinition`, and generated wrappers as compatibility adapters for the warning release.

Detailed design: [Resolved agent catalogue](./2026-07-10-agent-catalogue-design.md).

### Phase 6: Toolset characterization and conditional deepening

Measure execution phases, advertisement, concurrency, cancellation, and MCP lifecycle. Extract private modules only when a documented threshold is crossed.

Detailed design: [Toolset characterization](./2026-07-10-toolset-characterization-design.md).

## Cross-phase data flow

A user message is persisted through `Context`. In Phase 2, a temporary soul adapter asks `SkillCatalog` for a bounded candidate view and adds it only to provider-visible history. In Phase 3, `RequestAssembler` takes ownership of that projection, reads the current task, collects typed provider fragments, admits required fragments first, applies optional budgets, and constructs provider-ready history plus explicit history appends. It stores only a sanitized `RequestManifest` in memory and passes the unchanged provider argument shape to `pythinker_core.step`.

Agent definitions are resolved before tool construction into `ResolvedAgentCatalogue`; compatibility projections feed existing launch and UI callers until those callers migrate. Toolset characterization observes the resulting request/tool path without changing its external interface.

## Failure truthfulness

- Required assembly failure stops before provider invocation and before assembly-owned history mutation.
- Optional assembly failure records a degraded outcome and remains visible to the user through the sanitized manifest.
- Explicitly requested malformed skills return unavailable, not not-found.
- Context replacement leaves the old committed generation authoritative until atomic commit.
- Context cancellation is propagated after any cancellation-shielded commit section leaves memory and disk coherent.
- Required YAML agent errors fail startup. Optional Markdown errors remain isolated diagnostics.
- Toolset profiling does not add silent fallbacks or convert failures into success.

## Compatibility and removal policy

Compatibility adapters are temporary and named in tests and release notes.

- `Runtime.skills` remains an exhaustive mapping until every internal lookup uses `SkillCatalog`.
- Existing injection providers are adapted to typed fragments; they are not duplicated as a second business-logic path.
- Existing Context methods delegate to the new implementation until all callers migrate.
- `LaborMarket` and `AgentTypeDefinition` remain during the unknown-field warning release.
- `PythinkerToolset` remains the caller-facing interface even if private implementation modules are extracted.

An adapter is removed only after repository search proves no internal caller depends on it, focused compatibility tests pass, public documentation includes the removal timing, and the deletion does not widen the interface elsewhere.

## Verification strategy

Each phase begins with a failing behavior test and records the expected failure. Focused tests cover success, malformed input, required failure, optional degradation, cancellation, concurrency, and compatibility. Shared-module changes run `make check-pythinker-code` and `make test-pythinker-code` before the phase is declared complete.

The baseline on the isolated branch had a clean static gate. The first full test run produced one timing-dependent PTY cancellation timeout after 6,654 passes; the immediate focused rerun passed. This is recorded as a pre-existing baseline flake and is not part of the architecture scope.

## Documentation and release obligations

Each shipped-code phase adds an entry under `## Unreleased` in `CHANGELOG.md`. Public syntax and behavior changes update the relevant customization or reference page. The generated documentation changelog is updated only through the documented sync command.

The one-release unknown-field warning and following strict transition require explicit release notes. `/prompt-manifest` requires slash-command documentation and wire/CLI compatibility tests.

## Rollback

Each phase remains independently revertible. Compatibility adapters preserve the old caller shape during rollout. Feature behavior is not controlled by hidden environment flags. If a phase fails its compatibility or performance gate, revert that phase rather than adding a parallel fallback implementation.
