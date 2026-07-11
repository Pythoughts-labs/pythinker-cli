# Agent Core Deepening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Deliver the current-release implementation of all six approved agent-core-deepening phases while preserving provider, JSONL, tool, slash, agent, and configuration compatibility.

**Architecture:** Add four deep policy owners—SkillCatalog, RequestAssembler, transactional Context, and ResolvedAgentCatalogue—behind temporary compatibility projections. Characterize PythinkerToolset with deterministic tests and a local benchmark harness, then extract at most one private state machine only if an approved threshold is reproducibly crossed.

**Tech Stack:** Python 3.14, asyncio, Pydantic, pytest, uv, Ruff, Pyright, ty, and standard-library filesystem/timing primitives.

## Global Constraints

- Use uv or repository make targets for every Python command.
- Add no third-party runtime or benchmark dependency.
- Preserve pythinker_core.step, provider adapters, JSONL record shapes, tool names, slash behavior, agent precedence, and public configuration unless this plan names the change.
- Keep Runtime.skills, Runtime.labor_market, AgentTypeDefinition, generated Markdown wrappers, and PythinkerToolset as compatibility surfaces for their approved migration windows.
- Required guidance fails closed and is never truncated; optional guidance reports degradation explicitly.
- Never persist raw prompts, request manifests, prompt fragments, raw provenance paths, credentials, or user text as diagnostics or telemetry.
- Keep Context.file_backend on the local Path seam and make no cross-process safety claim.
- Phase 5 ships WARN as production unknown-field policy and tests FORBID; strict production activation belongs to the following minor release.
- Phase 6 treats a reproducible no-go decision as completion and forbids extraction based only on file length.
- Each production slice follows red then green at the named public seam, followed by focused tests and make check-pythinker-code.
- Each shipped-code phase adds an Unreleased bullet to CHANGELOG.md. Generated docs changelog changes only through npm run sync from docs/.

## Settled Interpretation

- The skill search query is the latest real user turn supplied explicitly by the soul; reminder-only and tool-result messages are not treated as the task.
- Explicit skill mentions are distinct, left-to-right $<exact-catalogue-name> and /skill:<exact-catalogue-name> tokens. Plain prose names use normal search ranking.
- Request assembly owns main agent-turn provider requests and the /btw aligned-history path. Compaction and blind-advisor calls remain specialized internal model operations outside the dynamic source matrix.
- Phase 6 writes evidence to docs/superpowers/reports/2026-07-10-toolset-characterization.json and the matching .md decision record.

---

### Task 1: Phase 1 compatibility characterization

**Files:**
- Create: tests/core/test_provider_handoff_contract.py
- Modify: tests/core/test_skills_prompt.py
- Modify: tests/core/test_context.py
- Modify: tests/core/test_load_agent.py
- Modify: tests/core/test_agent_list_injection.py

**Interfaces:**
- Consumes: current provider handoff, skill rendering, JSONL restoration, agent projection, and Toolset facade.
- Produces: executable compatibility contracts used by later tasks.

- [ ] Write a provider-handoff test that captures the four positional arguments to pythinker_core.step and independently asserts stable system prompt bytes, AGENTS.md position, dynamic reminder ordering, normalization, and persisted-versus-effective history.

~~~python
async def test_agent_step_has_one_characterized_provider_handoff(soul, monkeypatch):
    captured: list[tuple[str, tuple[Message, ...]]] = []

    async def capture(_provider, system_prompt, _toolset, history, **_kwargs):
        captured.append((system_prompt, tuple(history)))
        return StepResult(message=Message(role="assistant", content=[]), usage=None)

    monkeypatch.setattr(pythinker_core, "step", capture)
    await soul._step()
    assert captured == [(soul.agent.system_prompt, expected_effective_history)]
    assert tuple(soul.context.history) == expected_persisted_history
~~~

- [ ] Add literal mixed-record JSONL and AgentTypeDefinition projection fixtures. Do not derive expected values with production serializers.
- [ ] Run the pre-change gate:

~~~bash
uv run pytest -q tests/core/test_provider_handoff_contract.py tests/core/test_skills_prompt.py tests/core/test_context.py tests/core/test_load_agent.py tests/core/test_agent_list_injection.py tests/core/test_mcp_lifecycle.py tests/core/test_toolset_concurrency.py
~~~

- [ ] Commit with subject: test(core): characterize agent request contracts

### Task 2: Phase 2 exhaustive SkillCatalog seam

