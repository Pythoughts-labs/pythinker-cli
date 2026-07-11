# Bounded skill catalogue design

**Status:** Approved as phase 2 of the agent core deepening program

## Problem

Skill discovery currently resolves every configured root, parses every winning skill, builds an exhaustive mapping, and renders every name, absolute path, and description into the static system prompt. The exhaustive mapping provides valuable behavior for exact `ReadSkill` calls, aliases, local specialization, slash execution, and subagents. The leak is the full catalogue projection into model context and the duplicated construction path used by live runtime and prompt inspection.

The new module must reduce prompt context without making an omitted skill unreachable.

## Goals

- Keep deterministic project, user, extra, and built-in precedence.
- Preserve exhaustive exact resolution and local-specialization behavior.
- Search the complete discovered metadata set while returning bounded results.
- Keep task-dependent candidates out of the static system-prompt prefix.
- Share one implementation between runtime creation and prompt inspection.
- Preserve current callers through a temporary exhaustive mapping adapter.
- Meet the approved recall, size, and warm-retrieval targets without a new dependency.

## Non-goals

- Persisting a cross-session skill index.
- Adding embedding or vector-search dependencies.
- Loading full skill bodies before `ReadSkill` is invoked.
- Removing scope roots, aliases, MCP fallback, or resource manifests.
- Guaranteeing arbitrary semantic recall beyond the approved fixture.

## Module and interface

`pythinker_code.skill.catalog` becomes the deep module. Its caller-facing interface is intentionally small:

```python
@dataclass(frozen=True, slots=True)
class SkillMatch:
    skill: Skill
    score: int
    reasons: tuple[str, ...]

class SkillProjectionStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"

@dataclass(frozen=True, slots=True)
class SkillPromptView:
    matches: tuple[SkillMatch, ...]
    total_count: int
    omitted_count: int
    overflowed_priority_count: int
    rendered_characters: int

@dataclass(frozen=True, slots=True)
class SkillProjectionOutcome:
    status: SkillProjectionStatus
    view: SkillPromptView | None
    reason_code: str | None

class SkillCatalog:
    @classmethod
    async def discover(
        cls,
        roots: Sequence[ScopedSkillsRoot],
    ) -> SkillCatalog: ...

    def resolve(self, name: str) -> Skill | None: ...
    def search(self, query: str, *, limit: int) -> tuple[SkillMatch, ...]: ...
    def prompt_view(self, query: str, *, max_characters: int) -> SkillProjectionOutcome: ...
    def exhaustive_mapping(self) -> Mapping[str, Skill]: ...
```

The signatures are design intent; implementation planning may adjust names to match established repository conventions without widening the interface.

`SkillCatalog` owns:

- Winning-entry selection and scope precedence.
- Normalized exact-name and alias lookup.
- Search normalization and deterministic ranking.
- Prompt-view character accounting.
- Source diagnostics for malformed or unavailable entries.
- The compatibility mapping consumed through `Runtime.skills` during migration.

`format_skills_for_prompt` becomes a rendering adapter over `SkillPromptView`. It does not choose entries.

## Discovery and search

Phase 1 keeps eager metadata discovery so existing exact lookup remains behaviorally identical. Prompt reduction ships before filesystem laziness. A later optimization may reduce startup reads only after it demonstrates the same frontmatter-defined names, precedence, and malformed-file diagnostics.

Search is deterministic and standard-library-only:

1. Normalize the query and candidate text with the existing skill-name normalization rules plus case folding.
2. Promote an exact normalized name or alias match above every fuzzy match.
3. Rank name phrase matches above name-token overlap.
4. Rank name-token overlap above description-token overlap.
5. Use scope precedence only after relevance so a relevant lower-scope skill is not hidden by an unrelated higher-scope skill.
6. Break remaining ties by normalized name and canonical path.
7. Never use randomness, model calls, wall-clock state, or filesystem enumeration order.

The approved recall fixture defines task text, expected winning skill names, explicit ambiguous cases, aliases, and scope-shadowed entries. Recall parity means every expected skill appears within the bounded returned set for that fixture.

## Prompt projection

The static system prompt retains only stable skill invocation policy and a statement that task-relevant candidates arrive per request. It no longer contains the exhaustive catalogue.

For each provider request, `RequestAssembler` asks `SkillCatalog.prompt_view` for the current task. The rendered fragment:

- Contains name, scope, and concise description.
- Omits absolute filesystem paths.
- Includes the total and omitted counts.
- Tells the model to call `ReadSkill` before applying a candidate.
- Is request-only and never appended to persisted conversation history.
- Is capped at 8,000 characters, including headings and omission text.

Active skills and explicit skill names in the current message are priority entries, not required request fragments. The hard cap always wins. Rendering admits explicit names in message order, then active skills from most recently activated to oldest, then implicit matches. It first removes descriptions, then stops adding names when the next complete entry would exceed 8,000 characters. The view records `overflowed_priority_count`, returns a `DEGRADED` outcome with a safe reason code, and renders only the count that still fits. It never truncates a skill name into an ambiguous identifier and never exceeds the cap. Exact loading remains available through `ReadSkill`. The representative recall fixture is sized so this pathological overflow does not redefine its 100 percent recall requirement.

The static system prompt must remain byte-identical across different tasks under the same runtime configuration, protecting exact-prefix provider caching.

