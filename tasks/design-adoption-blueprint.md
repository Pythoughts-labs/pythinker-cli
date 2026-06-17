# Design Adoption Blueprint — cleaner layering for pythinker

Source: multi-agent architecture study (12 subsystem maps, 2 architect lenses, adversarial
verification per recommendation) comparing pythinker against a cleanly layered reference
agent harness (local clone under `external reference `, gitignored). All recommendations below
survived adversarial verification against both codebases. Each is independently landable
and behavior-preserving unless flagged.

## Diagnosis

Pythinker's runtime behavior is fundamentally sound — typed wire events, fail-closed
permission gates, persisted-everything subagents, exemplary ContextVar discipline. The
structural debt is **concentrated, not diffuse**:

- Three god modules fuse loop, policy, and integration glue:
  `soul/pythinkersoul.py` (2390 lines), `soul/permission.py` (1377), `soul/toolset.py` (1330).
- `soul` ↔ `tools` import each other in both directions, patched with dozens of
  function-local imports.
- Core reaches into UI in two places: `soul/toolset.py:944` (toast import) and
  `tools/usage.py` pricing display.
- `cli/__init__.py` (~700-line typer callback) + `PythinkerCLI.create` (~290 lines)
  fuse a dozen lifecycle concerns.
- Naming requires tribal knowledge: `skill/` (code) vs `skills/` (data),
  `agents/` (data) vs `subagents/` (code).
- The "turn" boundary is private: `flow_runner.py` and `slash.py` call `soul._turn`
  with pyright suppressions.

Reference design moves worth adopting (the *style*, not the language idioms):

1. **Functional core, stateful shell** — pure loop functions over (context, config,
   emit, signal); a stateful class owns transcript, queues, run lifecycle.
2. **Everything observable is an event** — internal state is updated by reducing the
   same event stream UIs subscribe to, so views can't diverge.
3. **One contracts surface** — closed event union + hook contracts with documented
   must-not-throw invariants in one types module that reads as a spec.
4. **Declarative metadata on definitions** — per-tool / per-agent-type capability
   metadata on the definition object, never string-matching on names/modules.