**Files:**
- Create: src/pythinker_code/skill/catalog.py
- Create: tests/core/test_skill_catalog.py
- Modify: src/pythinker_code/skill/__init__.py

**Interfaces:**
- Consumes: ScopedSkillsRoot, Skill, normalize_skill_name, and existing discovery.
- Produces: SkillCatalog.discover, resolve, search, prompt_view, and exhaustive_mapping.

- [ ] Write failing exact-name, case, alias, first-root precedence, reversed-input determinism, and unavailable-diagnostic tests.
- [ ] Implement frozen SkillMatch, SkillPromptView, SkillProjectionOutcome, status/diagnostic types, and one catalogue-owned winning index.

~~~python
class SkillCatalog:
    @classmethod
    async def discover(cls, roots: Sequence[ScopedSkillsRoot]) -> SkillCatalog: ...
    def resolve(self, name: str) -> Skill | None: ...
    def search(self, query: str, *, limit: int) -> tuple[SkillMatch, ...]: ...
    def prompt_view(self, query: str, *, max_characters: int) -> SkillProjectionOutcome: ...
    def exhaustive_mapping(self) -> Mapping[str, Skill]: ...
~~~

- [ ] Retain malformed-source diagnostics during discovery rather than reconstructing them in ReadSkill.
- [ ] Verify with uv run pytest -q tests/core/test_skill_catalog.py tests/core/test_skills_prompt.py tests/tools/test_skill_tool.py and make check-pythinker-code.
- [ ] Commit with subject: feat(skills): add exhaustive skill catalogue

### Task 3: Phase 2 bounded runtime discovery

**Files:**
- Create: tests/fixtures/skill_catalog_recall.json
- Create: tests/core/test_pythinkersoul_skill_projection.py
- Modify: src/pythinker_code/skill/catalog.py
- Modify: src/pythinker_code/skill/__init__.py
- Modify: src/pythinker_code/soul/agent.py
- Modify: src/pythinker_code/soul/pythinkersoul.py
- Modify: src/pythinker_code/tools/skill/__init__.py
- Modify: src/pythinker_code/agents/default/system.md
- Modify: src/pythinker_code/cli/system_prompt.py
- Modify: tests/conftest.py
- Modify: tests/tools/test_skill_tool.py

**Interfaces:**
- Consumes: Task 2 catalogue.
- Produces: bounded deterministic candidates, shared Runtime.skill_catalog, exhaustive Runtime.skills adapter, and bounded ReadSkill suggestions.

- [ ] Write red tests for relevance tiers, reversed insertion order, explicit/active priority, 8,000-character hard cap, complete names only, omission counts, no absolute paths/body reads, 100 percent fixture recall, and 1,000-entry warm median below 100 ms with an operation-count assertion.
- [ ] Implement exact/phrase/name-token/description-token ranking, then scope/name/path tie breakers.
- [ ] Add required Runtime.skill_catalog, derive Runtime.skills from exhaustive_mapping, and share catalogue identity in copy_for_subagent.
- [ ] Replace static exhaustive prompt content with stable invocation policy and route prompt inspection through the same discovery implementation.
- [ ] Add the temporary request-only soul projection. A degraded/failed projection stores safe status/reason and never adds an exhaustive fallback.
- [ ] Migrate ReadSkill while preserving local specialization and MCP fallback; distinguish not_found, unavailable, and mcp_fallback.
- [ ] Verify the catalogue, prompt, runtime, soul projection, ReadSkill, MCP bridge, and wire skill suites; run make check-pythinker-code.
- [ ] Commit with subject: feat(skills): bound provider-visible discovery

### Task 4: Phase 3 request admission engine

**Files:**
- Create: src/pythinker_code/soul/request_assembly.py
- Create: tests/core/test_request_assembly.py
- Modify: src/pythinker_code/soul/dynamic_injection.py

**Interfaces:**
- Consumes: Message, shared token estimator, normalize_history, and a SkillCatalog projection port.
- Produces: immutable fragments/outcomes/manifest/input/result and categorized errors.

- [ ] Write failing tests for required-first admission, non-budgeted AGENTS.md, stable equal-priority ordering, truncatable versus omitted content, redaction, aggregate accounting, and required failure.
- [ ] Implement the approved enums and frozen data contracts.

~~~python
@dataclass(frozen=True, slots=True)
class RequestAssemblyInput:
    system_prompt: str
    persisted_history: tuple[Message, ...]
    current_task: str
    budget_tokens: int

@dataclass(frozen=True, slots=True)
class AssembledRequest:
    system_prompt: str
    provider_history: tuple[Message, ...]
    history_appends: tuple[Message, ...]
    manifest: RequestManifest