## Exhaustive fallback

Bounded output does not mean bounded lookup. `search` evaluates the complete winning metadata set. `resolve` probes the complete normalized mapping. `ReadSkill` continues to perform alias and MCP fallback after filesystem resolution.

When an exact `ReadSkill` name is missing, the tool returns a small ranked suggestion set rather than enumerating every skill. It distinguishes:

- `not_found`: no winning skill or source diagnostic matches the requested name.
- `unavailable`: a matching discovered source exists but cannot be parsed or read.
- `mcp_fallback`: no filesystem skill exists and a connected MCP bridge resolves the name.

These outcomes remain concise model-facing text; internal details stay in structured diagnostics.

## Runtime compatibility

`Runtime` gains a `skill_catalog` field. During migration, `Runtime.skills` remains the exhaustive mapping returned by `SkillCatalog.exhaustive_mapping`. Root and subagent runtime copies share the same catalogue instance.

Live runtime and read-only system-prompt inspection both call the same catalogue discovery and stable static projection code. Prompt inspection reports catalogue counts and the configured cap, but cannot invent task-specific candidates without a task.

Phase 2 remains independently releasable before `RequestAssembler` exists. A narrow `_with_skill_candidates` compatibility adapter in `PythinkerSoul` asks the catalogue for the current-task view and adds one request-only message to `effective_history` after persisted-history construction. It does not select, rank, budget, or persist entries. Phase 3 moves that projection into `RequestAssembler` and deletes the adapter; tests require provider-visible byte parity across the handoff.

The compatibility mapping is removed only after repository search proves that exact resolution, slash execution, local specialization, compaction restore, and all tool callers use the catalogue interface.

## Failure semantics

Unreadable roots and malformed discovered skills remain isolated diagnostics, preserving startup compatibility. Diagnostics include source kind, safe path, category, and actionable reason without file contents.

An explicitly requested unavailable skill does not collapse to not-found. During independently releasable Phase 2, the compatibility adapter stores the latest `SkillProjectionOutcome` in memory on the soul and logs its safe status and reason code; it does not claim a request manifest exists. A failed or degraded projection adds no unbounded prompt fallback. In Phase 3, `RequestAssembler` maps both `SkillProjectionStatus.DEGRADED` and `SkillProjectionStatus.FAILED` to a best-effort `FragmentStatus.DEGRADED` outcome and an overall degraded request. A skill-projection failure never becomes a required-fragment failure or blocks the provider call. Exact resolution remains available if the catalogue itself was successfully constructed.

Catalogue construction failure is fatal only when the winning required project or built-in skill source cannot satisfy an existing invariant. This phase does not promote every optional skill parse warning to startup failure.

## Performance contract

The representative benchmark uses 1,000 in-memory entries after one warm-up search. It runs enough iterations to report a median and avoids filesystem work inside the measured region.

Acceptance gates:

- Expected fixture recall: 100 percent within the configured result limit.
- Rendered task candidate view: at most 8,000 characters.
- Warm in-memory retrieval median: below 100 ms.
- Stable deterministic output across repeated runs and insertion orders.
- No full skill-body reads during search or prompt rendering.

A deterministic operation-count assertion accompanies the wall-clock gate so a noisy machine does not become the only signal of an algorithmic regression.

## Test design

Tests are written before implementation and must first fail for the intended missing behavior.

- Exact name, alias, case, and local specialization preserve current results.
- Project overrides user, extra, and built-in definitions of the same normalized name.
- Search ordering is deterministic under reversed insertion order.
- Explicit and active skills are retained before implicit matches.
- Malformed matching skills return unavailable.
- Missing exact names return bounded suggestions.
- Prompt rendering includes omission counts and no absolute paths.
- Priority-entry overflow produces a structured degraded outcome.
- Phase 2 stores projection failure independently; Phase 3 maps the same outcome into the request manifest.
- Prompt rendering never exceeds 8,000 characters.
- Static system prompt remains identical for different tasks.
- Root and subagent runtimes share one catalogue.
- Live and inspection construction share the same implementation.
- The 1,000-entry recall and warm-performance fixture passes.

Focused verification covers skill discovery, skill prompt rendering, `ReadSkill`, runtime loading, default-agent snapshots, and wire skill behavior before the full Pythinker Code gate.

## Migration and deletion

1. Add characterization fixtures for current precedence, exact resolution, and prompt rendering.
2. Introduce `SkillCatalog` with exhaustive behavior only.
3. Route runtime and prompt inspection through the catalogue while preserving current rendered output.
4. Add deterministic search and bounded task projection.
5. Move task candidates into request assembly and replace the static exhaustive list with stable policy.
6. Change missing `ReadSkill` output to bounded suggestions.
7. Migrate internal mapping callers.
8. Delete direct discovery/index/render orchestration from runtime callers.
9. Remove `Runtime.skills` only after its documented compatibility window and repository-wide call-site check.

The deletion test passes when removing `SkillCatalog` would force precedence, indexing, ranking, diagnostics, prompt accounting, and exact fallback logic back into runtime, prompt inspection, and `ReadSkill` callers.

## Rollback

The phase is reverted as one unit if recall, prompt-size, provider-prefix, or compatibility tests fail. No hidden environment switch preserves a parallel production path. The exhaustive mapping adapter exists for caller compatibility, not as an alternate implementation.