5. **Layered construction** — services (cwd-bound deps) → session → runtime factory,
   with **diagnostics returned as data** (app layer decides what's fatal).
6. **Tools own their full behavior contract** — schema, limits interpolated into the
   LLM-facing description from the same constants the code enforces, truncation that
   always names the next action, self-described UI metadata.
7. **Tiny system prompt** — capability comes from tools/repo files/lazy skill indexes;
   code enforces mechanics, the prompt encodes only judgment.

## Ranked recommendations (verified)

### P1a (L) Split PythinkerSoul into stateful shell + extracted loop/recovery/compaction modules
Extract from `soul/pythinkersoul.py`: turn-loop sequencing, connection/OAuth recovery,
and compaction orchestration into sibling modules; the soul keeps state + lifecycle.
- Cheap first slice: recovery extraction (two methods already static; rest need only
  `_runtime`/`_current_step_no`).
- Couplings: 11 test files (39 hits) monkeypatch `soul._step`/`_agent_loop`/`_turn` —
  keep thin delegating methods or budget test migration. `_agent_loop`/`_step` touch
  ~15 `self` members; expect file-stratum readability, not full purity.
- No circular imports (slash.py imports the soul TYPE_CHECKING-only); no wire-snapshot churn.

### P1b (L) Public turn contract + functional-core/stateful-shell loop split
Step 1 (low-risk): make `turn()` public so `flow_runner.py:212` and `slash.py:285,300`
stop calling a private with pyright suppressions. Define nesting semantics for the
ralph path (FlowRunner currently nests TurnBegin inside `run()`'s framing).
Step 2 (genuinely L): hoist `_step`'s pure parts; the deps surface includes sleep
inhibitor, telemetry, event bus, hook engine, `_settle_shielded` cancellation — design
the seam around those. ~20 test files patch loop internals; reroute deliberately.

### P2a (M) Split toolset.py: dispatch pipeline / dedup state machine / MCP module; remove core→UI toast
- **Security-critical coupling**: `permission.py:426` and `toolset.py:139` string-match
  `module == 'pythinker_code.soul.toolset'` + qualname `MCPTool`/`WireExternalTool`.
  Moving MCPTool without updating both **fails OPEN on a permission gate**. Fix the
  detection to declarative flags as part of the move (see P3a).
- MCP lifecycle is entangled with toolset instance state (`_mcp_servers`,
  `_register_mcp_tools`, `runtime.mcp_tools`) → extract as a delegate class, not a file
  move; ~10 call sites in pythinkersoul/agent need a facade.
- Toast removal: notification hub + shell toast subscription (`ui/shell/__init__.py:856`)
  and `StatusUpdate.mcp_status` already exist as the right channel.

### P2b (L, two PRs) Per-tool-call pipeline: prepare / execute / finalize with discriminated outcomes
Restructure `toolset.handle` (toolset.py:514-773, ~260 lines, three nested closures).
- `handle()` must stay sync per the core Toolset protocol → prepare's permission/PreToolUse
  stages run inside the created task.
- Cancellation contract (toolset.py:759-767): finalize must run in the same task, never a
  wrapper task; the same-step dedup join is an awaiting Task, not an immediate outcome.

### P3a (M) Tools as a real layer below soul: neutral contracts module + declarative per-tool metadata
Kill string matching: `extract_key_argument`'s 25-name match (tools/__init__.py:29-120),
`hasattr(tool, '_approval')` duck-typing (toolset.py:125-129), module/qualname matching
(toolset.py:132-140, permission.py:421-427) → flags/metadata on tool definitions.
- `key_argument` consumers (`ui/shell/visualize/_blocks.py:860`, `acp/session.py:109`)
  only have wire tool names → needs a name→spec registry, not just a class attribute;
  behaviors are pinned by `tests/tools/test_extract_key_argument.py`.
- Riskiest sub-part: Protocol views of Runtime/Approval collide with DI —
  `toolset._load_tool` keys deps by exact annotation identity (agent.py:504-515).
- Keep `SkipThisTool` tools-owned (soul→tools is already the correct direction).
- Easy wins first: flags + pricing-display move.
- Precedent: `emits_tool_execution_started_after_approval` (tools/agent/__init__.py:564).

### P3b (M) Consolidate per-subagent-type behavior onto AgentTypeDefinition
Today scattered: tool policy in `agents/default/*.yaml`, permission floor in
`soul/permission.py:86-101`, summary min-lengths in `subagents/runner.py:45-56`,
explore-only git-context branch in `subagents/core.py:90`. Silent-fallback bug is real
(planner/scout missing from the tables). `tool_policy` and `supports_background` already
live on AgentTypeDefinition — extend that pattern.
- **Security**: do NOT let markdown frontmatter declare `permission_profile`
  (project-local agents could self-escalate); keep the read_only fallback and plan-mode
  downgrade (permission.py:283-286) applied to the *resolved* profile.
- Couplings: `tests/core/test_permission_profiles.py` (43 tests) uses bare
  subagent_type strings with empty labor markets → register type defs in fixtures or
  keep a name-table fallback; `test_summary_continuation.py` imports the private tables.

### P3c (L, hard) Decompose CLI entry: entry → mode resolution → services → runtime composition
Split the ~700-line typer callback (`cli/__init__.py`) and `PythinkerCLI.create`
(app.py:163-453) into layered factories with diagnostics-as-data.
- Corrections from verification: create() does not print inline (stderr pre-redirected
  to loguru; warnings surface via run_shell banner) — diagnostics motivation is weaker
  here; pythinker never consults TTY for mode selection — do NOT add TTY sniffing.
- Hidden couplings: Reload/ExitCode/Input-OutputFormat re-exports (~15 importers,
  circular-import risk with app.py), test_startup_imports lazy-import pins, flock
  release-before-reacquire ordering, per-session attach_sink for multi-session ACP.

### P4a (M) Compaction/prune orchestration out of the soul: prepare/execute split with one rollback
Duplicated rollback confirmed (pythinkersoul.py:1988-2001 vs 2095-2173) → one shared
`with_context_rewrite()` guard. `SimpleCompaction.prepare()` (compaction.py:194-238)
already supplies the pure plan half. PostCompact/SessionStart hooks fire inside the
guarded rewrite; usage accounting, root-role task snapshot, injection re-arm stay
soul-coupled → runner takes callbacks. Realistic reduction ~150-200 lines.
`test_context_pruning.py` pins rollback behavior (aids verification).

### P4b (M) Background runner gets a public seam; finish AgentTypeDefinition consolidation
`background/agent_runner.py` is a privacy-violation zone (file-wide
reportPrivateUsage=false; imports `_SUMMARY_MIN_LENGTH_BY_TYPE`; pokes
`manager._live_agent_tasks`, `_mark_task_running`, `_mark_task_awaiting_approval`).
Promote a narrow interface on the manager. ~15 test sites poke the same privates across
4 test files (incl. tests/background/test_manager.py:545-617) — migrate them with it.

### P5a (M) Uniform tool result surface: every tool owns status, brief/tail, untrusted wrapping
Older file tools (ReadFile/WriteFile/Glob) return bare ToolOk/ToolError; newer tools use
ToolResultBuilder with `extras.status`. Migrate stragglers.
- **Real bug found**: FetchURL writes pre-wrapped untrusted text into the builder
  (web/fetch.py:232,277,338) so truncation cuts the closing `</untrusted_data>` tag AND
  breaks `strip_untrusted_envelope` (endswith check) → envelope leaks to display.
  Fix by switching to `mark_untrusted()` (wrap after truncation).
- Byte-identical ReadFile migration needs `max_line_length=None` + raised max_chars
  (100KB cap vs builder's 50K default; marker text differs).
- Pins: `test_untrusted_wrapping.py` WRAPPER_RE; `test_extract_key_argument.py`
  (Bash|Shell alias, path normalization, raw-JSON default must survive).

### P5b (M) Split skill/__init__.py (872 lines) into named submodules; disambiguate code-vs-data dirs
Submodules: roots/discovery/frontmatter/prompt-rendering/resources. Decide owner of the
Skill model. `skills/` and `agents/` data dirs have no `__init__.py` — prefer a README
in data dirs over making them importable. ~12 tests monkeypatch barrel attributes
(`get_builtin_skills_dir`, `_supports_builtin_skills`, `_SKILL_RESOURCE_SCAN_CEILING` —
tests/core/test_skill.py, tests/tools/test_skill_tool.py:142) → retarget or re-export.

## Rejected (do not pursue as designed)

- **Telemetry seam derived from the wire stream**: the wire does not carry the needed
  facts (auto/yolo approvals resolve pre-wire; ToolResult lacks duration/error_type;
  CompactionEnd fires even on failure; dedup/slash/skill/agent_stuck never hit the wire).
  Salvageable kernel: a scoped wire subscriber for turn lifecycle, manual approvals,
  StepRetry only. Full inline-`track()` cleanup would require wire-protocol enrichment
  (serde back-compat + tests_e2e snapshots + ACP/web clients).

## Anti-patterns in the reference (do not import)

- Its own god files (3135-line session class, 5741-line interactive mode) — adopt the
  boundaries, not the file sizes.
- Throw-by-default hook errors that fail a user turn after state was committed.
- Stringly-typed event bus channels; last-result-wins multi-handler hook chaining.
- Whole-file session load with no torn-line repair and no multi-writer locking
  (pythinker is already ahead here — keep our invariants).
- Magic-string provider detection inside providers; in-place mutation with scratch fields.

## Suggested landing order

1. P1b step 1 (public `turn()`) + P3a easy wins (flags, pricing move) + P5a FetchURL bug fix — small, immediate.
2. P1a recovery slice → compaction (P4a) → loop split (P1b step 2).
3. P2a toolset split (with the permission-gate string-match fix) → P2b pipeline.
4. P3b type-def consolidation → P4b background seam.
5. P5b skill split; P3c CLI decomposition last (hardest, most coupled).