~~~

- [ ] Recompute all estimates inside the assembler, reserve required content, and keep request-only content out of history_appends.
- [ ] Preserve the legacy budget API as a projection over explicit outcomes; do not retain a second admission algorithm.
- [ ] Verify request assembly, legacy budget, normalization, and make check-pythinker-code.
- [ ] Commit with subject: feat(soul): add observable request assembler

### Task 5: Phase 3 provider and soul integration

**Files:**
- Create: tests/core/test_request_assembly_providers.py
- Create: tests/core/test_request_assembly_soul.py
- Modify: src/pythinker_code/soul/pythinkersoul.py
- Modify: src/pythinker_code/soul/dynamic_injection.py
- Modify: src/pythinker_code/soul/dynamic_injections/permissions_state.py
- Modify: src/pythinker_code/soul/dynamic_injections/model_defense.py
- Modify: src/pythinker_code/soul/btw.py

**Interfaces:**
- Consumes: Tasks 3 and 4.
- Produces: one agent-turn assembly path, explicit provider outcomes, acknowledgement/rearm, and latest manifest.

- [ ] Write red tests for every legacy provider as best-effort, exception degradation, required permission/model-defense failure, allowed not_applicable, disabled optional bus behavior, and persistence-failure retry.
- [ ] Assign requiredness/persistence in trusted composition: AGENTS.md required request-only/non-budgeted; permissions and applicable model defense required; unnamed providers best-effort.
- [ ] Move one-shot acknowledgement after successful persistence.
- [ ] Replace _step orchestration: assemble, store manifest, persist history_appends once, then invoke provider. Persistence failure stores context_persistence_failed and skips provider invocation.
- [ ] Delete _collect_injections and the temporary Phase 2 adapter only after byte-equivalence tests pass.
- [ ] Align /btw provider history through the assembler while keeping the side question request-only.
- [ ] Verify request assembly, provider, main-step, retry, /btw, and make check-pythinker-code.
- [ ] Commit with subject: feat(soul): route agent turns through request assembly

### Task 6: Phase 3 manifest observability

**Files:**
- Create: tests/core/test_prompt_manifest_slash.py
- Modify: src/pythinker_code/soul/slash.py
- Modify: src/pythinker_code/soul/pythinkersoul.py
- Modify: src/pythinker_code/telemetry/metrics.py
- Modify: tests_e2e/test_wire_protocol.py
- Modify: docs/en/reference/slash-commands.md

**Interfaces:**
- Consumes: latest in-memory RequestManifest.
- Produces: /prompt-manifest text and content-free aggregate telemetry.

- [ ] Write failing no-data, success, degraded, failure, and redaction tests.
- [ ] Register a normal soul slash command; add no wire event.
- [ ] Emit only counts, budgets, stable source IDs, and duration to telemetry.
- [ ] Verify slash, wire protocol, telemetry, and make check-pythinker-code.
- [ ] Commit with subject: feat(soul): expose sanitized prompt manifests

### Task 7: Phase 4 reducer and disk-first appends

**Files:**
- Create: tests/core/test_context_transactions.py
- Modify: src/pythinker_code/soul/context.py

**Interfaces:**
- Consumes: existing JSONL records and restore repair.
- Produces: one serializer/reducer, one mutation lock, disk-first append_messages, checkpoint, usage, and system-prompt writes.

- [ ] Write failing serialization/open/write/flush fault tests that assert exact old bytes and memory; include checkpoint ID and usage-counter failure.
- [ ] Extract an immutable reducer for system prompt, repaired history, authoritative/pending tokens, next checkpoint ID, and tail state.
- [ ] Serialize before locking, append one complete batch, flush, then swap precomputed memory without another await. Keep append_message as a compatibility delegate.
- [ ] Add event/barrier concurrency and queued-writer cancellation tests without sleeps.
- [ ] Verify context transaction, restore, repair, pending-token suites, and make check-pythinker-code.
- [ ] Commit with subject: feat(context): make incremental writes disk first

### Task 8: Phase 4 atomic replacement

**Files:**
- Modify: src/pythinker_code/soul/context.py
- Modify: tests/core/test_context_transactions.py

**Interfaces:**
- Consumes: Task 7 reducer/serializer/lock and next_available_rotation.
- Produces: ContextReplacement, ContextCommit, ContextPersistenceError, and replace_history.

- [ ] Write failing temp creation, record write, flush, temp fsync, archive, replace, directory fsync, cleanup, and cancellation tests.
- [ ] Implement a restrictive unique same-directory temp file, exact-byte numbered archive, os.replace, no-await memory swap, POSIX directory fsync, and BaseException cleanup preserving the primary cause.
- [ ] Shield only replace-plus-swap and re-raise cancellation after coherence.
- [ ] Verify replace/archive/cancel/concurrency tests and make check-pythinker-code.
- [ ] Commit with subject: feat(context): replace history atomically

### Task 9: Phase 4 soul-flow migration

**Files:**
- Modify: src/pythinker_code/soul/pythinkersoul.py
- Modify: src/pythinker_code/soul/slash.py
- Modify: src/pythinker_code/soul/context.py
- Modify: tests/core/test_context_pruning.py
- Modify: tests/core/test_compaction_restore.py
- Modify: tests_e2e/test_wire_sessions.py

**Interfaces:**
- Consumes: Task 8 replace_history.
- Produces: one-commit prune, compact, revert, and clear flows with no compensating rebuild.

- [ ] Rewrite failure tests at replace_history and assert exact old bytes/state.
- [ ] Prepare all prune/compact semantic messages and tokens first, call replace_history once, preserve CompactionEnd in finally, and rearm providers after commit.
- [ ] Delete both clear/rebuild rollback blocks.
- [ ] Delegate revert_to and clear to replacement; make /clear include current system prompt in one reset.
- [ ] Verify prune, compact, context, and wire clear/compact suites; run make check-pythinker-code.
- [ ] Commit with subject: feat(context): migrate history rewrites to transactions

### Task 10: Phase 5 validation and catalogue

**Files:**
- Create: src/pythinker_code/subagents/catalogue.py
- Create: tests/core/test_agent_catalogue_validation.py
- Create: tests/core/test_agent_catalogue_markdown.py
- Modify: src/pythinker_code/agentspec.py
- Modify: src/pythinker_code/subagents/discovery.py

**Interfaces:**
- Consumes: existing recursive YAML loader and Markdown parser/materializer.
- Produces: UnknownFieldPolicy, safe diagnostics/provenance, immutable catalogue entries, and source adapters.

- [ ] Write failing WARN/FORBID tests for top-level, agent, nested subagent, inherited, and Markdown unknown fields; assert stable paths and no raw values/absolute paths.
- [ ] Inspect raw mappings before Pydantic. WARN aggregates once per source and continues; FORBID fails required YAML and rejects only the optional Markdown entry.
- [ ] Use exactly name.casefold() for normalize_agent_name.
- [ ] Resolve existing behavior without duplicating inheritance; defensively freeze/copy nested collections.
- [ ] Preserve YAML fatality, Markdown isolation/root precedence, plugin order, deterministic enumeration, shadow diagnostics, required collision failure, and optional collision warning.
- [ ] Verify catalogue validation, Markdown, agent spec, discovery, and make check-pythinker-code.
- [ ] Commit with subject: feat(agents): resolve definitions through a catalogue

### Task 11: Phase 5 compatibility publication

**Files:**
- Create: tests/core/test_agent_catalogue_compat.py
- Modify: src/pythinker_code/soul/agent.py
- Modify: src/pythinker_code/subagents/runner.py
- Modify: src/pythinker_code/tools/agent/__init__.py
- Modify: src/pythinker_code/soul/dynamic_injections/agent_list.py
- Modify: src/pythinker_code/ui/shell/slash.py
- Modify: tests/conftest.py
- Modify: docs/en/customization/agents.md

**Interfaces:**
- Consumes: Task 10.
- Produces: shared Runtime.agent_catalogue, exact AgentTypeDefinition/LaborMarket projection, and WARN production composition.

- [ ] Write failing launch projection, wrapper parity, exact LaborMarket, case-insensitive catalogue, root/subagent identity, agent-list, /agents, MCP, background, hidden, and tool-policy tests.
- [ ] Construct once before tools, populate LaborMarket, retain generated wrappers and dependency injection, and migrate readers without changing output.
- [ ] Document WARN now, FORBID in the next minor, and earliest adapter removal one additional minor later after launch parity.
- [ ] Verify compatibility, builder/load/default agent, Agent tool, /agents, wire, and make check-pythinker-code.
- [ ] Commit with subject: feat(agents): publish resolved agent catalogue

### Task 12: Phase 6 schema and harness

**Files:**
- Create: src/pythinker_code/benchmark/toolset_characterization.py
- Create: scripts/benchmark_toolset.py
- Create: tests/core/test_toolset_characterization.py
- Create: docs/en/contributing/toolset-characterization.md

**Interfaces:**
- Consumes: public PythinkerToolset and standard-library timing/platform APIs.
- Produces: machine-readable schema, deterministic evaluator, and local runner.

- [ ] Write failing pure five-run crossed, uncrossed, and inconclusive tests: four crossings plus crossing median; one outlier requests one rerun.
- [ ] Implement typed environment/fixture/raw-sample/median/p95/throughput/hash/cancellation/leak/decision fields.
- [ ] Measure 1/10/100 safe/exclusive/mixed calls, 1 KiB/100 KiB/1 MiB dedupe, 50/500/5,000 advertisement, and 1/10/50 MCP fixtures, excluding setup and tool duration.
- [ ] Verify schema tests, runner help, and make check-pythinker-code.
- [ ] Commit with subject: test(toolset): add characterization harness

### Task 13: Phase 6 fault matrix and decision

**Files:**
- Modify: tests/core/test_toolset.py
- Modify: tests/core/test_toolset_concurrency.py
- Modify: tests/core/test_mcp_lifecycle.py
- Modify: tests/core/test_mcp_cleanup.py
- Create: docs/superpowers/reports/2026-07-10-toolset-characterization.json
- Create: docs/superpowers/reports/2026-07-10-toolset-characterization.md
- Conditional create: exactly one approved private Toolset module if a threshold crosses.

**Interfaces:**
- Consumes: Task 12 and approved thresholds.
- Produces: deterministic failure coverage and a reproducible go/no-go decision.

- [ ] Add event-driven pre/post-hook, telemetry-policy, gate cancellation, permit recovery, and later-call tests.
- [ ] Add hung/method-not-found/transient/duplicate/list-storm/race/cancel/timeout/partial-publication MCP tests with deterministic hashes and leak assertions.
- [ ] Run five isolated measurements:

~~~bash
uv run python scripts/benchmark_toolset.py --scenario all --runs 5 --output docs/superpowers/reports/2026-07-10-toolset-characterization.json
~~~

- [ ] Write every raw value, median, repeatability verdict, environment, crossed/uncrossed/inconclusive threshold, and decision to the Markdown record.
- [ ] If none cross, record no-go and do not refactor. If one crosses, first add a failing assertion, move only that state machine, delete moved state from PythinkerToolset, rerun identical fixtures, and revert if the target/locality does not improve.
- [ ] Verify Toolset, concurrency, MCP lifecycle/cleanup/startup, characterization, and make check-pythinker-code.
- [ ] Commit with subject: test(toolset): characterize execution and lifecycle

### Task 14: Release, full verification, and review

**Files:**
- Modify: CHANGELOG.md
- Modify: tasks/todo.md
- Conditional generated modify: docs/en/release-notes/changelog.md

**Interfaces:**
- Consumes: Tasks 1–13.
- Produces: release notes, completed ledger/review, full gates, and reviewed branch.

- [ ] Add one precise Unreleased bullet per shipped phase, including compatibility windows and /prompt-manifest. Sync generated changelog only through docs/npm run sync.
- [ ] Run provider snapshots:

~~~bash
uv run --directory packages/pythinker-core pytest -q tests/api_snapshot_tests/test_openai_responses.py tests/api_snapshot_tests/test_anthropic.py
~~~

- [ ] Apply pythinker-guard, test-guard, and clean-code-guard to the branch diff.
- [ ] Run fresh, unpiped make check, make test-pythinker-code, and git diff --check.
- [ ] If only the documented PTY cancellation node flakes, rerun that node on branch and base before classification; treat other failures as caused until disproven.
- [ ] Run the requested two-axis code review from fixed point 46ea01e001117489e0fe7fb57d29678253e2d2ea against the umbrella and five detailed specs. Fix Critical/Important findings and rerun affected tests.
- [ ] Record outcome, deviations, benchmark decision, exact verification, migration windows, and blockers in tasks/todo.md.
- [ ] Commit final metadata with subject: docs(core): record agent deepening release

## Plan Self-Review

- Coverage: all six phases, compatibility adapters, current WARN policy, Phase 6 no-go option, docs, changelog, and deletion conditions are assigned.
- Placeholders: the only conditional branch is the spec-required measured Toolset decision, bounded to the three approved private modules.
- Type ordering: SkillCatalog precedes RequestAssembler; Context replacement precedes flow migration; agent catalogue precedes runtime publication.
- Scope: future-minor strict activation and adapter deletion are documented obligations, not incorrectly shipped now.
