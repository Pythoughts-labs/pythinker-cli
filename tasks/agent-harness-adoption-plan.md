# Agent Harness Adoption Plan — verified gap map

**Generated:** 2026-06-12 from a 14-cluster / 28-agent map+adversarial-verify workflow comparing the
local reference agent harness against `src/pythinker_code`. Every item survived a refutation pass
against live source (124 kept, 3 refuted). `<ref>/` = the reference workspace root under
`external reference ` (Rust crates); pythinker paths are repo-relative. Naming rule: all adopted work is framed as
generic pythinker agent enhancements — no external product names in code, comments, commits, or docs.

## Execution discipline

- One checkpoint = one item (or one tight cluster of S items), TDD (red→green), `clean-code-guard`
  pass on the diff, `make check` + targeted pytest green, then commit. Branch: `feat/agent-harness-enhancements`.
- Multi-writer tree: re-check `git status`/`git log` before each checkpoint; accept and harmonize
  concurrent work, never revert what you did not author.
- Hot files (serialize across checkpoints): `soul/pythinkersoul.py`, `soul/toolset.py`, `config.py`,
  `soul/permission.py`, `agents/default/system.md` (test-pinned: use `--inline-snapshot=fix` deliberately).
- L-effort items get their own design note in `tasks/` before code.

## Tier 1 — high value, S/M effort (27 items)

### `context-mgmt/history-invariant-repair-at-restore-prompt-build-synthesize-` — partial, S, high

**Today.** Partial. Write-time coverage exists: interrupted tool calls get synthetic 'interrupted by user' results (src/pythinker_code/soul/pythinkersoul.py:1763-1790) and torn-line repair fixes partial JSONL writes (src/pythinker_code/soul/context.py:35-44). But normalize_history only merges adjacent user messages (src/pythinker_code/soul/dynamic_injection.py:185-211); a crash between appending the assistant message (with tool_calls) and the tool results leaves a dangling call that, after restore, makes every subsequent API call fail with a pairing error. Image stripping is gated only at ingestion (src/pythinker_code/tools/file/read_media.py:56,194).

**Verifier note.** Claim confirmed as partial. Minor line drift: the synthetic 'Tool call interrupted by user.' results are at pythinkersoul.py:1801-1817 (claim cited 1763-1790).

**Adopt.** Extend normalize_history (or a restore-time pass in Context.restore) to scan for assistant messages whose tool_calls lack a matching tool-role message and insert a synthetic 'aborted' tool result right after, and to drop tool messages whose tool_call_id has no preceding call. Pure-function change, easily unit-tested against crafted crash transcripts.

**Files.** `src/pythinker_code/soul/dynamic_injection.py`, `src/pythinker_code/soul/context.py`, `src/pythinker_code/soul/pythinkersoul.py`

### `prompts-instructions/decision-complete-plan-mode-interviewing-protocol-and-plan-d` — partial, S, high

**Today.** Plan mode is mechanically stronger (tool-enforced read-only, plan file, mandatory Verification section, ExitPlanMode options for multiple approaches, AskUserQuestion guidance, 2-3 approaches max — soul/dynamic_injections/plan_mode.py _full_reminder/_sparse_reminder), but the prompting lacks the discoverable-vs-preference unknowns taxonomy, the recommended-default-and-proceed-as-assumption rule, the decision-completeness bar, and any plan-document structure/brevity rubric.

**Verifier note.** Claim survives for interactive plan mode, but two adjacent implementations were missed and should temper the gap: (1) the recommended-default-and-proceed-as-assumption rule exists nearly verbatim in prompts/best_practices.md line 23 ('enumerate the plausible interpretations and say which one you are taking... otherwise proceed and record the assumption') — though only opt-in via /best-practices (soul/slash.py lines 301-324), not wired into plan mode; (2) the plan subagent spec (agents/default/plan.yaml) enforces a decision-completeness-like bar ('Every load-bearing task must be executable as written. "Figure out X during implementation" is not a task — it is either an explicit explore task or a BLOCKER'; Escalation: 'list the exact questions under BLOCKERS instead of planning on assumptions') plus a full plan-document structure contract (SUMMARY/CONTEXT/TASK DEPENDENCY GRAPH/PLAN/EVIDENCE/RISKS/BLOCKERS). Interactive plan mode itself (plan_mode.py _full_reminder/_sparse_reminder, tools/plan/enter_description.md) has only a rudimentary preference trigger ('When the best approach depends on user preferences... use AskUserQuestion'; 'User Preferences Matter' condition 7) — no discoverable-vs-preference taxonomy, no decision-completeness bar, and no plan-file brevity rubric anywhere.

**Adopt.** Extend _full_reminder (and a line in _sparse_reminder) in plan_mode.py: (a) two-unknowns rule — explore repo-discoverable facts before asking, ask preferences early via AskUserQuestion with 2-4 options + a recommended default, record un-answered defaults as Assumptions; (b) finalization bar — exit only when the plan is decision-complete; (c) plan-file shaping — 3-5 short sections incl. Assumptions, subsystem-grouped bullets, minimal path-naming. Pure prompt edits; update phrase-pinned tests with --inline-snapshot=fix.

**Files.** `src/pythinker_code/soul/dynamic_injections/plan_mode.py`

### `protocol-headless/strict-stdout-stderr-channel-discipline-and-structured-error` — partial, S, high

**Today.** Partial. src/pythinker_code/ui/print/__init__.py uses `from rich import print` to stdout for all failure paths (LLMNotSet, ChatProviderError, MaxStepsReached + handoff, 'Interrupted by user', 'Unknown error') and echoes the command to stdout in text mode — in stream-json mode these plain-text lines corrupt the JSONL stream for parsers. Background-task timeout notices correctly use open_original_stderr(), showing the right pattern exists but is not applied uniformly.

**Verifier note.** Claim confirmed as stated; could not refute. src/pythinker_code/ui/print/__init__.py:18 does `from rich import print`, and every failure path prints plain text to stdout: LLMNotSet (line 412), LLMNotSupported (416), ChatProviderError (420), MaxStepsReached + handoff block (424, 436), 'Interrupted by user' (440), 'Unknown error' (444) — none are gated on output_format, so they corrupt the stream-json stdout channel. The command echo (lines 90-91) IS gated to text mode (`output_format == "text" and not final_only`), matching the claim's wording. No structured error WireMessage type exists for JsonPrinter to emit. The background-timeout notice correctly uses open_original_stderr() (lines 296-308), confirming the right pattern exists but is not applied to the exception handlers.

**Adopt.** In Print.run, route all human-facing diagnostics through the original-stderr writer; when output_format == stream-json, additionally emit a final structured error event (reuse Notification or a new ErrorEvent wire type) on stdout before exiting. Add a unit test asserting every stdout line in stream-json mode parses as JSON across each failure path.

**Files.** `src/pythinker_code/ui/print/__init__.py`, `<ref>/exec/src/lib.rs`

### `review-mode/deterministic-review-target-resolution-and-prompt-synthesis` — partial, S, high

**Today.** The standalone review engine resolves diffs deterministically with base/staged/working-tree/range modes, fallback refs, and a fallback audit trail (packages/pythinker-review/src/pythinker_review/engine/diff_source.py). But agent-mediated review dispatch leaves git scoping entirely to the model — system.md only instructs it prose-style to compute the merge base (src/pythinker_code/agents/default/system.md:120), and git-context injection is gated to explore subagents only (src/pythinker_code/subagents/core.py:90).

**Verifier note.** Claim CONFIRMED as stated; all three cited anchors verified. The standalone engine is deterministic; the agent-mediated path has no deterministic diff scoping anywhere — the review/code-reviewer subagent specs receive scope purely via the parent's prompt text.

**Adopt.** Add a small resolver (target -> prompt + hint) that precomputes the merge-base SHA via the async git helper and renders one of three template prompts (uncommitted / base-branch-with-sha + backup variant / commit-with-title), then prepend it to the review subagent's prompt at dispatch. Also extend collect_git_context injection in subagents/core.py to reviewer-class agents so every review run starts with branch, dirty files, and merge-base already in context instead of burning turns rediscovering them.

**Files.** `src/pythinker_code/subagents/core.py`, `src/pythinker_code/subagents/git_context.py`, `packages/pythinker-review/src/pythinker_review/engine/diff_source.py`, `src/pythinker_code/agents/default/system.md`

### `tools-registry-codemode/concurrency-policy-for-parallel-tool-calls-parallel-safe-too` — missing, S, high

**Today.** PythinkerToolset.handle spawns an asyncio task per call immediately (src/pythinker_code/soul/toolset.py:769) and pythinker_core.step awaits them with no ordering or locking (packages/pythinker-core/src/pythinker_core/__init__.py:86-122). When a provider emits parallel tool calls, WriteFile + Shell + StrReplaceFile from the same assistant message all execute concurrently with no race protection; no lock exists in tools (rg asyncio.Lock).

**Verifier note.** Claim survives. The only sharing mechanism is same-step dedup of byte-identical calls (same tool name + canonical args await the original task); there is no parallel-safe vs mutating classification and no serialization of mutating tools. The external_side_effect_tool flag exists but is consumed only by permission gating/visibility, not concurrency.

**Adopt.** Add a class-level supports_parallel flag on tools (default False for Shell, WriteFile, StrReplaceFile, MultiEdit, plugin/MCP side-effect tools; True for ReadFile/Grep/Glob/recall etc.) and an async read/write gate inside the per-call task in PythinkerToolset.handle: parallel-safe tools acquire shared, others exclusive. Keeps streaming dispatch but makes mutation ordering deterministic.

**Files.** `src/pythinker_code/soul/toolset.py`, `packages/pythinker-core/src/pythinker_core/__init__.py`, `<ref>/core/src/tools/parallel.rs`

### `config-features/per-project-trust-gating-of-project-scope-config-and-hooks` — missing, M, high

**Today.** Missing. _load_scoped in src/pythinker_code/config.py merges .pythinker/config.toml unconditionally; the `hooks` field (shell commands, src/pythinker_code/hooks/config.py HookDef) is NOT in SCOPE_LOCKED_PATHS, and app.py:390 runs HookEngine(config.hooks) — so a cloned repo's project config can auto-execute shell hooks on SessionStart. Trust exists only as a session-scoped flag (src/pythinker_code/session_state.py TrustStateData, /trust at ui/shell/slash.py:1730) that never gates config loading.

**Verifier note.** Claim confirmed. Project-scope config merges unconditionally and project-defined hooks execute with no trust gate. Trust/safe-mode is wired only into the approval layer (auto-approve gating), never into config loading or hook execution. Minor citation fix: /trust is defined at ui/shell/slash.py:1718 (state.trusted set at 1730).

**Adopt.** Persist a project-root -> trust map (in user config or a metadata file, keyed by normalized resolved path; let /trust record it). In _load_scoped, when the project root is untrusted, still read project/local dicts but hold them as disabled scopes with a reason surfaced once at startup ('run /trust to enable project config and hooks'); at minimum gate the hooks list and statusline-adjacent keys behind trust immediately. Treat invalid TOML in untrusted scopes as an empty disabled scope.

**Files.** `src/pythinker_code/config.py`, `src/pythinker_code/hooks/config.py`, `src/pythinker_code/app.py`, `src/pythinker_code/session_state.py`, `<ref>/config/src/loader/mod.rs`

### `config-features/unknown-config-key-detection-with-source-located-diagnostics` — missing, M, high

**Today.** Missing. Config models in src/pythinker_code/config.py use Pydantic's default extra='ignore', so a typo'd key (e.g. 'default_yolo_typo') silently vanishes and changes agent behavior with no signal; validation errors are scope-attributed via _lookup_provenance but carry no positions, and there is no strict mode.

**Verifier note.** Claim confirmed. No extra='forbid'/strict mode anywhere in the config models; typo'd keys are silently dropped. Provenance enrichment attaches only scope file-path strings to validation errors, with no line/column positions.

**Adopt.** After merge, diff the raw per-scope dicts against Config's field tree (recursive walk of model_fields, or model_json_schema) and emit startup warnings naming the file and dotted path of each unrecognized key; add an opt-in strict flag (env or config) that escalates to ConfigError. tomlkit retains item positions if line numbers are wanted later.

**Files.** `src/pythinker_code/config.py`, `<ref>/config/src/strict_config.rs`, `<ref>/config/src/diagnostics.rs`

### `context-mgmt/context-overflow-recovery-shrink-and-retry-instead-of-failin` — missing, M, high

**Today.** Missing. classify_api_error tags 'context_overflow' for telemetry only (src/pythinker_code/soul/pythinkersoul.py:166-203); _is_retryable_error (pythinkersoul.py:2279-2297) treats context-length 400s as non-retryable, so the step raises and the turn dies. SimpleCompaction concatenates the entire history into one request with no shrink-on-overflow fallback (src/pythinker_code/soul/compaction.py:152-238).

**Verifier note.** Claim confirmed. One nuance: pythinker does have two PROACTIVE pre-step shrink tiers (prune + auto-compact run before every step), but nothing REACTIVE — a context-length 400 from the provider still kills the turn, which is exactly what the claim says.

**Adopt.** In the step error path, detect the context_overflow classification and respond by forcing prune_context then compact_context and retrying the step once instead of raising. Inside SimpleCompaction, on a context-length rejection drop the oldest messages from to_compact (preserving tool_call_id pairs) and retry, falling back to summarizing only the newest fitting slice. Bound both loops to avoid infinite retry.

**Files.** `src/pythinker_code/soul/pythinkersoul.py`, `src/pythinker_code/soul/compaction.py`

### `core-loop/model-switch-context-continuity-compact-with-the-previous-mo` — missing, M, high

**Today.** Missing. /model creates a brand-new session and reloads ('Starting fresh session for the new model...', ui/shell/slash.py:360-368) — all conversation context is discarded on every model change. There is no path that carries a compacted summary across the switch (rg comp_hash/model_switch shows only the telemetry event).

**Verifier note.** Claim confirmed. /model with an actual model change unconditionally creates a brand-new Session (copying only additional_dirs) and reloads; no summary, compaction, or history is carried across. Minor precision notes: a thinking-effort-only change keeps the same session (slash.py:368), and session_fork (/fork,/undo) / session_recap exist but are unrelated to model switching. The only model_switch artifact is the telemetry event.

**Adopt.** On model switch, before Reload, optionally run compact_context with the OUTGOING provider (compaction output is plain text, so it sidesteps provider-specific message-format incompatibilities like thinking blocks), then seed the new session's context with the summary sized to the new model's window. Offer continue-vs-fresh as a prompt or config flag; fall back to fresh on compaction failure.

**Files.** `src/pythinker_code/ui/shell/slash.py`, `src/pythinker_code/soul/pythinkersoul.py`, `src/pythinker_code/soul/compaction.py`

### `core-loop/reactive-recovery-from-hard-context-overflow-provider-errors` — missing, M, high

**Today.** Missing. classify_api_error labels 4xx context overflow as 'context_overflow' for telemetry only (pythinkersoul.py:186-196); _is_retryable_error excludes 4xx so the step raises and the turn ends with an error card. The shell only special-cases an LM Studio n_ctx hint (ui/shell/__init__.py:258-300, 1551-1564). Proactive thresholds can miss when the heuristic undercounts (e.g. large pending tool output) and there is no recovery path.

**Verifier note.** Claim confirmed. There is no reactive compact-and-retry path anywhere: classification is telemetry-only, retry predicates exclude 4xx, and the only post-error special-casing is user-facing LM Studio messaging in the shell. Recovery wrappers handle 401 OAuth refresh and connection/timeout only. Compaction/pruning are exclusively proactive (threshold-driven before each step).

**Adopt.** In _step's error path (or a wrapper in _agent_loop), detect the context_overflow classification, set the context token count to max_context_size so should_auto_compact is guaranteed to fire, run prune_context then compact_context, and retry the step exactly once (a one-shot flag prevents loops). Track a telemetry event for overflow-recovered vs overflow-failed.

**Files.** `src/pythinker_code/soul/pythinkersoul.py`, `src/pythinker_code/ui/shell/__init__.py`

### `exec-safety/known-safe-read-only-command-auto-approval-prompt-elision` — missing, M, high

**Today.** Absent. Every Shell call prompts unless yolo/auto mode or a prior signature-keyed session approval covers it (src/pythinker_code/tools/shell/__init__.py:154, src/pythinker_code/soul/approval.py request flow); the first `ls` or `git status` of a session always interrupts the user. permission.py has the inverse (block-listing) classifiers but no positive safe-list, and no host-path pinning (rsplit('/') basename normalization would let /tmp/fake/git match a future safelist).

**Verifier note.** Claim confirmed. No positive safe-list exists anywhere; every foreground/background Shell call unconditionally calls Approval.request, which only auto-resolves via yolo/auto mode or a prior session-approval key. PreToolUse hooks cannot elide prompts either — runner.py honors only permissionDecision=deny/exit-2 block; 'allow' just means not-blocked and the approval prompt still fires later inside the tool. The claim's host-path note is also accurate: base-command normalization is bare rsplit('/') basename, no path pinning.

**Adopt.** Add `is_known_safe_command(command) -> bool` to soul/permission.py reusing the existing tokenizer, _unwrap_command, _shell_hidden_command_reason rejection, and _has_unsafe_git_global_option: a positive safelist of read-only binaries with unsafe-flag exclusions, requiring every ;/&&/||/| segment to be safe. Consult it in Shell.__call__ before Approval.request (only when the execution policy is 'ask', never to override 'deny' profiles), and restrict basename matching for absolute first tokens to known system bin dirs so a workspace-local fake binary cannot ride the safelist. Track elisions in telemetry like auto_session approvals.

**Files.** `src/pythinker_code/soul/permission.py`, `src/pythinker_code/tools/shell/__init__.py`, `src/pythinker_code/soul/approval.py`, `<ref>/shell-command/src/command_safety/is_safe_command.rs`

### `mcp/per-server-startup-timeout-plus-actionable-startup-failure-d` — partial, M, high

**Today.** Partial. Only a global mcp.client.tool_call_timeout_ms (60s) exists (config.py MCPClientConfig); there is no startup timeout, so a hung connect leaves a server in 'connecting' forever. The OAuth pre-check logs an actionable 'run pythinker mcp auth X' hint and sets 'unauthorized' status, but generic failures log raw exceptions and MCPServerSnapshot (wire/types.py) carries no error string, so /mcp shows 'failed' with no reason.

**Verifier note.** Verdict stands, details confirmed: MCPClientConfig has only tool_call_timeout_ms (60000); the only other MCP timeout is _MCP_CLOSE_TIMEOUT_S for teardown, not connect. MCPServerSnapshot (wire/types.py) carries name/status/tools and no error string; /mcp rendering (ui/shell/mcp_status.py) shows an actionable hint only for 'unauthorized' (run: pythinker mcp auth X) and bare 'failed' otherwise. Two additions the claim missed: (a) a hung connect doesn't just leave 'connecting' forever — it blocks every agent turn, since _agent_loop awaits the loading task with no timeout; (b) an out-of-session diagnostic exists: `pythinker mcp test <name>` (cli/mcp.py) prints the actual connect exception, and the system prompt points users to /mcp and mcp auth (agents/default/system.md:130).

**Adopt.** Wrap _connect_server in asyncio.wait_for with a per-server startup_timeout_s (config key, default ~30s); add an `error` field to MCPServerSnapshot and render it in /mcp; classify the common failure shapes (timeout -> suggest raising startup timeout; OAuth/401 -> suggest the auth command; command-not-found -> show resolved command) into one short actionable string.

**Files.** `src/pythinker_code/soul/toolset.py`, `src/pythinker_code/config.py`, `src/pythinker_code/ui/shell/mcp_status.py`, `<ref>/mcp/src/connection_manager.rs`

### `mcp/per-server-tool-allow-deny-filtering-enforced-at-list-time-a` — partial, M, high

**Today.** Missing. _register_mcp_tools (soul/toolset.py) registers every tool the server lists; mcp.json schema and `pythinker mcp add` (cli/mcp.py) have no enabled/disabled tool fields; no grep hits for enabled_tools/disabled_tools/include_tools outside agentspec.py (which filters built-in tools for subagents, not MCP).

**Verifier note.** Verdict 'missing' is too strong. Confirmed missing for the main session and config layer: mcp.json has no enabled/disabled tool fields and _register_mcp_tools registers every listed tool unconditionally. BUT the claim's parenthetical that agentspec filtering covers 'built-in tools for subagents, not MCP' is factually wrong: agent specs' allowed_tools/exclude_tools accept named MCP entries keyed mcp__<server>__<tool>; load_tools deliberately skips them as 'named dynamic tools' and toolset.add_shared_tools attaches them from runtime.mcp_tools (populated at connect: runtime.mcp_tools[f"mcp__{server_name}__{tool.name}"] = tool). So per-tool MCP allowlisting enforced at list time AND call time (tool simply absent from the toolset) exists for subagents/custom agent specs — just not via mcp.json and not for the default main agent, whose load_mcp_tools path bypasses the spec allowlist.

**Adopt.** Add optional `enabledTools`/`disabledTools` arrays per server in mcp.json; filter in _connect_server before _register_mcp_tools, and re-check membership inside MCPTool.__call__ as the call-time gate. Keeps noisy servers (e.g. browser MCP with 30 tools) from flooding the model tool list and doubles as a safety scoping knob.

**Files.** `src/pythinker_code/soul/toolset.py`, `src/pythinker_code/cli/mcp.py`, `<ref>/mcp/src/tools.rs`

### `multi-agent/enforced-workspace-isolation-for-parallel-write-capable-chil` — partial, M, high

**Today.** Partial — intent only. Agent/RunAgents accept isolation="worktree" but the docstring says it merely 'records a git-worktree isolation intent' (src/pythinker_code/tools/agent/__init__.py:88-94); BackgroundTaskManager stores it as task-spec metadata (src/pythinker_code/background/manager.py:334,367) and src/pythinker_code/background/agent_runner.py never reads it, so parallel coder/implementer children share one working tree and can clobber each other.

**Verifier note.** Claim upheld exactly: intent-only metadata. No code in the repo executes `git worktree` for agents; the word 'worktree' appears in src only in the isolation field docs and prompt prose (system.md/best_practices.md warnings about dirty worktrees).

**Adopt.** Honor isolation="worktree" for background write-profile children: create a git worktree per agent under the session dir before launch, point the child runtime's work_dir at it, and on completion report the worktree path plus a diff summary in the final report so the orchestrator (or user) merges deliberately. Clean up or retain worktrees per existing recovery rules; reject isolation for non-git roots with an actionable error.

**Files.** `src/pythinker_code/tools/agent/__init__.py`, `src/pythinker_code/background/agent_runner.py`, `src/pythinker_code/background/manager.py`

### `multi-agent/spawn-time-context-fork-child-inherits-filtered-parent-histo` — missing, M, high

**Today.** Missing. prepare_soul in src/pythinker_code/subagents/core.py restores only the child's own persisted context (resume case); new children start blank and rely on the orchestrator hand-writing RunAgents base_prompt / Agent prompt context packets (src/pythinker_code/tools/agent/__init__.py). Session-level fork/recap exists for sessions but there is no parent-to-child history fork at Agent spawn.

**Verifier note.** Claim upheld. No parent-to-child history fork exists at Agent spawn; checked for fork/inherit/handoff mechanisms — TaskHandoff (tools/background/__init__.py:423) is an info dump for the user, not a context transfer, and session_fork.py operates on the root session wire (enumerate_turns/truncate_wire_at_turn/fork_session) only.

**Adopt.** Add fork_context: bool to Agent Params (new instances only, reject with model override). In prepare_soul, when forking, seed the child Context from the parent soul's history filtered to user messages and assistant final texts (skip tool call/result records and thinking blocks), then append the task prompt. Persist the seeded history through the existing context file so resume keeps working.

**Files.** `src/pythinker_code/subagents/core.py`, `src/pythinker_code/tools/agent/__init__.py`, `src/pythinker_code/subagents/store.py`

### `observability-feedback/consolidated-per-turn-rollup-analytics-event-turn-fact-reduc` — partial, M, high

**Today.** Partial. The pythinker.turn span records stop_reason/step_count/model/plan_mode (src/pythinker_code/soul/pythinkersoul.py:1171-1202) and record_turn emits counters (src/pythinker_code/telemetry/metrics.py:178); token usage lands on per-call llm spans (pythinkersoul.py:1665-1700) and tool_call/tool_error track events fire per call without turn_id (src/pythinker_code/soul/toolset.py:693-706). No single per-turn record ties tool-type counts, usage, error kind, and resolved config together, so fleet dashboards must join disparate events.

**Verifier note.** Claim stands as 'partial'; details verified accurate. Full inventory of track() event names (incl. multiline calls) confirms there is no consolidated per-turn analytics event tying tool counts, usage, error kind, and config together. Minor refinements: a turn_id DOES exist internally (soul._current_turn_id, passed to the toolset per step), and the tool_call_dedup_detected track events DO carry turn_id+step_no — but the general tool_call/tool_error track events do not, exactly as claimed.

**Adopt.** Accumulate a TurnSummary in the soul during _agent_loop (tool counts bucketed by category, cumulative usage delta, steer count, api error kind/status, approval mode, plan/goal mode flags, is_first_turn) and emit one `turn_completed` track event plus span attributes at turn end. Reuse existing classify_api_error and tool categories; add turn_id to the existing tool_call track events for joinability.

**Files.** `<ref>/analytics/src/reducer.rs`, `<ref>/analytics/src/facts.rs`, `src/pythinker_code/soul/pythinkersoul.py`, `src/pythinker_code/soul/toolset.py`, `src/pythinker_code/telemetry/metrics.py`

### `observability-feedback/feedback-log-ring-buffer-structured-session-tags-and-connect` — partial, M, high

**Today.** Partial. /feedback builds a redacted structured payload (git snapshot, 10 recent-error metadata entries from telemetry/errors.py ring, recent messages, tool-call summaries, subagents) with an explicit consent summary (src/pythinker_code/feedback.py, telemetry/errors.py:111-140), and /report-error submits the error ring (src/pythinker_code/ui/shell/slash.py:725). But no in-memory full-log buffer or log-tail attachment exists (logs go to a rotating disk file, src/pythinker_code/app.py:102, never attached), no proxy/connectivity diagnostics, and no session-long structured tag accumulation (e.g. last endpoint/auth state).

**Verifier note.** Claim stands as 'partial' with one factual correction: the statement 'logs go to a rotating disk file, never attached' is wrong for the export path — `pythinker export` collects recent pythinker.*.log files (session-window + export-window, 100MB cap) and bundles them under logs/ in the export ZIP, and the CLI crash path advertises this ('run pythinker export to share diagnostics'). It is true that /feedback and /report-error do not attach logs, the only in-memory ring is the 10-entry error-metadata ring, there are no proxy/connectivity diagnostics, and no session-long tag accumulation (feedback payload carries only point-in-time client/session/model metadata; Sentry extra_tags are per-event).

**Adopt.** Add a bounded in-memory loguru sink (byte-capped ring, DEBUG/TRACE level independent of console filter) and attach its redacted snapshot to /feedback and /report-error when the user opts in to logs; add a connectivity-diagnostics section that reports which proxy env vars are set (names + redacted values); accumulate a small capped dict of session diagnostic tags (provider endpoint key, auth mode, last api status) that report_handled_error and the llm layer update, merged into the feedback payload with reserved keys protected.

**Files.** `<ref>/feedback/src/lib.rs`, `<ref>/feedback/src/feedback_diagnostics.rs`, `src/pythinker_code/feedback.py`, `src/pythinker_code/telemetry/errors.py`, `src/pythinker_code/app.py`, `src/pythinker_code/ui/shell/slash.py`

### `patch-file-tools/graduated-fuzzy-matching-ladder-for-edit-location-recovery` — partial, M, high

**Today.** src/pythinker_code/tools/file/replace.py matches edit.old with exact str.count plus a single CRLF-translation fallback (_crlf_translated_edit); any whitespace drift or smart-quote mismatch hard-fails with 'old string not found', forcing a re-read + retry turn. No trailing-newline normalization in replace.py or tools/file/write.py.

**Verifier note.** Claim confirmed as stated. The only recovery beyond exact matching is the CRLF translation fallback; no whitespace/smart-quote/indentation fuzzy ladder and no trailing-newline normalization exist anywhere in the edit path.

**Adopt.** When exact match count is 0 after the CRLF fallback, split file and needle into lines and run a line-wise seek with the same ladder (exact → rstrip-equal → strip-equal → Unicode-punctuation-normalized); on a unique hit, replace the actual matched file slice (never the needle text) and report which relaxation fired in the tool message. Keep ambiguity semantics: >1 fuzzy hit without replace_all still errors. Add an opt-in final-newline normalization for whole-file writes.

**Files.** `<ref>/apply-patch/src/seek_sequence.rs`, `<ref>/apply-patch/src/lib.rs`, `src/pythinker_code/tools/file/replace.py`, `src/pythinker_code/tools/file/write.py`

### `prompts-instructions/dynamic-permissions-state-instructions-rendered-from-live-en` — missing, M, high

**Today.** Enforcement is rich (PermissionProfile, fail-closed shell mutation/workspace-escape classifiers in src/pythinker_code/soul/permission.py; ApprovalState with session-approved actions in soul/approval.py) but prompt-side the model only sees static 'not sandboxed, be cautious' text (system.md §10) plus auto-mode guidance (auto_mode.py). The injection-provider registry (pythinkersoul.py lines 441-447: plan/auto/goal/inline/model-defense/orchestration) has no permissions provider — the model is never told whether yolo/safe-mode is active, what auto-approves, which actions are session-approved, or how the shlex-based gate segments commands; it discovers policy via failed tool calls.

**Verifier note.** Verdict correct; one overstatement in the supporting text: the model is not entirely uninformed about what auto-approves — static descriptions of yolo/auto approval semantics exist in tools/plan/enter_description.md ('Auto-approve mode notes: Yolo mode only bypasses permission approval... In auto mode, EnterPlanMode/ExitPlanMode are approved automatically') and in the auto-mode injections ('Tool calls are auto-approved only when the current trust/safe-mode policy allows', 'Outside-workspace file writes are not auto-approved'). But none of this is rendered from live enforcement config: the injection-provider registry (soul/pythinkersoul.py, _injection_providers list at ~lines 441-460: PlanMode, GoalMode, ModelDefense, InlineCommandReminder, Orchestration, AutoMode) has no permissions provider; /yolo toggling only wire_sends UI text with no context injection (soul/slash.py lines 114-140); permission_profile_for_runtime is used solely to set the step enforcement profile (pythinkersoul.py lines 1652-1666), never serialized into the prompt; session-approved actions (approval.py) and the shlex segmentation rules are never surfaced. system.md §10 line 226 is the only always-on text ('not sandboxed... be extremely cautious').

**Adopt.** Add a PermissionsInjectionProvider that renders a short block from active_permission_profile + ApprovalState: current trust posture (safe-mode/yolo/auto), what is auto-approved vs always-prompted (git mutations, destructive ops), session-approved action names, and the gate's command-segmentation caveats (subshells/glued operators are not classified — write plain commands). Re-emit on /yolo //auto toggles like AUTO_DISABLED_REMINDER. Saves wasted gated calls and teaches the model to shape commands the classifier can see.

**Files.** `src/pythinker_code/soul/permission.py`, `src/pythinker_code/soul/approval.py`, `src/pythinker_code/soul/pythinkersoul.py`, `src/pythinker_code/soul/dynamic_injections/auto_mode.py`

### `prompts-instructions/model-initiated-escalation-with-justification-and-suggested-` — missing, M, high

**Today.** Approval prompts are entirely harness-initiated: the Shell tool has no justification parameter (src/pythinker_code/tools/shell/__init__.py, bash.md), ApprovalResult.feedback flows only on rejection (soul/approval.py; confirmed by memory note), and any 'always allow' scoping is chosen by the user/UI — the model can neither pre-justify a gated command nor propose a scoped reusable rule.

**Verifier note.** Claim survives as stated. Note the Shell tool does have a `description` parameter, but it is exclusively a background-task label (defaulted via _default_background_description when run_in_background), not an approval justification, and it is not surfaced in the approval prompt.

**Adopt.** Add an optional justification field to Shell (and other gated tools) surfaced in the approval prompt UI, plus an optional model-suggested allow-prefix that the approval runtime validates against a banned-prefix list (bare interpreters, rm/git-reset class, heredoc-containing commands) before offering a persist-this-rule option. Prompt-side: one paragraph in the Shell description telling the model when to supply each. Reduces blind approval prompts and makes persisted rules better-scoped.

**Files.** `src/pythinker_code/tools/shell/__init__.py`, `src/pythinker_code/tools/shell/bash.md`, `src/pythinker_code/soul/approval.py`, `src/pythinker_code/approval_runtime/runtime.py`

### `protocol-headless/stable-machine-readable-jsonl-event-stream-with-thread-turn-` — partial, M, high

**Today.** Partial. --print --output-format stream-json (src/pythinker_code/ui/print/visualize.py JsonPrinter) emits merged assistant Message JSON, tool-result messages, PlanDisplay, ProgressNote/Suggestion and Notifications — but no session-id event at stream start, no turn started/completed/failed lifecycle events, no terminal status, no token usage (StatusUpdate with TokenUsage exists in src/pythinker_code/wire/types.py but falls into JsonPrinter's ignore branch), and no item ids linking tool start/result. A resume hint with the session id goes to stderr only at exit (src/pythinker_code/cli/__init__.py _print_resume_hint).

**Verifier note.** Claimed verdict 'partial' is correct, but one evidence point is wrong: tool start/result lines ARE linked by ids. The merged assistant message includes tool_calls each carrying ToolCall.id (packages/pythinker-core/src/pythinker_core/message.py:174-182), and each tool-result line is a Message with tool_call_id set from the originating call (src/pythinker_code/soul/message.py:58-63, tool_result_to_message). The rest holds: TurnBegin/TurnEnd ARE wire events and ARE sent (src/pythinker_code/wire/types.py:37-60; src/pythinker_code/soul/flow_runner.py:210,214) but JsonPrinter drops them in its `case _` ignore branch, so the emitted JSONL has no turn lifecycle; StatusUpdate (wire/types.py:217-229, token_usage) is likewise dropped by JsonPrinter while the ACP frontend does consume it (src/pythinker_code/acp/session.py:186); no session-id event is emitted in-stream — the resume hint goes to the original stderr fd at exit via _emit_fatal_error (src/pythinker_code/cli/__init__.py:683-691, 1089-1092, 1117, 1172); no terminal status event exists (exit code only).

**Adopt.** Add a v2 stream format that serializes WireMessageEnvelope events (TurnBegin/StepBegin/ToolCall/ToolResult/StatusUpdate/PlanDisplay/TurnEnd) as JSONL, prefixed by a session-started event carrying session_id/model/work_dir and terminated by a turn-completed event carrying final token usage and a terminal status (completed|failed|interrupted). Reuse the existing envelope from wire/types.py so the headless stream, wire server, and session wire-file share one schema; keep the current stream-json as legacy.

**Files.** `src/pythinker_code/ui/print/visualize.py`, `src/pythinker_code/wire/types.py`, `<ref>/exec/src/exec_events.rs`, `<ref>/exec/src/event_processor_with_jsonl_output.rs`

### `protocol-headless/structured-final-output-constrained-by-a-caller-supplied-jso` — missing, M, high

**Today.** Missing. rg over src/pythinker_code finds no output-schema/response_format/structured-output plumbing in the CLI (src/pythinker_code/cli/__init__.py), print UI, or soul run path; the only json_schema hits are FastAPI internals in web/app.py and vis/app.py.

**Verifier note.** Claim confirmed; could not refute. rg for output_schema/response_format/json_schema/structured-output across src/pythinker_code hits only FastAPI's separate_input_output_schemas (web/app.py:171, vis/app.py:52) and prose in skills/agent-creator/SKILL.md (a 'structured output contract' meaning headed markdown sections, not JSON Schema). The full CLI option list in src/pythinker_code/cli/__init__.py has no schema flag, and packages/pythinker-core has no response_format plumbing.

**Adopt.** Add --output-schema FILE (print mode only): validate the file as JSON at startup (fail fast), then thread the schema into the final-turn prompt as a strict output contract (system-reminder instructing JSON-only final message conforming to schema) and validate the final assistant text against it with jsonschema, retrying once with the validation error before exiting nonzero. Provider-native structured-output can be layered later for models that support it.

**Files.** `src/pythinker_code/cli/__init__.py`, `src/pythinker_code/ui/print/__init__.py`, `<ref>/exec/src/lib.rs`

### `review-mode/interactive-review-command-with-target-presets-and-git-picke` — missing, M, high

**Today.** No /review slash command exists in either command registry (src/pythinker_code/ui/shell/slash.py, src/pythinker_code/soul/slash.py — audited every @registry.command), and src/pythinker_code/ui/shell/selectors/ has no branch or commit picker. Review is reachable only via natural language, /skill:review-pr (src/pythinker_code/skills/review-pr/SKILL.md), or the separate `pythinker review` CLI (src/pythinker_code/cli/review.py, cli/_lazy_group.py).

**Verifier note.** Claim CONFIRMED. No /review command is registered in either registry and no git branch/commit picker exists. One nuance the analyst missed: the Suggest tool's spec actively instructs the model to prefill '/review' (src/pythinker_code/tools/suggest/description.md:8, tools/suggest/__init__.py:14-19), yet typing it hits the unknown-command error path — so the shipped suggestion surface references a command that does not exist. Reachability list in the claim is otherwise accurate (natural language, /skill:review-pr, `pythinker review` CLI), plus reviewer subagents (agents/default/review.yaml, code_reviewer.yaml, security_reviewer.yaml) dispatched via the Agent tool.

**Adopt.** Add a /review slash command that shows a 4-item selector (uncommitted / base branch / commit / custom). Branch and commit sub-pickers run `git branch --format` and `git log -100 --format=%h%x09%s` through the existing async git helper pattern (subagents/git_context.py:_run_git) and feed the shell's existing searchable selector component. The chosen target is resolved (see prompt-synthesis finding) and dispatched as a `review` subagent run, with results rendered through the existing report-block renderer.

**Files.** `src/pythinker_code/ui/shell/slash.py`, `src/pythinker_code/soul/slash.py`, `src/pythinker_code/ui/shell/selectors/`, `src/pythinker_code/skills/review-pr/SKILL.md`

### `skills-hooks-memories/posttooluse-hook-feedback-surfaced-to-the-model` — partial, M, high

**Today.** toolset.py fires PostToolUse as fire_and_forget_trigger (soul/toolset.py:746) — results are discarded, so a hook that detects a broken build or lint failure can never tell the model. The additional_context plumbing exists in hooks/runner.py:112-126 but is only consumed for PostCompact/SessionStart-after-compact via build_hook_context_message (soul/pythinkersoul.py:2149, soul/compaction_restore.py:161); _stdout_adds_context (runner.py:121-126) explicitly restricts plain-stdout context to those two events.

**Verifier note.** Claim confirmed (minor line drift only: PostToolUse fire-and-forget is toolset.py:743-754 not 746; consumer is pythinkersoul.py:2182-2188 not 2149). PostToolUse and PostToolUseFailure results are discarded via fire_and_forget_trigger; additional_context is consumed solely in the compaction path — build_hook_context_message (compaction_restore.py:161) over PostCompact + SessionStart(source=compact) results at pythinkersoul.py:2182-2188. _stdout_adds_context (runner.py:121-126) restricts plain-stdout context to exactly those two events. UserPromptSubmit (pythinkersoul.py:997-1010) and PreToolUse (toolset.py:625-633) results are checked for action=='block' only; their additional_context is dropped too. rg confirms additional_context consumers are only runner.py, pythinkersoul.py, compaction_restore.py.

**Adopt.** Await PostToolUse results in toolset.py instead of fire-and-forget (keep a short timeout so slow hooks don't stall turns), and when any result carries additional_context or a block decision, append it to the ToolResult output (or as a follow-up user-role fragment) so the model sees hook feedback. Extend _stdout_adds_context / JSON parsing to honor additionalContext for PostToolUse and UserPromptSubmit, not just compaction events.

**Files.** `src/pythinker_code/soul/toolset.py`, `src/pythinker_code/hooks/runner.py`, `src/pythinker_code/soul/pythinkersoul.py`, `<ref>/hooks/src/events/post_tool_use.rs`

### `skills-hooks-memories/trust-gating-for-repo-scoped-hook-definitions` — missing, M, high

**Today.** Hooks load from merged config including project scope: config.py:944 defines Config.hooks, load_config merges ~/.pythinker/config.toml → .pythinker/config.toml → config.local.toml (config.py:1030-1040), and app.py:388-391 builds HookEngine(config.hooks) unconditionally. SCOPE_LOCKED_PATHS (config.py:54-68) deliberately blocks repo-controlled statusline commands but does NOT lock ('hooks',), so a repo-controlled .pythinker/config.toml can register auto-executing shell hooks. Workspace-trust primitives exist (session_state.py TrustStateData, soul/approval.py) but are not consulted for hooks.

**Verifier note.** Claim confirmed. Hooks merge from project scope with no trust check: Config.hooks at config.py:944, scoped merge user→project→local in load_config/_load_scoped (config.py:1030-1043), HookEngine(config.hooks) built unconditionally at app.py:388-390. SCOPE_LOCKED_PATHS (config.py:54-68) locks providers/services/feedback.api_key and four tui.statusline fields only — no ('hooks',) entry. TrustStateData exists (session_state.py:21, Field at :53) but is never consulted anywhere in hooks/* or the hook construction path (rg 'trust' over hooks/ and config.py: zero hits).

**Adopt.** Either add ('hooks',) to SCOPE_LOCKED_PATHS (smallest fix, loses project hooks entirely) or port the trust model: hash each HookDef (event+matcher+command+timeout), persist trusted hashes in user-level state keyed by source scope, and have HookEngine skip non-user-scope handlers whose hash is untrusted/modified until the user approves via a one-time prompt (reuse the existing workspace-trust flow in soul/approval.py). Surface skipped-untrusted hooks in /hooks display.

**Files.** `src/pythinker_code/config.py`, `src/pythinker_code/hooks/engine.py`, `src/pythinker_code/app.py`, `<ref>/hooks/src/engine/discovery.rs`

### `tools-registry-codemode/deferred-tool-loading-with-on-demand-tool-search` — missing, M, high

**Today.** Every connected MCP tool's full schema is always advertised via PythinkerToolset.tools (src/pythinker_code/soul/toolset.py:402-404), subject only to the policy visibility filter and manual hide/unhide. defer_mcp_tool_loading (toolset.py:825) defers only server connection at startup, not schema exposure; no tool-search facility exists (rg tool_search/defer_loading is empty in src).

**Verifier note.** Claim survives. PythinkerToolset.tools advertises the full schema of every non-hidden, policy-visible tool each step; defer_mcp_tool_loading defers only the server connection until shell start, after which all tool schemas are exposed. No tool-search tool exists in tools/ and no schema-deferral mechanism exists.

**Adopt.** Add a per-server defer flag (or auto-trigger above N tools): deferred tools register but enter _hidden_tools with a search-index entry (name + description + schema property names). A built-in ToolSearch tool matches the index, unhides the top matches, and returns their names/descriptions so the next step can call them. Pairs naturally with the existing hide/unhide machinery.

**Files.** `src/pythinker_code/soul/toolset.py`, `<ref>/tools/src/tool_search.rs`, `<ref>/tools/src/tool_discovery.rs`

### `tools-registry-codemode/foreign-tool-schema-sanitization-and-budgeted-compaction` — partial, M, high

**Today.** mcp_tool.inputSchema is passed verbatim into the tool definition (src/pythinker_code/soul/toolset.py:1119, MCPTool.__init__) and flows raw into every provider request; WireExternalTool likewise (toolset.py:1200). No sanitize/prune/compact layer exists (rg sanitiz/inputSchema across src and pythinker-core). A malformed or 100KB schema from a third-party server can break provider calls or permanently tax context.

**Verifier note.** REFUTED as 'missing' — a narrow sanitization layer already exists. ensure_property_types() fills missing `type` keywords in nested property schemas (explicitly to keep loose MCP-server schemas working) and is applied to every tool schema sent through the Pythinker platform chat provider; deref_json_schema() normalizes internal CallableTool2 schemas. What IS missing: this sanitization is provider-specific (provider adapters send tool.parameters verbatim), and there is no size budgeting, pruning, or compaction anywhere — a 100KB schema still passes through untaxed. Correct verdict: partial.

**Adopt.** Add a normalize_tool_schema() applied at MCPTool/WireExternalTool registration: fill missing type, const→enum, strip keywords providers reject, prune unreferenced $defs, and when serialized size exceeds a budget run lossy passes (drop nested descriptions, then collapse deep structure to permissive objects) with a warning log. Pure function, easy to fixture-test against real server schemas.

**Files.** `src/pythinker_code/soul/toolset.py`, `<ref>/tools/src/json_schema.rs`

## Tier 2 — high value, L effort (6 items)

### `exec-safety/os-sandboxed-command-execution-with-escalation-on-denial-lif` — missing, L, high

**Today.** Missing. Shell commands are spawned directly via pythinker_host.exec with no OS confinement (src/pythinker_code/tools/shell/__init__.py:366); all containment is advisory pre-classification in permission.py plus approval gating, which the comments themselves call 'not a shell sandbox'. No sandbox integration exists anywhere in src/pythinker_code (grep confirmed).

**Verifier note.** Claim confirmed. Commands are spawned directly via pythinker_host.exec with the shell binary and -c/-command — no seatbelt/landlock/bwrap/seccomp/firejail integration anywhere in src/pythinker_code (the only 'sandbox' hits are jinja2.sandbox imports for template rendering and the permission.py comment explicitly disclaiming sandbox status). Containment is purely advisory pre-classification (permission.py profile gates) plus approval gating; no escalation-on-denial retry lifecycle and no network-off default at the OS level.

**Adopt.** Add a sandbox transform layer in the Shell tool: on macOS wrap the argv in /usr/bin/sandbox-exec with a generated profile (deny default; allow read broadly, write only to workspace/additional_dirs/tmp; deny network unless the profile allows it), on Linux wrap in bwrap with equivalent binds when available; degrade gracefully to today's direct exec when unsupported. On a sandbox-denial exit signature, return a structured error inviting one escalation: re-request approval flagged 'will run unsandboxed', then re-run without the wrapper. This converts the heuristic read-only/offline guarantees of restricted profiles into kernel enforcement.

**Files.** `src/pythinker_code/tools/shell/__init__.py`, `src/pythinker_code/soul/permission.py`, `<ref>/sandboxing/src/manager.rs`, `<ref>/sandboxing/src/seatbelt.rs`

### `exec-safety/user-extensible-declarative-exec-policy-with-allow-prompt-fo` — partial, L, high

**Today.** Absent. All command classification is hardcoded Python (_MUTATING_COMMANDS, _GIT_MUTATIONS, _PACKAGE_MANAGER_MUTATIONS etc. in src/pythinker_code/soul/permission.py); config.py exposes no shell-command allow/forbid settings (only web.allowed_domains); execution_profiles.py gives coarse per-tool ask/deny/allow modes, not per-command rules.

**Verifier note.** Overstated as 'Absent'. Pythinker DOES have a user-extensible forbidden tier: config.py:944 exposes hooks: list[HookDef]; a PreToolUse hook (regex matcher on tool name, full tool_input JSON on stdin) can block any Shell call with a reason via exit-2 or permissionDecision=deny, fired before tool execution in soul/toolset.py:611-633. Documented at docs/en/customization/hooks.md. However the rest of the claim is correct: it is imperative (arbitrary shell command), deny-only (no allow/auto-approve tier, no prompt tier), fail-open on error/timeout (hooks/runner.py:31,49,59), with no declarative per-command rules and no self-testing. Built-in classification is hardcoded Python and config.py has no shell allow/forbid lists (only web.allowed_domains at :567); execution_profiles.py gives only coarse per-tool deny/ask/allow.

**Adopt.** Add a small policy schema (TOML/YAML under ~/.pythinker and project .pythinker, repo-scope locked like statusline.command) of token-prefix rules with decision allow|prompt|forbidden and optional justification; validate match/not_match examples at load and refuse the file on failure. Evaluate before the heuristic classifiers in check_shell_command_allowed/Approval.request: forbidden returns a ToolError carrying the justification, prompt forces a fresh approval even under session rules, allow feeds the prompt-elision path from the safelist finding. Strictest decision wins across matches; unmatched commands fall through to today's heuristics unchanged.

**Files.** `src/pythinker_code/soul/permission.py`, `src/pythinker_code/config.py`, `<ref>/execpolicy/src/policy.rs`, `<ref>/execpolicy/README.md`

### `multi-agent/mid-run-steering-of-live-child-agents-send-input-with-option` — missing, L, high

**Today.** Missing for agents. src/pythinker_code/subagents/runner.py busy_resume_message() hard-rejects resume of any running instance; src/pythinker_code/tools/background/input.md states TaskInput is 'only for non-terminal bash background tasks'; src/pythinker_code/background/manager.py write_input() is bash-stdin only. The only steering path is the coarse TaskStop-kill then Agent(resume=...) after terminal state.

**Verifier note.** Claim upheld for child agents. One nuance the analyst missed: a steering primitive DOES exist at root level — PythinkerSoul.steer() (src/pythinker_code/soul/pythinkersoul.py:923) with a pending-steer queue (_consume_pending_steers/_inject_steer, lines 927/946) wired from the user via wire/server.py _handle_steer and the shell UI. It is user->root only; nothing routes steers parent->child.

**Adopt.** Add an AgentInput (or Agent param message_to=) path: for a running_background agent, enqueue the message into a per-agent inbox file in SubagentStore; BackgroundAgentRunner checks the inbox between soul steps (or via a cancellation+requeue wrapper around run_soul) and injects it as the next user message, with an optional interrupt flag that cancels the in-flight step first. Reuse the existing wire/SubagentEvent plumbing for begin/end visibility and keep busy_resume_message pointing at the new tool.

**Files.** `src/pythinker_code/subagents/runner.py`, `src/pythinker_code/background/agent_runner.py`, `src/pythinker_code/background/manager.py`, `src/pythinker_code/tools/background/input.md`

### `review-mode/llm-approval-guardian-auto-review-of-on-request-approvals-wi` — missing, L, high

**Today.** Approval gating is deterministic: profile rules, never-auto-approve lists for boundary-crossing actions, and yolo/auto flags (src/pythinker_code/soul/approval.py — no LLM path). The nearest seed is the blind-first decision advisor used for auto-mode AskUserQuestion deliberation (src/pythinker_code/soul/deliberation.py), which proves the tool-less single-call advisor pattern but never gates tool approvals.

**Verifier note.** Verdict CONFIRMED — no LLM ever reviews a tool-approval request — but the claimed state understates pythinker's deterministic coverage of this capability's fail-closed/backstop half. (1) Approval.deliberation_gate (soul/approval.py:384-447) is a destructive-action auto-approval guardian: under auto/yolo it bounces destructive calls once per (execution-context, generation) fingerprint with explicit fail-closed fallback when no deliberation scope is bound ('Fail CLOSED: keep bouncing'), gating AHEAD of the yolo bypass — the 'deliberation' is done by the main agent on re-issue, not a separate LLM. (2) PreToolUse hooks can deny tool calls with structured permissionDecision=deny and fail-closed sink handling (hooks/runner.py:83-86, hooks/engine.py:318-324) — user-scripted, not LLM. (3) The claim's deliberation.py characterization is accurate: blind_advisor_verdict is a tool-less single-call advisor scoped to auto-mode AskUserQuestion only (consumed by tools/ask_user), and it advises rather than gates. The only circuit breaker found is the degenerate-loop backstop (pythinkersoul.py:1898-1918, max_consecutive_failures) which counts all-error steps, not approval outcomes. So: LLM auto-review of approvals + approval-scoped circuit breakers = genuinely absent; fail-closed destructive backstop = present deterministically.

**Adopt.** Extend the deliberation.py pattern into an opt-in approval advisor for auto/yolo modes: when a gated action would otherwise auto-approve (or would interrupt an unattended run), make one tool-less LLM call with a bounded transcript (reuse existing pruning utilities for caps) and a strict-JSON verdict; fail closed to the normal human prompt on timeout/malformed output. Add per-turn consecutive-denial and windowed-denial circuit breakers so a misfiring advisor degrades to human prompting instead of looping, and tag manual overrides of advisor denials in history so the model knows the user explicitly authorized that exact action.

**Files.** `src/pythinker_code/soul/approval.py`, `src/pythinker_code/soul/deliberation.py`, `src/pythinker_code/soul/permission.py`

### `skills-hooks-memories/llm-driven-two-phase-durable-memory-pipeline-extraction-cons` — missing, L, high

**Today.** Pythinker's durable-memory profile is heuristic and synchronous: memory/harvest.py extracts only regex-prefixed lines (decision:/blocker:/next:/evidence:) from compaction-dropped assistant messages; memory/recap.py writes fixed-schema JOURNAL.md recaps; memory/consolidation.py only stages existing scratch/journal blocks into an approval-gated inbox (no LLM, no synthesis, no forgetting); config.py:470-519 gates these flags. Nothing reads past rollouts with a model, classifies outcomes, or consolidates/prunes MEMORY.md content.

**Verifier note.** Claim confirmed for the stated capability; one nuance to record. Extraction is regex-only (memory/harvest.py _NOTE_RE matching decision:/blocker:/next:/evidence: in dropped assistant messages); recap.py writes fixed-schema journal recaps; consolidation.py generate_inbox_candidates (:56-99) only stages existing scratch/journal blocks into an approval-gated inbox — no model call anywhere in memory/ (rg confirms). Flags at config.py:470-519. Nuance: in-session model-driven curation DOES exist — the root-only Memory tool (tools/memory/__init__.py) supports add/replace/remove against MEMORY.md/USER.md, MEMORY_CHAR_LIMIT=2200 (project_memory.py:38) forces curation when full, and the journal is count-pruned to 100 entries (:315). But nothing reads past rollouts with a model, classifies outcomes, or runs background consolidation/forgetting, so the two-phase-pipeline verdict 'missing' stands.

**Adopt.** Adopt incrementally: (1) port the Phase-1 prompt design (no-op gate, outcome triage, preference-signal/evidence rules, redaction) as an opt-in background extraction over recent idle sessions using the existing subagent runner, writing candidates into the existing approval-gated inbox so the consent model is preserved; (2) later add a consolidation pass (offline subagent profile, workspace-write-only) that merges approved candidates into MEMORY.md with diff-reviewable output, using session-store leases (multi-instance invariants already exist) and a budget guard that skips the pipeline when recent provider usage is high. Keep the inbox approval gate as the trust boundary instead of fully autonomous writes.

**Files.** `src/pythinker_code/memory/harvest.py`, `src/pythinker_code/memory/consolidation.py`, `src/pythinker_code/memory/recap.py`, `<ref>/memories/README.md`, `<ref>/memories/write/templates/memories/stage_one_system.md`, `<ref>/memories/write/templates/memories/consolidation.md`

### `tools-registry-codemode/unified-interactive-pty-exec-sessions-persistent-process-ids` — partial, L, high

**Today.** The background subsystem (src/pythinker_code/background/worker.py, manager.py; src/pythinker_code/tools/background/__init__.py) provides TaskInput stdin writes, TaskOutput with byte-offset paging + blocking waits + anti-poll escalation hints, TaskStop/TaskList/TaskHandoff. But processes run on plain pipes (worker.py:181 asyncio.subprocess.PIPE), not a PTY; foreground Shell closes stdin immediately (src/pythinker_code/tools/shell/__init__.py:370); there is no interrupt-without-kill and no persistent interactive shell session — one command per task.

**Verifier note.** Claim survives as stated. Background tasks have persistent ids, TaskInput stdin writes, TaskOutput paging, TaskStop — but on plain pipes, stop is SIGTERM with SIGKILL escalation (no interrupt-without-kill), foreground Shell closes stdin immediately, and each task is one command (no persistent shell session). No PTY code exists anywhere in src/ or packages/ (rg openpty|ptyprocess|TIOCSWINSZ|conpty: zero hits).

**Adopt.** Add a PTY-backed task kind in background/worker.py (pty.openpty on POSIX, pywinpty on Windows) selected via a tty flag at creation; extend TaskInput with an interrupt/control-character path and clamp poll windows like the reference; cap concurrent interactive sessions with last-used pruning. This unlocks REPLs, debuggers, password prompts and watch UIs for the agent.

**Files.** `src/pythinker_code/background/worker.py`, `src/pythinker_code/tools/background/__init__.py`, `src/pythinker_code/tools/shell/__init__.py`, `<ref>/core/src/unified_exec/process_manager.rs`

## Tier 3 — medium value (62 items)

### `config-features/per-key-runtime-config-override-layer` — partial, S, medium

**Today.** Missing. --config replaces the entire config with inline text validated standalone (src/pythinker_code/cli/__init__.py:791 load_config_from_string, defaults for everything else), and --config-file bypasses the scope pipeline entirely (load_config explicit-path branch in config.py). There is no way to tweak one key for one run while keeping the resolved user/project config.

**Verifier note.** REFUTED as 'missing' — downgrade to partial. The claim's core assertion ('no way to tweak one key for one run while keeping the resolved user/project config') is factually wrong: the PYTHINKER_* env overlay applies per-key overrides ON TOP of the fully resolved user/project/local merge for 17 mapped keys (including the nested tui.statusline.enabled), and dedicated CLI flags mutate individual loop_control keys after load_config. What IS missing: a generic mechanism for arbitrary dotted keys (no --set key=value); env coverage is limited to the ENV_FIELD_MAP allowlist. The claim's characterization of --config/--config-file is accurate but incomplete.

**Adopt.** Add a repeatable `--set key=value` option parsed into a nested dict (dotted paths, TOML-ish value coercion) and merged in _load_scoped as the final, highest-precedence scope with provenance 'cli --set'; reuse the unknown-key warning machinery from the strict-config finding.

**Files.** `src/pythinker_code/cli/__init__.py`, `src/pythinker_code/config.py`, `<ref>/config/src/overrides.rs`

### `config-features/post-load-per-field-origin-map-exposed-to-the-user` — partial, S, medium

**Today.** Partial. _type_based_merge in src/pythinker_code/config.py builds exactly this provenance map, but it is a local variable used only to enrich ValidationError messages and is discarded after validation; only source_scopes (scope -> file path) survives on Config, and the settings panel (ui/shell/selectors/settings.py:274) shows just source_file.

**Verifier note.** Claim confirmed. The per-field provenance map is built during merge but is a local variable discarded after validation; only the coarse scope->file map survives on Config, and no UI surface shows per-field origins.

**Adopt.** Retain the provenance dict on Config as an excluded field; show 'set by project scope (.pythinker/config.toml)' next to non-default values in the /settings panel and in a `config origin <key>` query. Nearly free since the map is already computed.

**Files.** `src/pythinker_code/config.py`, `src/pythinker_code/ui/shell/selectors/settings.py`, `<ref>/config/src/fingerprint.rs`

### `config-features/sanitize-and-warn-handling-for-denied-project-scope-config-k` — partial, S, medium

**Today.** Partial. SCOPE_LOCKED_PATHS in src/pythinker_code/config.py covers the right keys (providers, services, statusline command) but _check_scope_locks hard-raises ConfigError, and _read_toml raises on invalid project TOML — a repo-controlled file can therefore prevent pythinker from launching in that directory (a friction/DoS vector the reference avoids).

**Verifier note.** Claim confirmed. Scope locks exist and cover the claimed keys, but enforcement is hard-fail (ConfigError raise), not sanitize-and-warn; invalid project TOML also hard-fails, so a repo-controlled .pythinker/config.toml can block startup in that directory.

**Adopt.** In the GUARD step, strip locked paths from project/local dicts and collect startup warnings ('ignored providers in .pythinker/config.toml; move to ~/.pythinker/config.toml') instead of raising; degrade unparsable project/local TOML to an empty scope with a warning. Keep hard failure for the user scope only. Surface accumulated warnings once in the shell banner.

**Files.** `src/pythinker_code/config.py`, `<ref>/config/src/loader/mod.rs`

### `context-mgmt/calibrated-token-estimation-for-non-text-payloads-tool-call-` — partial, S, medium

**Today.** Partial. The pending-estimate mechanism exists (Context._pending_token_estimate, token_count_with_pending in src/pythinker_code/soul/context.py:29,92), but estimate_text_tokens counts only TextPart chars/4 (src/pythinker_code/soul/compaction.py:44-53): tool_call arguments and image/media parts contribute zero, so large un-sampled reads or media can silently undercount until the next API usage report, delaying compaction past the trigger.

**Verifier note.** Claim confirmed as partial. Add: ThinkPart text is also uncounted, not just tool-call args and media.

**Adopt.** Extend estimate_text_tokens to include len(tool_call.function.arguments)//4 per tool call and a flat per-media-part constant (e.g. ~1800 tokens per image, matching common provider downscaling), keeping the chars/4 heuristic elsewhere. One function, existing tests in tests/ cover the call sites.

**Files.** `src/pythinker_code/soul/compaction.py`, `src/pythinker_code/soul/context.py`

### `context-mgmt/model-visible-context-budget-signals-threshold-crossing-budg` — missing, S, medium

**Today.** Missing. Context usage is surfaced only in the TUI footer (src/pythinker_code/ui/shell/components/footer.py:154-212) and StatusSnapshot (soul/pythinkersoul.py:864-879); no dynamic-injection provider in src/pythinker_code/soul/dynamic_injections/ exposes remaining tokens to the model, and no tool under src/pythinker_code/tools/ reports it.

**Verifier note.** Claim confirmed. Do not confuse ContextBudget in dynamic_injection.py — that is the budget for sizing injections, not a signal exposed to the model.

**Adopt.** Add a TokenBudgetInjectionProvider to soul/dynamic_injections/ that compares token_count_with_pending against max_context_size each step and, on first crossing of each threshold (state reset in on_context_compacted), emits a one-line system-reminder with tokens remaining. Optionally add a tiny read-only tool returning the same figure. Fits the existing InjectionCandidate budget framework directly.

**Files.** `src/pythinker_code/soul/dynamic_injection.py`, `src/pythinker_code/soul/dynamic_injections/`, `src/pythinker_code/ui/shell/components/footer.py`

### `context-mgmt/verbatim-user-intent-retention-across-compaction-within-a-to` — partial, S, medium

**Today.** Partial. SimpleCompaction preserves only the last 2 user/assistant messages by count, summary-first (src/pythinker_code/soul/compaction.py:146-238); older user messages survive only as summarized bullets in the Goal/Constraints sections of prompts/compact.md. Post-compaction restore re-injects file names, skills, and task snapshots (src/pythinker_code/soul/compaction_restore.py) but not raw user text.

**Verifier note.** Claim confirmed as partial. Two partial mitigations worth adding: (1) the cheap prune tier preserves EVERY non-tool message verbatim (it only elides stale tool-result bodies), deferring the lossy summary; (2) when /goal is active, GoalModeInjectionProvider re-injects the goal objective text verbatim after compaction. Neither is a token-budgeted verbatim retention of raw user messages through full compaction.

**Adopt.** In SimpleCompaction.prepare/compact, collect user-role messages (excluding prior compaction-summary messages) from the to_compact slice and append them verbatim after the summary, newest-first within a configurable token budget (e.g. 8-20k via loop_control), truncating the oldest selected message to fit. Skip messages already inside the preserved tail.

**Files.** `src/pythinker_code/soul/compaction.py`, `src/pythinker_code/prompts/compact.md`

### `core-loop/model-visible-interrupted-turn-history-marker` — partial, S, medium

**Today.** Partial. Cancellation during the tool phase persists the assistant message plus per-call synthetic 'Tool call interrupted by user' results via shielded writes (pythinkersoul.py:1761-1785). But cancellation during the LLM stream leaves no trace: run_soul cancels the soul task (soul/__init__.py:235-241), partial assistant text is dropped, and the next turn's model gets no signal the prior turn was aborted mid-answer.

**Verifier note.** Claim confirmed exactly as stated. Tool-phase cancellation persists markers; LLM-stream-phase cancellation leaves no trace in model-visible history. The shell-side RunCancelled handler only prints 'Interrupted by user' and kills turn-spawned background tasks — it writes nothing to context.

**Adopt.** On RunCancelled where no tool-phase marker was written, append a short system_reminder ('your previous response was interrupted by the user before completion; do not assume it was delivered') to the context via a shielded write before the turn unwinds. Optionally persist the partial streamed assistant text above the marker so work is not silently lost.

**Files.** `src/pythinker_code/soul/pythinkersoul.py`, `src/pythinker_code/soul/__init__.py`

### `core-loop/token-budget-remaining-notices-injected-into-model-context-a` — missing, S, medium

**Today.** Missing. Context usage is surfaced to the USER via StatusUpdate/statusline (pythinkersoul.py:1738-1752, ui/shell/stats_pricing.py) but the model itself is never told how much budget remains; it only experiences sudden prune/compaction. No injection provider covers this (checked soul/dynamic_injections/*).

**Verifier note.** Claim confirmed. No injection provider or system-reminder path tells the model how much context budget remains. The registered providers are PlanMode, GoalMode, ModelDefense, InlineCommandReminder, Orchestration (new, orchestration-shape guidance only — not budget), and AutoMode. Context usage flows only to the USER via StatusUpdate/statusline. The closest-sounding feature, subagents/usage.py 'budget-visible' child token roll-ups, surfaces child SPEND to the parent agent, not remaining context budget.

**Adopt.** Add a small self-filtering DynamicInjectionProvider that tracks the last context_usage ratio, and on crossing 25/50/75% emits a one-shot system_reminder with approximate tokens remaining and a hint to leave durable notes (todo/scratchpad) before compaction. Re-arm thresholds in on_context_compacted.

**Files.** `src/pythinker_code/soul/dynamic_injection.py`, `src/pythinker_code/soul/dynamic_injections/orchestration.py`, `src/pythinker_code/soul/pythinkersoul.py`

### `exec-safety/process-self-hardening-at-startup-core-dumps-off-debugger-at` — partial, S, medium

**Today.** Partial. Child-process env hygiene exists — internal session tokens always dropped, secret-shaped vars scrubbed for restricted profiles, PyInstaller LD_ restoration (src/pythinker_code/utils/subprocess_env.py) — but the agent process itself is unhardened: no RLIMIT_CORE=0, no dumpable/ptrace controls, and LD_/DYLD_ are not stripped from its own environment outside the frozen-Linux case.

**Verifier note.** Claim confirmed. Child-process hygiene exists in utils/subprocess_env.py (PYTHINKER_WEB/VIS session tokens always dropped at :22-27, scrub_secret_env heuristic at :102-119 for restricted profiles, PyInstaller LD_LIBRARY_PATH/LD_PRELOAD restore at :17-20/:53-59). But there is no self-hardening of the agent process: zero hits for RLIMIT_CORE/setrlimit/PR_SET_DUMPABLE/PT_DENY_ATTACH across src/; the only prctl usage is PR_SET_PDEATHSIG in utils/sleep_inhibitor.py:169-185, which is child-lifetime management, not anti-debug/core-dump hardening.

**Adopt.** At CLI entry (before config/auth load): resource.setrlimit(RLIMIT_CORE, (0, 0)); on Linux call prctl(PR_SET_DUMPABLE, 0) via ctypes; strip DYLD_*/LD_* from os.environ (after capturing any PyInstaller _ORIG values subprocess_env needs). A core dump of the agent process contains API keys and conversation data, so this is cheap leak prevention; keep it best-effort (log, never abort) since Python startup differs from a native pre-main hook.

**Files.** `src/pythinker_code/utils/subprocess_env.py`, `src/pythinker_code/cli/__init__.py`, `<ref>/process-hardening/src/lib.rs`

### `mcp/bounded-retry-backoff-for-retryable-http-transport-initializ` — missing, S, medium

**Today.** Missing. _connect_server (soul/toolset.py) makes a single connect attempt per server; any transient failure (gateway blip, 429) permanently marks the server 'failed' for the whole session with no retry and no per-server reconnect command (/reload restarts the session wholesale).

**Verifier note.** Verdict stands as claimed, and the project's own progress log confirms it: _connect_server makes a single attempt (status pending -> connecting -> connected|failed, no retry loop; rg for retry/backoff/reconnect in toolset.py and all of src returns zero MCP hits). Granular per-server /mcp reconnect/disconnect was explicitly scoped as mcpext-2(b) and DEFERRED ('/reload covers coarsely') per tasks/agent-enhancement-remaining-plan.md.

**Adopt.** Wrap the connect attempt for remote (http/sse) servers in a small retry loop (2 retries, 250ms/1s delays, all inside the startup-timeout deadline), retrying only on connection errors and 408/429/5xx-shaped exceptions, never on auth errors. Optionally add a `/mcp reconnect <name>` action to re-attempt a failed server without a session reload.

**Files.** `src/pythinker_code/soul/toolset.py`, `<ref>/rmcp-client/src/streamable_http_retry.rs`

### `mcp/mcp-resources-and-prompts-surfaced-to-the-model-list-read-wi` — partial, S, medium

**Today.** Partial. tools/mcp_resource/__init__.py provides ListMcpResources and ReadMcpResource (with untrusted-data wrapping and binary placeholders), and resources/prompts are captured at connect with METHOD_NOT_FOUND-aware discovery (_discover_optional_capability in soul/toolset.py). Gaps: resource templates are not listed, prompts are listed but cannot be fetched/invoked (no get_prompt anywhere), and the inventory is frozen at connect time.

**Verifier note.** Verdict and details fully confirmed — this is the mcpext-1 arc, landed 2026-06-08 (commit 4a8424e0 per tasks/agent-enhancement-remaining-plan.md:287). ListMcpResources/ReadMcpResource exist with untrusted-data wrapping (builder.mark_untrusted) and binary placeholders; resources/prompts captured at connect via _discover_optional_capability with METHOD_NOT_FOUND awareness. Confirmed gaps exactly as claimed: no list_resource_templates anywhere, prompts are listed (prompt.name + description) but never fetched — zero get_prompt hits in src — and the inventory is frozen at connect time.

**Adopt.** Add a GetMcpPrompt tool (or surface server prompts as parameterized slash commands) using fastmcp's get_prompt, and include resource templates in ListMcpResources output; re-list on demand inside the tools rather than serving the connect-time snapshot so late-published resources appear.

**Files.** `src/pythinker_code/tools/mcp_resource/__init__.py`, `src/pythinker_code/soul/toolset.py`, `<ref>/mcp/src/connection_manager.rs`

### `mcp/required-server-gating-at-session-init` — missing, S, medium

**Today.** Missing. MCPServerInfo (soul/toolset.py) has no required/optional distinction; _connect raises MCPRuntimeError if ANY server fails, but only when wait_for_mcp_tools is awaited — in normal background mode failures degrade to a toast and the session proceeds regardless. mcp.json schema (cli/mcp.py) has no required field.

**Verifier note.** Verdict stands: no required/optional field exists anywhere (MCPServerInfo has only status/client/tools/resources/prompts; mcp.json schema in cli/mcp.py writes only command/args/env or url/transport/headers/auth). However the claimed mechanics are wrong in one material way: wait_for_mcp_tools IS awaited in normal background mode — PythinkerSoul._agent_loop awaits wait_for_background_mcp_loading() at the start of every turn, so a failed server does not merely toast; the MCPRuntimeError raised by _connect re-raises into the first agent turn (then the task is cleared in wait_for_mcp_tools' finally and subsequent turns proceed without the failed server). Effectively every server is hard-surfaced once, then silently optional — still no required/optional distinction.

**Adopt.** Add an optional `required: true` per-server key in mcp.json; on session start (or first turn), await only required servers and surface a hard, aggregated error if any failed, leaving optional servers best-effort. Change MCPRuntimeError raising to cover only required servers so optional failures stop poisoning wait_for_mcp_tools.

**Files.** `src/pythinker_code/soul/toolset.py`, `src/pythinker_code/cli/mcp.py`, `<ref>/mcp/src/connection_manager.rs`

### `multi-agent/multi-target-wait-primitive-returning-per-agent-status-map` — partial, S, medium

**Today.** Partial. TaskOutput(block=true) in src/pythinker_code/tools/background/__init__.py waits on exactly one task; BackgroundTaskManager.wait() (src/pythinker_code/background/manager.py:495) is single-task; a session-wide completion_event and automatic completion notifications already exist, plus poll-escalation counters (note_nonblocking_poll/note_blocking_timeout) that discourage serial polling.

**Verifier note.** Claim upheld, but partial credit is larger than stated: (1) TaskList tool (src/pythinker_code/tools/background/__init__.py:239-270) returns a status snapshot of all active/terminal tasks in one call; (2) foreground RunAgents already does a multi-target gather — asyncio.gather over all children with per-child results returned inline (src/pythinker_code/tools/agent/__init__.py:756, summarized via subagents/usage.py summarize_batch/aggregate_findings). What is genuinely absent is a blocking multi-target wait over arbitrary already-running task ids.

**Adopt.** Extend TaskOutput (or add TaskWait) to accept task_ids: list[str]; loop on the manager's completion_event with a deadline, return on the first task reaching a terminal status (collecting any others already terminal), and emit a per-task status map plus timed_out. Treat unknown ids as status=not_found entries rather than tool errors.

**Files.** `src/pythinker_code/tools/background/__init__.py`, `src/pythinker_code/background/manager.py`

### `observability-feedback/http-response-debug-context-extraction-on-provider-api-error` — partial, S, medium

**Today.** Partial. The soul reads `request_id` off exceptions when the provider SDK exposes it and logs it (src/pythinker_code/soul/pythinkersoul.py:1450-1457), and api_error events already carry only error_type/status/model family — no bodies (pythinkersoul.py:1466-1476). But there is no generic header-level extraction for SDKs that don't surface request_id, no gateway ray capture, despite an httpx response hook already existing for the rate-limit cache (src/pythinker_code/llm.py:139).

**Verifier note.** Verdict 'partial' stands but the claimed state understates what exists: the sub-claim 'no generic header-level extraction for SDKs that don't surface request_id' is wrong. pythinker-core's error conversion reads x-request-id directly off response headers in two places — for provider-SDK status errors AND for raw httpx errors that leak through streaming — and additionally captures the parsed response BODY on APIStatusError (.body), which the shell UI uses to surface structured 429 usage-limit detail. The soul then logs request_id. Still genuinely missing: gateway-ray (cf-ray) capture, and attaching request_id/headers to the api_error telemetry event (which carries only error_type/status/model family).

**Adopt.** Extend the existing httpx response event hook to retain the last error response's request-id/ray/auth headers per provider in a tiny process cache; on classify_api_error, merge that context into the api_error event, the user-facing failure line, and the recent-errors ring entry. Keep the status-only message policy for exported telemetry.

**Files.** `<ref>/response-debug-context/src/lib.rs`, `src/pythinker_code/llm.py`, `src/pythinker_code/soul/pythinkersoul.py`

### `patch-file-tools/auto-create-missing-parent-directories-on-file-write` — partial, S, medium

**Today.** src/pythinker_code/tools/file/write.py returns a hard ToolError ('parent directory does not exist') when the parent is missing, costing the model a mkdir round-trip; the mkdir(parents=True) courtesy exists only for plan-file writes (line 126).

**Verifier note.** Claim confirmed as stated. WriteFile hard-errors on missing parents; mkdir(parents=True) exists only for the plan-file path.

**Adopt.** In WriteFile, after approval (the prompt already displays the full path, mitigating typo risk), replace the parent-exists error with mkdir(parents=True, exist_ok=True) before writing; keep the error only when the parent path exists but is not a directory. Mention created directories in the success message so the transcript stays auditable.

**Files.** `<ref>/apply-patch/src/lib.rs`, `src/pythinker_code/tools/file/write.py`

### `persistence-resume/session-provenance-metadata-captured-at-creation-git-snapsho` — missing, S, medium

**Today.** Missing. The wire.jsonl header records only protocol_version (src/pythinker_code/wire/file.py WireFileMetadata); SessionState has no provenance fields at all (src/pythinker_code/session_state.py); fork_session records lineage only as a 'Fork: <title>' prefix with no machine-readable forked_from id (src/pythinker_code/session_fork.py:333-343); subagent parentage exists only via the subagents/ dir layout.

**Verifier note.** Claim confirmed. SessionState (src/pythinker_code/session_state.py:50-72) has no provenance fields; the wire header WireFileMetadata carries only protocol_version (src/pythinker_code/wire/file.py:20-26); fork_session sets lineage only as a title string f'{title_prefix}: {source_title}' with no machine-readable forked_from id (src/pythinker_code/session_fork.py:333-343); rg for forked_from/parent_session/lineage finds nothing. Two near-misses worth noting that do NOT satisfy the capability: (a) per-step model_name and provider_key are recorded in wire StatusUpdate records (src/pythinker_code/wire/types.py:233-235), so model identity is recoverable from the transcript per turn but is not creation-time session metadata; (b) the git branch is computed only for the welcome banner display via _safe_git_branch (src/pythinker_code/app.py:45-63, 789-791) and never persisted. Web Session model (web/models.py) also has no provenance fields.

**Adopt.** Extend SessionState with created_at, cli_version, model_id, source (cli/web/acp), forked_from_id, and a git snapshot (branch, commit, origin URL via one subprocess at Session.create, skipped outside repos). Set forked_from_id in fork_session and parent linkage when subagent sessions are materialized. Surface branch/commit in the session picker, web list, and /recap; lineage enables fork grouping and provenance during debugging (which commit a session's edits were made against).

**Files.** `src/pythinker_code/session_state.py`, `src/pythinker_code/session.py`, `src/pythinker_code/session_fork.py`, `<ref>/thread-store/src/thread_metadata_sync.rs`, `<ref>/rollout/src/recorder.rs`

### `prompts-instructions/ambition-vs-precision-calibration-for-greenfield-vs-existing` — partial, S, medium

**Today.** Only the precision half exists: Rule 7 'smallest complete change' and §6 'no features beyond what was asked' apply unconditionally (system.md), which can produce flat, minimal output on greenfield 'build me X' asks where users expect creative completeness.

**Verifier note.** Claimed verdict label (partial) ends up right, but the claimed state is factually wrong: 'only the precision half exists' is refuted. The exact calibration ships verbatim in src/pythinker_code/prompts/best_practices.md line 134 (Final answers section): 'Ambition vs. precision: for brand-new projects, be ambitious and demonstrate creativity. In an existing codebase, do exactly what the user asks with surgical precision — no renaming files or variables, no relocating code, no unrequested improvements.' The real gap is narrower than claimed: this rule is opt-in — injected only when the user runs /best-practices (soul/slash.py lines 301-324, session-scoped system message; rg shows prompts.BEST_PRACTICES is referenced nowhere else) — while the always-on system.md applies Rule 7 'Smallest complete change' (line 30) and §6 'No features beyond what was asked' (line 150) unconditionally, with no greenfield carve-out in the default prompt.

**Adopt.** Add 2-3 sentences to §3 or §6 scoping the minimalism rules to existing codebases, and granting judicious ambition (sensible extras, polished defaults, stated as assumptions) for from-scratch projects with vague scope. Pure prompt edit; re-pin phrase-guard tests.

**Files.** `src/pythinker_code/agents/default/system.md`

### `prompts-instructions/uniform-mode-transition-semantics-and-a-pair-programming-col` — partial, S, medium

**Today.** Pythinker has plan mode (tool-enforced read-only, persisted, periodic reminders — soul/dynamic_injections/plan_mode.py), auto mode with an explicit disable-reminder canceling prior guidance (auto_mode.py AUTO_DISABLED_REMINDER), goal mode (goal_mode.py), and the new orchestration provider defers to stronger modes (orchestration.py _stronger_mode_active). But mode supersession wording is ad hoc per provider, there is no uniform 'mode X active, prior mode guidance void' contract, and no pair-programming/interactive style exists at all.

**Verifier note.** Claim survives as stated. Supersession wording is per-provider and inconsistent; no pair-programming/interactive collaboration style exists anywhere in the prompt surface.

**Adopt.** Standardize a one-line activation/deactivation preamble across mode injections ('<mode> is now active; guidance from previously active modes no longer applies'), emitted on every toggle so stale-mode bleed-through (especially across compaction) is impossible; optionally add a /pair style toggle that injects small-step pacing + ask-the-user-for-observations debugging guidance.

**Files.** `src/pythinker_code/soul/dynamic_injections/plan_mode.py`, `src/pythinker_code/soul/dynamic_injections/auto_mode.py`, `src/pythinker_code/soul/dynamic_injections/orchestration.py`

### `protocol-headless/final-message-to-file-output-o` — missing, S, medium

**Today.** Missing. --final-message-only prints the final message to stdout (src/pythinker_code/ui/print/visualize.py FinalOnly*Printer) but there is no file-output option; rg for last_message/output-last-message in src/pythinker_code finds only unrelated feedback.py context fields.

**Verifier note.** Claim confirmed; could not refute. FinalOnlyTextPrinter/FinalOnlyJsonPrinter write only to stdout (src/pythinker_code/ui/print/visualize.py:145-153, 189-197 via print/_print_final_text). The complete CLI flag inventory (src/pythinker_code/cli/__init__.py:332-638) has no -o/--output-file/--last-message option; rg for last_message/output-file/output_file across src/pythinker_code finds nothing relevant.

**Adopt.** Add --output-file FILE for print mode: capture the final assistant text (the FinalOnly printers already isolate it) and write it on exit regardless of output format; write empty content with a stderr warning when the turn produced no final message. Composes with stream-json so callers get both the event stream and the answer artifact.

**Files.** `src/pythinker_code/ui/print/visualize.py`, `src/pythinker_code/cli/__init__.py`, `<ref>/exec/src/event_processor.rs`

### `protocol-headless/progress-to-stderr-final-answer-to-stdout-split-in-human-hea` — partial, S, medium

**Today.** Partial. Default --print text mode (TextPrinter in src/pythinker_code/ui/print/visualize.py) rich-prints every wire message to stdout, so captured output mixes progress with the answer; --final-message-only/--quiet gives a clean answer but discards progress entirely instead of moving it to stderr. No config-summary header (model/work-dir/approval posture) is emitted at run start.

**Verifier note.** Claim confirmed; could not refute. TextPrinter rich-prints every WireMessage object to stdout (src/pythinker_code/ui/print/visualize.py:38-43), mixing progress and answer in captured output. FinalOnlyTextPrinter/--quiet (visualize.py:132-153; cli/__init__.py:550-566, 707-716) buffer only ContentParts and discard all progress rather than routing it to stderr. No config-summary header (model/work-dir/approval) is emitted at print-run start — the print path (cli/__init__.py:1013-1019 run_print; Print.run in ui/print/__init__.py) prints only the command echo. --verbose (cli/__init__.py:339-345) is log verbosity, not a stderr progress channel.

**Adopt.** Make TextPrinter write progress to the original stderr stream and only the final assistant text to stdout (unconditionally, or gated on stdout-not-a-tty like the reference); print a one-block run header (model, session id, work dir, approval mode) to stderr at start. Keeps --final-message-only as the fully quiet variant.

**Files.** `src/pythinker_code/ui/print/visualize.py`, `<ref>/exec/src/event_processor_with_human_output.rs`

### `protocol-headless/robust-stdin-prompt-contract-sentinel-append-as-context-enco` — partial, S, medium

**Today.** Partial. src/pythinker_code/ui/print/__init__.py reads stdin only when no -p prompt was given and stdin is not a tty; when both -p and piped stdin are present, the piped data is silently ignored; there is no `-` sentinel and no encoding detection (sys.stdin.read() will raise or mojibake on UTF-16 input). cli/__init__.py rejects empty --prompt but has no stdin guidance message.

**Verifier note.** Claim confirmed; could not refute. src/pythinker_code/ui/print/__init__.py:73-75 reads stdin only when `command is None and not sys.stdin.isatty() and input_format == "text"` — when -p is supplied, piped stdin is silently ignored (never combined as context). No '-' sentinel exists (rg for dash handling in cli/__init__.py and ui/print/__init__.py: nothing). sys.stdin.read() uses the default text decoder with no encoding detection. cli/__init__.py:766-767 raises BadParameter('Prompt cannot be empty') with no stdin guidance.

**Adopt.** In Print.run: support prompt == '-' to force stdin; when a prompt is given and stdin is piped, append stdin wrapped in a clearly tagged context block (consistent with the existing untrusted-data conventions); read stdin as bytes and decode with BOM sniffing, exiting with a convert-to-UTF-8 hint on failure.

**Files.** `src/pythinker_code/ui/print/__init__.py`, `<ref>/exec/src/lib.rs`

### `review-mode/upstream-aware-merge-base-selection` — partial, S, medium

**Today.** diff_source.py computes `git merge-base HEAD <chosen_ref>` with a static candidate chain (origin/main, main, master) and records fallback reasons, but never checks whether the chosen local branch's upstream is ahead (packages/pythinker-review/src/pythinker_review/engine/diff_source.py:144-167). subagents/git_context.py collects no merge-base at all.

**Verifier note.** Claim CONFIRMED. The candidate chain is static (origin/main → main → master) and there is no upstream-tracking-ref logic anywhere in src/pythinker_code or packages/. The only @{u}-adjacent git calls are current-branch lookups (`rev-parse --abbrev-ref HEAD`), never upstream comparisons. Minor line drift only: the merge-base block actually spans diff_source.py L144-177 (merge-base call at L161), defaults at L94-95.

**Adopt.** In diff_source.resolve_diff (and the new target resolver), after choosing a base ref, resolve `<ref>@{upstream}`; if `git rev-list --left-right --count <ref>...<upstream>` shows the upstream ahead, use the upstream for the merge-base and record it in the existing fallback_reason audit field. Two extra guarded git calls, fully covered by the existing PreflightError handling.

**Files.** `packages/pythinker-review/src/pythinker_review/engine/diff_source.py`, `src/pythinker_code/subagents/git_context.py`

### `skills-hooks-memories/hook-output-spill-to-disk-with-recovery-path` — partial, S, medium

**Today.** hooks/engine.py:33-39 hard-truncates hook stdout/stderr at 12,000 chars with a '...[truncated]' suffix — the tail is lost and there is no recovery path. Pythinker already has disk-spill infrastructure for oversized tool output from a prior arc that this could reuse.

**Verifier note.** Claim confirmed, with one precision fix: the 12,000-char truncation (_MAX_HOOK_OUTPUT_CHARS, engine.py:33-39) applies in _hook_outputs_for_wire (engine.py:42-58), i.e. the wire/UI display path; the in-memory HookResult keeps full stdout, and the post-compact additional_context path is separately bounded by MAX_RESTORED_SKILL_CHARS in build_hook_context_message (compaction_restore.py:161-183) with its own '...[truncated]' and no recovery either. The reusable disk-spill infrastructure the analyst referenced does exist: enable_spill / SPILL_MAX_CHARS=5_000_000 in tools/utils.py:58-139 (used by shell/web tools). Hooks use none of it.

**Adopt.** Replace _truncate_hook_output with the existing tool-output disk-spill helper: write full text under the session dir, return head/tail preview plus the saved path. Applies to wire-visible outputs and (once adopted) model-injected hook context.

**Files.** `src/pythinker_code/hooks/engine.py`, `<ref>/hooks/src/output_spill.rs`

### `skills-hooks-memories/pretooluse-hook-input-rewriting-updated-input` — missing, S, medium

**Today.** hooks/runner.py HookResult has only action/reason/additional_context; toolset.py:617-637 checks results solely for action=='block' and then executes the original arguments unchanged. No mechanism for a hook to modify tool input.

**Verifier note.** Claim confirmed. HookResult (hooks/runner.py:11-21) carries only action/reason/stdout/stderr/exit_code/timed_out/additional_context — no updated_input. rg for 'updated_input|updatedInput' over src returns zero hits (only 'hookSpecificOutput' for permissionDecision parsing at runner.py:81-91). toolset.py:625-633 checks PreToolUse results only for block, then executes tool.call(arguments) with the original parsed arguments at toolset.py:655. No mechanism exists for a hook to mutate tool input.

**Adopt.** Add an updated_input field to HookResult parsed from JSON stdout (hookSpecificOutput.updatedInput), and in toolset.py apply the first/last non-null rewrite to tool_input_dict before permission checks and tool.call — re-running check_tool_call_allowed on the rewritten input so a hook cannot widen permissions.

**Files.** `src/pythinker_code/hooks/runner.py`, `src/pythinker_code/soul/toolset.py`, `<ref>/hooks/src/events/pre_tool_use.rs`

### `skills-hooks-memories/skill-usage-doctrine-in-the-system-prompt-trigger-rules-prog` — partial, S, medium

**Today.** agents/default/system.md §12 (lines 261-267) has a single paragraph: identify relevant skills, read SKILL.md before applying, -local companions, conserve context. No mandatory trigger rule, no read-to-EOF/no-subagent-delegation rule, no announce-usage or minimal-set/sequencing guidance, no missing-skill fallback wording.

**Verifier note.** Verdict 'partial' stands but the claimed_state materially understates what exists — skill doctrine is NOT confined to the §12 paragraph. system.md:126 (§5 tool guidance) is a mandatory trigger rule: 'Load a skill's exact instructions before applying its workflow — mandatory for review-pr, diagnose-ci-failures, fix-errors, implement-specs, spec-driven-implementation, check-impl-against-spec, resolve-merge-conflicts, and create-pr.' system.md:112 is a read-to-EOF rule for skills ('when the file is a spec, skill, or checklist you are implementing against, keep reading to the end before acting on it'). Also present: inline /skill:<name> handling doctrine (:128), skill-content authority/prompt-injection framing (:181), and checklist-walking for skill compliance claims (:216). Still genuinely absent: announce-usage wording, an explicit no-subagent-delegation rule for skill reading, minimal-set/sequencing guidance, and missing-skill fallback wording. So 'no mandatory trigger rule, no read-to-EOF rule' in the claim is factually wrong.

**Adopt.** Expand §12 with the doctrine bullets (trigger obligation when a skill is named or clearly matches, full-read rule, main-agent-reads-skills rule, minimal set + one-line announcement, fallback when a named skill is absent). Note: agent prompt text is test-pinned — update phrase pins/inline snapshots (pytest --inline-snapshot=fix) in the same change.

**Files.** `src/pythinker_code/agents/default/system.md`, `<ref>/core-skills/src/render.rs`

### `tools-registry-codemode/head-tail-capped-output-retention-drop-the-middle-keep-prefi` — partial, S, medium

**Today.** ToolResultBuilder (src/pythinker_code/tools/utils.py:168-214) is head-only: after 50K chars everything later is dropped from the inline result. Mitigations exist — tail() surfaces the last 5 non-empty lines in error briefs (shell/__init__.py:221) and enable_spill writes the full output to disk with a ReadFile recovery hint — but the inline body still loses the suffix.

**Verifier note.** Claim survives as stated. ToolResultBuilder.write is head-only (stops appending once max_chars=50_000 reached); suffix recovery exists only via tail() in error briefs and enable_spill disk spill with a ReadFile hint. The only middle-drop truncation in the repo is the TUI diff renderer (display-only, not the model-facing result).

**Adopt.** Convert ToolResultBuilder's buffer to a head+tail budget (e.g. 60% head / 40% tail) with an explicit '[... N chars omitted ...]' marker between segments; keep disk spill as the full-output escape hatch. Touches only utils.py plus truncation-marker assertions in tests.

**Files.** `src/pythinker_code/tools/utils.py`, `<ref>/core/src/unified_exec/head_tail_buffer.rs`

### `config-features/central-feature-flag-registry-with-lifecycle-staging` — missing, M, medium

**Today.** Missing. Toggles are scattered Pydantic booleans across config sections (MemoryConfig.lexical_recall/injection_bus/durable_memory, TUIConfig.turn_recaps/smooth_streaming, GoalConfig.auto_continue, etc. in src/pythinker_code/config.py) with no staging metadata, no experimental menu, no deprecation/removal pathway, and no warning when an experimental flag is on.

**Verifier note.** Claim confirmed. No flag registry, staging metadata, experimental menu, or deprecation pathway exists; toggles are plain Pydantic booleans.

**Adopt.** Add a small registry module (list of dataclasses: key, stage, default, description, optional announcement) backing a [features] table in config; route new experimental behaviors through it; render an /experimental toggle list in the settings selector; print a one-line warning when under-development flags are enabled; keep removed keys parseable as no-ops so old configs survive upgrades.

**Files.** `src/pythinker_code/config.py`, `src/pythinker_code/ui/shell/selectors/settings.py`, `<ref>/features/src/lib.rs`

### `config-features/comment-preserving-targeted-config-edits` — partial, M, medium

**Today.** Partial. Writes correctly target the single user file by re-loading it first (e.g. src/pythinker_code/soul/pythinkersoul.py:848, auth/oauth.py, web/api/config.py), but save_config in src/pythinker_code/config.py dumps the entire validated model (model_dump -> tomlkit.dumps), destroying comments/ordering and materializing every default into the file on any single-field persist.

**Verifier note.** Claim confirmed. All persist paths re-load the user file then call save_config, which serializes a fresh full model_dump through tomlkit.dumps — no round-trip document editing, so comments/ordering are destroyed and all defaults are materialized.

**Adopt.** Add an edit helper that parses the user file as a tomlkit TOMLDocument, applies targeted set/delete of dotted keys, and writes back (tomlkit already round-trips comments); migrate the persist-one-setting call sites (thinking effort, auth tokens, web API) onto it, leaving full save_config for initial file creation.

**Files.** `src/pythinker_code/config.py`, `src/pythinker_code/soul/pythinkersoul.py`, `src/pythinker_code/web/api/config.py`, `<ref>/config/src/mcp_edit.rs`

### `config-features/named-user-config-profile-overlays` — partial, M, medium

**Today.** Partial. agent_execution_profile (src/pythinker_code/config.py _apply_agent_execution_profile + src/pythinker_code/execution_profiles.py) is a fixed enum of behavioral presets that fill unset fields — useful, but users cannot author arbitrary named config overlays (different model/provider/theme bundles) and switch with a flag.

**Verifier note.** Claim confirmed as partial. agent_execution_profile is a fixed 5-value Literal filling unset behavioral fields; users cannot author arbitrary named config bundles. One adjacent escape hatch the claim omits: agent spec files (--agent/--agent-file) can pin a model per named agent, but that covers only the model field, not provider/theme/config overlays.

**Adopt.** Support ~/.pythinker/<name>.config.toml selected via --profile or PYTHINKER_PROFILE, merged as an extra scope between user and project with provenance label 'profile <name>'; keep existing execution-profile presets unchanged. Settings writes while a profile is active should target the profile file.

**Files.** `src/pythinker_code/config.py`, `src/pythinker_code/execution_profiles.py`, `<ref>/config/src/profile_toml.rs`

### `context-mgmt/conversation-carry-over-on-model-switch-compact-with-the-out` — missing, M, medium

**Today.** Missing. /model switching creates a brand-new session and discards the conversation entirely (src/pythinker_code/ui/shell/slash.py:359-366 'Starting fresh session for the new model...'); in-runtime LLM swaps (soul/pythinkersoul.py:825-835) would compact lazily with the NEW model, whose window may not fit the request.

**Verifier note.** Claim confirmed, but fix the second citation: pythinkersoul.py:825-835 is the thinking-effort swap (same model recreated via create_llm), not a model switch. The real in-runtime model swap that keeps history is the ACP server's setSessionModel path — it replaces runtime.llm in place (acp/server.py:439-448), so any later compaction runs with the NEW model whose window may not fit; same conclusion, stronger evidence.

**Adopt.** On model switch, instead of always forking an empty session, offer carry-over: while the old LLM is still live, run compact_context (optionally with a 'handoff to a different model' instruction) when the history exceeds the new model's trigger threshold, then swap the LLM in place (or seed the new session with the compaction summary). Keeps multi-hour threads usable across model changes.

**Files.** `src/pythinker_code/ui/shell/slash.py`, `src/pythinker_code/soul/pythinkersoul.py`

### `core-loop/per-turn-aggregated-diff-tracker-net-unified-diff-of-all-fil` — missing, M, medium

**Today.** Missing. Pythinker has per-file restore points for undo (file_restore.py) and per-tool-call diff rendering in the TUI, but no turn-level aggregation: there is no 'what did this turn change overall' artifact for the UI, hooks, or review flows (rg for turn_diff/TurnDiff over src/pythinker_code is empty).

**Verifier note.** Claim confirmed. No turn-level diff aggregation exists. Closest analogs found (none per-turn, none net-unified-diff): per-file restore points for undo; a session-level files-modified NAME list for the resume recap; a web API endpoint computing workdir-vs-HEAD git numstat (session/workdir scope, not agent-turn scope); and per-tool-call DiffDisplayBlock rendering.

**Adopt.** Hook WriteFile/StrReplaceFile (and any patch tool) to report (path, before, after) into a turn-scoped baseline map keyed by first-seen content; at turn end render a net unified diff (difflib, with a size cutoff fallback to a stat summary) and emit it as a wire event plus a /diff slash command. The existing file_restore capture point already has before-content in hand.

**Files.** `src/pythinker_code/file_restore.py`, `src/pythinker_code/tools/file/write.py`, `src/pythinker_code/soul/pythinkersoul.py`

### `exec-safety/durable-always-allow-policy-amendment-across-sessions` — partial, M, medium

**Today.** Partial. 'Approve for session' records a signature key in ApprovalState.auto_approve_actions which persists only with that session's state (src/pythinker_code/session_state.py:18, src/pythinker_code/soul/agent.py:311-332); there is no user- or project-durable grant, so the same approval is re-asked in every new session.

**Verifier note.** Claim confirmed as stated. approve_for_session writes a shell_command_signature-scoped key into ApprovalState.auto_approve_actions, which is persisted and restored only with that session's state (so it survives resume, not new sessions). The only durable knobs are blanket ones: config default_yolo (config.py:858) and workspace trust/safe_mode (session.state.trust, saved in agent.py:320) — neither is a per-command grant. No user- or project-level per-command store exists.

**Adopt.** Add an 'approve always' response tier to the approval runtime (models.py ApprovalResponseKind) that appends the command's signature prefix to a durable rules file (the policy file from the declarative-policy finding, or a minimal JSON-lines grants file as a first step) with fcntl locking and dedup, loaded and merged into auto-approve checks at startup. Keep the existing invariant: destructive and config-surface calls are never eligible, mirroring _is_session_approvable.

**Files.** `src/pythinker_code/approval_runtime/models.py`, `src/pythinker_code/soul/approval.py`, `src/pythinker_code/session_state.py`, `<ref>/execpolicy/src/amend.rs`

### `exec-safety/structural-ast-shell-parsing-for-safety-classification-of-co` — partial, M, medium

**Today.** Partial. permission.py uses shlex with punctuation_chars plus targeted patches — a regex for $()/backticks/<()/>(), a glued-operator double-lex diff, and an unquoted-newline check (_shell_hidden_command_reason) — which is fail-closed but heuristic; consequently `bash -c '...'` is treated as opaque/mutating wholesale (interpreters listed in _MUTATING_COMMANDS), so wrapped read-only scripts can never be classified or safe-listed.

**Verifier note.** Claim confirmed. All classification is shlex-token based with fail-closed heuristics: _shell_hidden_command_reason (permission.py:434) detects $()/backticks/<()/>() via regex plus a punctuation_chars double-lex diff (lines 449-462) and unquoted newlines; opaque commands get a self-scoped 'shell:opaque:' signature (permission.py:944-945). Shell interpreters and script runtimes (bash/sh/zsh/python/node/perl...) are listed wholesale in _MUTATING_COMMANDS (permission.py:140-157), so bash -c '<read-only script>' can never be classified safe. No AST parser in pythinker's own code: bashlex appears in uv.lock only as a transitive dep of an unrelated package (batrachian-toad, uv.lock:299,312) and is never imported under src/; no tree-sitter.

**Adopt.** Introduce an optional structural parse (tree-sitter-bash via py-tree-sitter, or bashlex) used in two places: (a) replace/back up _shell_hidden_command_reason with whitelist-of-node-kinds rejection, eliminating the documented shlex blind-spot patches; (b) when a command is `bash|zsh|sh -c/-lc <script>`, extract word-only sub-command sequences and run each through the existing segment classifiers instead of blanket-blocking the interpreter, keeping today's behavior as the fallback when the parse rejects. Gate behind a parser-availability check so the dependency stays optional.

**Files.** `src/pythinker_code/soul/permission.py`, `<ref>/shell-command/src/bash.rs`

### `exec-safety/windows-powershell-aware-command-safety-classification` — partial, M, medium

**Today.** Partial-to-missing. The Shell tool spawns PowerShell on Windows (src/pythinker_code/tools/shell/__init__.py:89-99, 390-393) but every classifier in src/pythinker_code/soul/permission.py is POSIX/shlex-based: no PowerShell cmdlets appear in _MUTATING_COMMANDS or shell_destructive_reason, so the read-only profile gate, destructive deliberation backstop, and session-approval signatures are largely blind for PowerShell commands (Remove-Item -Recurse -Force is neither mutating nor destructive today).

**Verifier note.** Claim confirmed, with one small nuance. The Shell tool is PowerShell-aware only for spawn/description (shell/__init__.py:90-98 loads powershell.md; :392-393 spawns '<path> -command <cmd>'), while soul/permission.py contains zero PowerShell cmdlets — rg for Remove-Item/cmdlet/powershell across permission.py returns nothing — so the read-only mutation gate, destructive backstop, and network gate are blind to cmdlets. Nuance: session-approval signatures are not entirely blind — shell_command_signature still keys on the first token (e.g. 'remove-item'), so approvals are scoped per cmdlet family; the real hole is that shell_destructive_reason never matches cmdlets, so Remove-Item -Recurse -Force IS session-approvable and never deliberation-bounced.

**Adopt.** Add a PowerShell branch to the permission classifiers: a light tokenizer (split on ;, |, && with quote awareness), Verb-Noun cmdlet classification by verb family (Get/Read/Select/Measure read-only; Remove/Set/New/Clear/Stop mutating; Remove-Item with -Recurse+-Force, Format-Volume etc. destructive), suffix/case normalization for .exe/.cmd/.bat, and alias mapping (rm/del/ri -> Remove-Item). Wire it in when the active shell is PowerShell so signatures, mutation, and destructive checks share the same data, mirroring how the POSIX path shares _unwrap_command.

**Files.** `src/pythinker_code/soul/permission.py`, `src/pythinker_code/tools/shell/__init__.py`, `<ref>/shell-command/src/command_safety/windows_dangerous_commands.rs`

### `mcp/mcp-elicitation-requests-with-policy-based-auto-accept-auto-` — missing, M, medium

**Today.** Missing. No elicitation handling exists (grep for elicit across src/pythinker_code returns nothing); fastmcp 3.2.0 (pyproject.toml) supports an elicitation_handler on Client but pythinker never registers one, so servers that require elicitation fail or hang.

**Verifier note.** Verdict stands as claimed. rg -i 'elicit' across src/pythinker_code, tests, tests_e2e, tasks returns zero hits; fastmcp is pinned at 3.2.0 (pyproject.toml:47) and no elicitation_handler is ever passed to fastmcp.Client (toolset.py:1038 constructs it bare).

**Adopt.** Register a fastmcp elicitation_handler that consults the existing approval system: decline by default in non-interactive/never-ask modes, auto-accept only empty-schema confirmations when the active profile would auto-approve MCP actions anyway, and otherwise raise an approval request through runtime.approval with the server name and message. Fail closed (decline) on any doubt.

**Files.** `src/pythinker_code/soul/toolset.py`, `src/pythinker_code/soul/permission.py`, `<ref>/mcp/src/elicitation.rs`

### `mcp/model-visible-tool-name-normalization-charset-sanitization-d` — missing, M, medium

**Today.** Missing. MCPTool (soul/toolset.py) exposes the raw mcp_tool.name to the model with no charset/length normalization anywhere (grep for sanitize/[A-Za-z0-9_-] found only unrelated hits); cross-server same-name collisions are last-wins with a warning log; mcp.json server names are unvalidated (only the stderr log filename is sanitized via _MCP_LOG_NAME_RE).

**Verifier note.** Verdict stands as claimed. MCPTool.__init__ passes name=mcp_tool.name raw to the model-visible tool list; no charset/length normalization exists in toolset.py or llm.py (grep for sanitize/re.sub on tool names returned nothing relevant). Cross-server same-name collisions are last-wins with a warning (toolset.py:363-373); MCP-vs-builtin collisions skip the MCP tool. The only name sanitization is _MCP_LOG_NAME_RE for the stderr log filename, exactly as the claim says.

**Adopt.** At registration, sanitize the model-visible name to the provider-safe charset, cap length (~64 chars), and on collision (cross-server or post-sanitize) append a short stable hash of (server, raw name) instead of silently shadowing; keep the raw name on MCPTool for the actual call_tool protocol request. Validate server names on `mcp add`.

**Files.** `src/pythinker_code/soul/toolset.py`, `src/pythinker_code/cli/mcp.py`, `<ref>/mcp/src/tools.rs`

### `observability-feedback/code-retention-outcome-analytics-accepted-line-counts-from-u` — missing, M, medium

**Today.** Missing. No per-turn edit-volume telemetry exists; feedback.py collects diff stats only on explicit /feedback (src/pythinker_code/feedback.py:_collect_git_snapshot), and grep over src/pythinker_code shows no added/deleted-line tracking tied to turns. This is the main outcome metric (does the agent's code stick?) absent from the fleet view.

**Verifier note.** Claim stands. Verified via full track()-event inventory (no edit-volume/line-count event exists) and grep for additions/deletions/numstat across src. Line stats exist only as on-demand display surfaces: the web API git-diff endpoint (GitDiffStats with total_additions/total_deletions via `git diff --numstat HEAD`) and /feedback's diff_stat snapshot. Nothing ties added/deleted lines to turns or reaches telemetry.

**Adopt.** After each turn that ran file-mutating tools, compute `git diff --numstat` (or diff the Edit/Write tool results) in the workdir, derive added/deleted effective-line counts with the same normalization rules, and attach counts (never content or hashes) to the per-turn rollup event, plus an optional salted repo-remote hash for cohorting. Gate behind the existing telemetry kill switch.

**Files.** `<ref>/analytics/src/accepted_lines.rs`, `src/pythinker_code/feedback.py`, `src/pythinker_code/soul/pythinkersoul.py`

### `observability-feedback/per-attempt-api-request-and-stream-event-health-telemetry` — partial, M, medium

**Today.** Partial. Pythinker meters whole LLM calls (duration/success/tokens, src/pythinker_code/soul/pythinkersoul.py:1607-1700; telemetry/metrics.py record_llm_call) and tracks api_error on final failure; tenacity retries are surfaced to the UI/log (_before_step_retry_sleep, pythinkersoul.py:1705-1710) but not metered, and there is no stream-event-level health accounting (parse failures, idle timeout, early close) for diagnosing flaky providers in a BYO-key multi-provider fleet.

**Verifier note.** Verdict 'partial' stands but two details need correction: (1) per-attempt metering DOES exist at the span/metric layer — the pythinker.llm span plus record_llm_call(success=False) and record_error(kind='api_error') live inside _run_step_once, which is exactly the unit tenacity retries, so EVERY failed attempt (not just final failure) emits a failed-llm-call metric, an api_error metric, and an error-annotated llm span. (2) Only the track('api_error') analytics event is final-failure-only. Confirmed correct: retries are surfaced via wire StepRetry + log only (no retry counter metric), and there is no stream-event-level health accounting (no parse-failure/idle-timeout/early-close telemetry found in src or packages/pythinker-core).

**Adopt.** Add a retry counter metric incremented in the tenacity before_sleep hook (tagged with error_type and attempt), and an attempt attribute on the llm span. In the provider streaming layer, count malformed/early-closed stream events into a low-cardinality `pythinker.llm.stream_anomalies` counter (kind = parse_error | idle_timeout | early_close).

**Files.** `<ref>/otel/src/events/session_telemetry.rs`, `src/pythinker_code/soul/pythinkersoul.py`, `src/pythinker_code/telemetry/metrics.py`

### `observability-feedback/turn-latency-profile-decomposition-and-time-to-first-token-t` — partial, M, medium

**Today.** Missing. Pythinker records whole-turn, whole-llm-call, and whole-tool durations as separate metrics (src/pythinker_code/telemetry/metrics.py, soul/pythinkersoul.py:1607-1700, soul/toolset.py:646), but no first-token timing exists anywhere (visualize UI computes display-only token rates: src/pythinker_code/ui/shell/visualize/_blocks.py:508) and no per-turn breakdown distinguishes harness overhead from sampling vs tool-blocking time; retry counts are logged but not metered.

**Verifier note.** Claimed 'missing' is overstated; correct verdict is partial. TTFT is confirmed absent everywhere (only display-side token rates in the visualize UI). BUT per-turn latency decomposition exists at the trace level: start_span deliberately nests spans into a connected tree (pythinker.turn -> pythinker.llm -> pythinker.tool, with tool.duration_ms attributes), and the default trace sample rate is 1.0, so harness overhead vs sampling vs tool-blocking time is derivable per turn from exported traces. What's missing is metrics-level (aggregate histogram) decomposition, TTFT, and a retry counter — though note each failed retry attempt IS already metered as a failed llm call (see claim 7).

**Adopt.** Instrument the streaming callback (on_message_part) to record monotonic first-token time per llm call and per turn; emit a pythinker.turn.ttft_seconds histogram. Accumulate per-turn buckets in the soul loop: time before first llm call, sum of llm-call durations, sum of tool-task durations, residual overhead; attach as turn span attributes and to the per-turn rollup event, with retry counts from the tenacity hooks.

**Files.** `<ref>/analytics/src/facts.rs`, `<ref>/otel/src/events/session_telemetry.rs`, `src/pythinker_code/soul/pythinkersoul.py`, `src/pythinker_code/telemetry/metrics.py`, `src/pythinker_code/ui/shell/visualize/_blocks.py`

### `patch-file-tools/first-class-delete-rename-file-operations-with-diff-display-` — missing, M, medium

**Today.** Pythinker has only WriteFile (src/pythinker_code/tools/file/write.py) and StrReplaceFile (replace.py); there is no audited delete or rename tool, so the model must shell out to rm/mv, which bypasses build_diff_blocks display and create_file_restore_point snapshots (file_restore.py is only invoked from write.py/replace.py). Multi-edit batches are validated in-memory before write, but only within a single file.

**Verifier note.** Claim survives. No delete/rename/move file tool exists under any name; restore points are created only by WriteFile and StrReplaceFile.

**Adopt.** Add delete and rename capabilities to the file toolset (either a small DeleteFile/MoveFile pair or ops on the existing tools) that run the same workspace/symlink validation, refuse directories, show the removed/moved content as a diff block in the approval prompt, snapshot via create_file_restore_point (capturing overwritten destination content on rename), and auto-create destination parents. Optionally accept a list of per-file ops validated fully before the first write for multi-file refactors.

**Files.** `<ref>/apply-patch/src/parser.rs`, `<ref>/apply-patch/src/lib.rs`, `src/pythinker_code/tools/file/write.py`, `src/pythinker_code/file_restore.py`

### `patch-file-tools/persistent-background-fuzzy-filename-search-session` — partial, M, medium

**Today.** src/pythinker_code/ui/shell/prompt.py LocalFileMentionCompleter delivers fuzzy @-completion backed by utils/file_filter.py: git ls-files (or capped 1000-file walk) with a 2s TTL cache invalidated on .git/index mtime, scope-aware caching, and basename re-ranking — solid for small/medium repos, but the listing rebuild is synchronous on the UI thread path, untracked files are invisible in git mode only via ls-files flags used, results cap at 1000 in walk mode, and there are no highlight indices or progress/cancellation for huge trees.

**Verifier note.** Verdict 'partial' stands, but two factual errors in the claimed state: (1) untracked files are NOT invisible in git mode — list_files_git defaults include_untracked=True and runs `git ls-files --others --exclude-standard`, also subtracting deleted working-tree files via `ls-files --deleted`; (2) fuzzy match highlighting exists in the completion menu, because the completer is prompt_toolkit's FuzzyCompleter, which styles matched characters in its display. Accurate parts: synchronous listing on the completion path (plain Completer via merge_completers, no ThreadedCompleter), 2.0s TTL (refresh_interval=2.0), .git/index mtime invalidation, scope-aware cache, 1000-entry cap in walk mode (and top-level mode), and no persistent background session/progress/cancellation.

**Adopt.** Move candidate indexing to a background thread that walks once per session and serves re-queries from memory (invalidate on .git/index mtime or watcher events); raise the candidate cap, add cancellation when the fragment changes, and return match-character indices for highlight styling in the completion menu. Keep the existing file_filter ignore rules as walker overrides.

**Files.** `<ref>/file-search/src/lib.rs`, `src/pythinker_code/ui/shell/prompt.py`, `src/pythinker_code/utils/file_filter.py`

### `patch-file-tools/shared-file-watcher-service-with-subscriber-fan-out-and-watc` — missing, M, medium

**Today.** rg over src/pythinker_code finds no watchdog/watchfiles/inotify usage anywhere; skills/ and skill/ have no reload/refresh path (manifest loaded at startup), and the mention completer polls with a 2s TTL + .git/index mtime check instead of event-driven invalidation.

**Verifier note.** Claim survives, with one nuance: while skill DISCOVERY runs once at agent construction with no reload path, ReadSkill reads skill content from disk at call time (read_skill_text_with_local_specialization), so edits to an already-discovered skill's body are picked up; only the discovered set/manifest is startup-frozen. No event-driven watching anywhere.

**Adopt.** Introduce a small watcher service (watchfiles handles the OS layer) owned by the app: subscribers register path sets and receive debounced, deduped path batches via asyncio queues; first consumers are (a) skills/AGENTS.md hot-reload with a changed-notification into the TUI, and (b) mention-completer index invalidation. Port the two non-obvious design points: refcount watches shared across subscribers, and ancestor-fallback for not-yet-existing paths with events renamed back to the requested path.

**Files.** `<ref>/file-watcher/src/lib.rs`, `<ref>/app-server/src/skills_watcher.rs`, `<ref>/app-server/src/fs_watch.rs`, `src/pythinker_code/ui/shell/prompt.py`

### `persistence-resume/full-content-transcript-search-for-session-discovery-grep-ac` — missing, M, medium

**Today.** Missing. The TUI session picker has no search input at all — only a Ctrl+A scope toggle (src/pythinker_code/ui/shell/session_picker.py); the web list's q parameter filters title and work_dir only (src/pythinker_code/web/api/sessions.py:267-272). Raw transcripts are never content-searched for discovery; memory/recall.py retrieves distilled memory blocks, not transcripts, and serves injection rather than resume.

**Verifier note.** Verdict confirmed, but the claimed_state mischaracterizes the recall landscape: besides memory/recall.py (distilled blocks), pythinker has a model-invocable cross-session Recall TOOL (src/pythinker_code/tools/recall/__init__.py, adoption arc memory-1/ctxmgmt-3) with mode='search' and mode='read' that reads full sanitized transcripts of prior sessions. However its search ranks by TITLE keyword overlap only (_rank_sessions, lines 51-66 — 'score = sum(1 for term in terms if term in title.lower())'), serves in-conversation context to the model, and is not a session-discovery/resume affordance. The TUI session picker has no search input (only Ctrl+A scope toggle, ui/shell/session_picker.py:177-183) and the web q parameter filters title/work_dir only (web/store/sessions.py:351-359). No grep/ripgrep acceleration, no transcript content matching, no match snippets anywhere.

**Adopt.** Add a type-to-filter search to the session picker and a content search mode to the web list: JSON-escape the term (json.dumps slice) and run rg -l --fixed-strings over the sessions buckets' context.jsonl/wire.jsonl, with a pure-Python line scan fallback when rg is missing; merge matched session ids into the normal listing and show a ~140-char snippet around the first match. Pythinker already vendors ripgrep discovery logic in tools/file/grep_local.py to reuse.

**Files.** `src/pythinker_code/ui/shell/session_picker.py`, `src/pythinker_code/web/api/sessions.py`, `src/pythinker_code/tools/file/grep_local.py`, `<ref>/rollout/src/search.rs`

### `persistence-resume/structured-cross-agent-session-import-with-content-hash-dedu` — partial, M, medium

**Today.** Partial. /import accepts a text file or a pythinker session id and injects flattened text as a single context message into the current session (src/pythinker_code/soul/slash.py import_context, src/pythinker_code/utils/export.py resolve_import_source/perform_import, with token-budget and sensitive-file guards). There is no structured conversion of a foreign agent transcript into a native resumable session, no detection of recent external sessions, and no dedup ledger — re-importing duplicates content.

**Verifier note.** Claim confirmed as stated. /import (registry command import_context, soul/slash.py:459-491) resolves either a UTF-8 text file or a same-workspace pythinker session id (resolve_import_source, utils/export.py:708-789), flattens session history via stringify_context_history, and appends ONE wrapped user message (build_import_message '<imported_context source=...>') to the current context with token-budget and sensitive-file guards (perform_import, utils/export.py:809-858). There is no conversion of foreign-agent transcripts into a native resumable session, no detection of external agents' recent sessions (rg for external-agent session directories reference harness session paths finds nothing), and no dedup: perform_import appends unconditionally with no content hash or ledger (rg 'already imported|ledger|sha' in export.py: no hits in the import path).

**Adopt.** Add an importer that parses a foreign JSONL transcript (role/message/timestamp records) into a new native session: user/assistant messages into context.jsonl, TurnBegin/TurnEnd records into wire.jsonl, cwd-matched to the current work dir, title from the first user line. Keep an import ledger (source path -> sha256 + imported session id) under the share dir so repeat invocations skip unchanged sources. Expose as a flag on /import (or a sessions import command); optional startup detection of recent foreign sessions for the cwd can come later.

**Files.** `src/pythinker_code/utils/export.py`, `src/pythinker_code/soul/slash.py`, `<ref>/external-agent-sessions/src/detect.rs`, `<ref>/external-agent-sessions/src/ledger.rs`, `<ref>/external-agent-sessions/src/export.rs`

### `prompts-instructions/first-class-review-target-resolution-lifecycle-preset-prompt` — partial, M, medium

**Today.** The rubric and dispatch discipline are adopted (system.md §4.1, §5 review fan-out including model-side merge-base guidance; review.yaml/code_reviewer.yaml; review-pr skill; ```report rendering in §8), but there is no /review slash command (soul/slash.py registers init/recap/compact/clear/yolo/auto/plan/goal/learn/best-practices/add-dir/export/import only), no harness-computed diff target, and no structured selectable result re-entry or interrupted-review record — the model must orchestrate all of it ad hoc per request.

**Verifier note.** Verdict correct, but the 'no /review slash command' framing undercounts the preset-prompt half: every bundled skill is auto-registered as a slash command, so /skill:review-pr IS a slash-invocable review preset prompt — soul/pythinkersoul.py SKILL_COMMAND_PREFIX (line 136) and _make_skill_runner (lines 1349-1370) inject the skill text as the user turn, and tests_e2e/test_wire_protocol.py (lines 164/364) pins 'skill:review-pr' in the advertised command list. The other two lifecycle pieces are genuinely absent: no harness-computed diff target (merge-base appears only as model-side guidance — system.md line 120 '$(git merge-base main HEAD)'; skills/review-pr/SKILL.md step 1 tells the model to 'Identify the exact diff under review and its base'; no .py code computes a review target — rg 'merge.base' hits no source besides system.md), and no structured selectable result re-entry (ui/shell/components/report.py renders ```report blocks display-only; the /reports command in ui/shell/slash.py lines 1932-1940 opens the Agent Tracing Visualizer, unrelated to review findings; no interrupted-review record exists).

**Adopt.** Add a /review [uncommitted|branch <base>|commit <sha>] command: harness resolves the target (runs git merge-base itself, embeds the SHA), dispatches the review subagent with the preset prompt + rubric, and posts results as a report message whose findings the user can reference to request fixes; on interrupt, record an explicit 'review interrupted — re-run /review' marker in history. Update the wire-handshake slash-command snapshot (tests_e2e).

**Files.** `src/pythinker_code/soul/slash.py`, `src/pythinker_code/agents/default/review.yaml`, `src/pythinker_code/agents/default/code_reviewer.yaml`

### `prompts-instructions/per-model-system-prompt-variants-scaled-to-model-capability` — partial, M, medium

**Today.** One ~41KB system.md serves every model (src/pythinker_code/agents/default/system.md, agent.yaml system_prompt_path; agentspec.py has a single system_prompt_path per agent, no model keying). Model-specific quirks are handled by a deliberate lightweight alternative: family-matched dynamic defense fragments (src/pythinker_code/soul/dynamic_injections/model_defense.py), whose docstring explicitly rejects per-model prompt cloning to keep the prompt cache-stable.

**Verifier note.** Claim survives as stated. No per-model prompt keying exists anywhere; the family-matched defense-fragment channel is the only model-specific prompting.

**Adopt.** Keep the canonical system.md but add an optional model-family→prompt-profile map (e.g. a condensed variant for small local models served via Ollama/llama.cpp that drowns in 41KB of instructions), resolved at agent load using the same substring-matching machinery as model_defense.py; default unchanged, gate behind config so prompt-cache stability is opt-out.

**Files.** `src/pythinker_code/agents/default/system.md`, `src/pythinker_code/agentspec.py`, `src/pythinker_code/soul/dynamic_injections/model_defense.py`

### `review-mode/interactive-findings-triage-select-findings-and-dispatch-fix` — partial, M, medium

**Today.** Report rendering is display-only: findings are grouped by severity and pretty-printed but there is no selection surface and no path from a rendered finding to a dispatched fix task (src/pythinker_code/ui/shell/components/report.py). The user must re-describe findings in a new prompt.

**Verifier note.** Verdict CORRECTED from missing to partial. The TUI half of the claim is right — src/pythinker_code/ui/shell/components/report.py is render-only (parse_report_block/render_report/render_finding, no selection or dispatch surface). But the claim 'no path from a finding to a dispatched fix task; the user must re-describe findings' is FALSE for the product: the pythinker-review reviewflow persists findings with stable IDs and ships a complete finding→fix dispatch loop — `pythinker review next` / `show-finding` / `triage --finding --status` / `fix --finding` / `revalidate` / `open-pr`. fix_project() plans a patch via LLM, applies it under an allowed-paths constraint, runs trusted+allowlisted validation commands, records a PatchAttempt linked to the finding, and updates finding lifecycle/history. What is genuinely missing is only an interactive picker-style selection surface (TUI or CLI — triage/fix are flag-driven, not menus).

**Adopt.** After a review report renders, offer an optional follow-up selector listing findings (severity-ordered, checkbox multi-select using the existing selector component). On confirm, synthesize a fix prompt quoting only the selected findings (title, location, body) and feed it as the next user turn — or dispatch an implementer subagent in goal-style mode. Keep it additive: plain Enter dismisses with no behavior change.

**Files.** `src/pythinker_code/ui/shell/components/report.py`, `src/pythinker_code/ui/shell/selector.py`, `src/pythinker_code/ui/shell/slash.py`

### `skills-hooks-memories/memory-usage-feedback-loop-read-citation-tracking-drives-ret` — missing, M, medium

**Today.** memory/retriever.py ranks recall purely by BM25 + 14-day recency half-life + label/path boosts; nothing records whether an injected recall block or a Recall-tool read was actually useful, and no usage signal influences future ranking or pruning (memory/recall.py, tools/recall/__init__.py). Durable MEMORY.md entries never expire.

**Verifier note.** Claim confirmed. retriever.py ranks with hand-rolled BM25 (k1=1.5, b=0.75) x recency decay (_RECENCY_HALF_LIFE_DAYS=14.0) + _PATH_BOOST=0.5 (lines 12-15, 50-85); the only 'used' variables in retriever.py:96-105 and tools/recall/__init__.py:77-106 are token/char budget counters, not usage signals. Nothing in memory/recall.py or tools/recall records whether an injected block or Recall read helped, and no signal feeds back into ranking or pruning. Durable MEMORY.md entries have no expiry (project_memory.py has only the per-store char limit and the 100-entry journal cap; no time/usage-based eviction).

**Adopt.** Record a last_used/use_count sidecar (e.g. memory/usage.json) bumped when (a) the Recall tool reads a session, (b) a recalled block's source entry text appears in later assistant output, or (c) the model Reads MEMORY.md/USER.md via file tools; fold use_count and last_used into LexicalRetriever scoring and have the approval-gated consolidation list never-used stale entries as prune candidates.

**Files.** `src/pythinker_code/memory/retriever.py`, `src/pythinker_code/memory/recall.py`, `src/pythinker_code/tools/recall/__init__.py`, `<ref>/memories/read/src/usage.rs`, `<ref>/memories/read/src/citations.rs`

### `skills-hooks-memories/memory-authored-skill-promotion-recurring-procedures-become-` — missing, M, medium

**Today.** No path from memory to skills: memory/consolidation.py targets only MEMORY.md/USER.md entries; skill authoring exists solely as the interactive skill-creator bundled skill (src/pythinker_code/skills/skill-creator/). Recurring workflows learned across sessions never crystallize into reusable skill packages.

**Verifier note.** Claim confirmed. memory/consolidation.py targets only the memory/user stores (InboxCandidate.target, approve path writes via ProjectMemoryStore); rg 'skill' over src/pythinker_code/memory/ and project_memory.py: zero functional hits. rg 'promote|crystalliz|instinct' over src *.py: only unrelated UI/shell matches (spinner words, markdown, background manager). Skill authoring exists only as interactive bundled skills (src/pythinker_code/skills/skill-creator/, plus agent-creator). No automated or suggested path from recurring memory content to a skill package.

**Adopt.** Extend the inbox candidate model with a target='skill' kind: when consolidation (heuristic now, LLM later) sees the same procedure recur across journal recaps, stage a proposed skill directory (SKILL.md draft) in the inbox; on approval, write it under the user skills root where existing discovery picks it up. Reuse the quality-gate checklist from the reference template inside the skill-creator prompt.

**Files.** `src/pythinker_code/memory/consolidation.py`, `src/pythinker_code/skills/skill-creator/SKILL.md`, `<ref>/memories/write/templates/memories/consolidation.md`

### `skills-hooks-memories/per-skill-enable-disable-rules-and-invocation-policy` — missing, M, medium

**Today.** No per-skill disable mechanism exists: config.py:945-960 only offers merge_all_available_skills and extra_skill_dirs; rg for disabled/disable over skill code finds nothing. parse_skill_text (skill/__init__.py:701-750) reads only name/description/type frontmatter — every discovered skill is always listed in the prompt, registered as a slash command, and readable via ReadSkill, including side-effectful ones (e.g. create-pr) the model can self-trigger.

**Verifier note.** Claim confirmed. Skill config surface is only merge_all_available_skills (config.py:945-953) and extra_skill_dirs (:954-957); rg for disable/exclude/blocklist/denylist/allowed_skills over skill/__init__.py and lockfile.py finds nothing. parse_skill_text (skill/__init__.py:701-750) reads only name/description/type frontmatter. Every discovered standard/flow skill is auto-registered as /skill:<name> at pythinkersoul.py:1259-1276 (only filters: type check at :1260 and name-collision skip at :1263-1268), flow skills additionally as flow commands (:1279-1297), and all are model-readable via the ReadSkill tool (tools/skill/__init__.py). No invocation policy of any kind.

**Adopt.** Add a [skills] config table with per-name/per-path enabled overrides applied after discovery, and honor a disable-model-invocation (or allow_implicit_invocation) frontmatter key: such skills stay out of PYTHINKER_SKILLS and ReadSkill but keep their /skill: slash command. Surface disabled skills in a /skills listing.

**Files.** `src/pythinker_code/skill/__init__.py`, `src/pythinker_code/config.py`, `<ref>/core-skills/src/config_rules.rs`, `<ref>/core-skills/src/model.rs`

### `skills-hooks-memories/permissionrequest-hook-event-programmatic-approval-decisions` — missing, M, medium

**Today.** hooks/config.py HookEventType has 13 events but no PermissionRequest; the approval path (soul/permission.py check_tool_call_allowed, soul/approval.py) runs before PreToolUse hooks with no hook integration, so users cannot script auto-approve/deny policies beyond the static allowlist.

**Verifier note.** Claim confirmed with one overstatement to trim. HookEventType (hooks/config.py:5-19) lists exactly 13 events; no PermissionRequest (rg over src+tests: zero hits; the ACP test matches are the unrelated ACP permission protocol). The permission gate check_tool_call_allowed (permission.py:412-426) runs at toolset.py:600-609 BEFORE the PreToolUse trigger (:614) and contains no hook integration; soul/approval.py has zero hook references. Nuance the analyst missed: auto-DENY is already scriptable — a PreToolUse hook can block via exit 2 or hookSpecificOutput.permissionDecision=='deny' (runner.py:66-91). What is genuinely missing is a hook in the approval path itself, i.e. programmatic auto-APPROVE / decision injection before the user prompt.

**Adopt.** Add a PermissionRequest event fired from the approval flow just before an interactive prompt, passing tool_name/tool_input/permission context; honor hook decisions allow→skip prompt, deny→reject with reason, no-output→fall through to the normal prompt. Keep it fail-open to the interactive prompt (never fail-open to allow).

**Files.** `src/pythinker_code/hooks/config.py`, `src/pythinker_code/soul/approval.py`, `<ref>/hooks/src/events/permission_request.rs`

### `skills-hooks-memories/skills-prompt-listing-context-budget-with-graceful-degradati` — missing, M, medium

**Today.** skill/__init__.py:354-389 format_skills_for_prompt renders every skill with full absolute path and full frontmatter description, unbounded; only the body-derived fallback description is capped (240 chars, line 693). soul/agent.py:259 injects the result verbatim into PYTHINKER_SKILLS. A user with many skills across brand+generic dirs (merge_all_available_skills) silently bloats every system prompt.

**Verifier note.** Claim confirmed. format_skills_for_prompt (skill/__init__.py:354-389) renders every skill with name, full Path, and full description, grouped by scope, with no count cap, char budget, or degradation. Only the body-derived fallback description is truncated (_DESCRIPTION_FALLBACK_MAX_LEN=240 at :693, _truncate at :760); frontmatter descriptions are used verbatim — the docstring's mention of a 1024-char spec cap is not enforced in parse_skill_text (:701-750). soul/agent.py:259 formats and :351 injects verbatim as PYTHINKER_SKILLS into system.md:265. No skills-budget work found in tasks/ logs either.

**Adopt.** Give format_skills_for_prompt a token budget derived from the model context window: cap per-skill descriptions proportionally when over budget, then fall back to name+path lines and an omitted-count note the model can see; emit a one-time UI warning when truncation occurs. Optionally add a roots-alias table ($PROJ/, $USER/) to shrink repeated path prefixes.

**Files.** `src/pythinker_code/skill/__init__.py`, `src/pythinker_code/soul/agent.py`, `<ref>/core-skills/src/render.rs`

### `tools-registry-codemode/hook-driven-tool-input-rewriting-pre-execution-hooks-can-mod` — partial, M, medium

**Today.** HookResult carries only action allow|block, reason, and additional_context (src/pythinker_code/hooks/runner.py:12-21); the PreToolUse path in PythinkerToolset (soul/toolset.py:617-636) can veto a call but never amend its arguments, so policy hooks cannot e.g. rewrite a command to add flags or redirect a path.

**Verifier note.** Claim survives as stated. HookResult carries only action allow|block, reason, stdout/stderr, exit_code, timed_out, additional_context. The JSON-output parser understands permissionDecision deny and additionalContext but has no updatedInput/modified-arguments channel; the PreToolUse path in toolset can only veto, never amend arguments.

**Adopt.** Extend the hook output JSON with an optional updated_input object; in _call_with_lifecycle, when present, re-validate it through the tool's pydantic params model and substitute before tool.call, logging the rewrite. Reject rewrites that fail validation as a hook error rather than executing ambiguous input.

**Files.** `src/pythinker_code/hooks/runner.py`, `src/pythinker_code/soul/toolset.py`, `<ref>/core/src/tools/registry.rs`

### `tools-registry-codemode/namespaced-tool-identity-with-deterministic-flat-naming-and-` — partial, M, medium

**Today.** MCP tools register under their raw server-side names; cross-server collisions are last-wins with a warning (_register_mcp_tools, src/pythinker_code/soul/toolset.py:354-376), and which server wins is nondeterministic because servers connect concurrently. A separate mcp__{server}__{tool} key scheme already exists in runtime.mcp_tools (toolset.py:983) but is not the model-facing name.

**Verifier note.** Claim survives as stated. MCPTool registers under the raw server-side name (name=mcp_tool.name); cross-server collisions are last-wins with a logged warning and the winner is nondeterministic (servers connect concurrently, acknowledged in a code comment); the mcp__{server}__{tool} key exists only in runtime.mcp_tools bookkeeping, not as the model-facing name.

**Adopt.** On collision (or always, behind a config flag), register the model-facing name as the existing mcp__server__tool flat form so both servers' tools stay addressable; update permission matchers and the dedup key derivation to accept the namespaced form. Deterministic and removes silent shadowing.

**Files.** `src/pythinker_code/soul/toolset.py`, `<ref>/tools/src/code_mode.rs`

### `mcp/agent-as-mcp-server-mode-expose-the-agent-itself-as-an-mcp-t` — partial, L, medium

**Today.** Partial (different protocols). Pythinker can run as an ACP server (acp/server.py, `pythinker acp`) and an experimental Wire server (cli/__init__.py --wire), which serve the same embed-the-agent role for editors, but there is no MCP-protocol server mode, so MCP-only clients (other agents, MCP-capable IDEs) cannot drive pythinker as a tool.

**Verifier note.** Claimed state is factually accurate. ACP server mode exists (src/pythinker_code/acp/server.py ACPServer; `pythinker acp` subcommand at cli/__init__.py:1556, plus deprecated --acp flag) and an experimental Wire server mode (--wire, UIMode 'wire', cli/__init__.py:174,525-529). No MCP-protocol server mode exists: cli/mcp.py has only add/remove/list/auth/reset-auth/test subcommands (no 'serve'), and rg for 'FastMCP(' as a server returns nothing. Whether 'partial' vs 'missing' is the right label depends on whether ACP/Wire count as the same capability, but the underlying facts as stated check out.

**Adopt.** Add a `pythinker mcp serve` stdio mode reusing the existing wire/ACP session plumbing: one 'run agent' tool whose call starts a session and streams progress notifications, with the session id echoed in structured_content for follow-up calls, and approvals auto-resolved per a non-interactive policy flag. Only worth doing if agent-to-agent embedding becomes a goal.

**Files.** `src/pythinker_code/acp/server.py`, `<ref>/mcp-server/src/tool_runner module`, `<ref>/mcp-server/src/lib.rs`

### `mcp/server-initiated-notification-handling-server-log-messages-p` — missing, L, medium

**Today.** Missing. Pythinker exits the client context after listing tools (_connect_server) and re-enters `async with self._client` per tool call (MCPTool.__call__), so no session persists between calls; no fastmcp message_handler/log_handler/progress_handler is registered anywhere (grep found zero hits). The tool/resource list is frozen at connect time; only child stderr is captured to a session log file.

**Verifier note.** Verdict stands as claimed. _connect_server uses `async with server_info.client as client` and exits after list_tools/list_resources/list_prompts; MCPTool.__call__ re-enters `async with self._client` per call, so no persistent session. fastmcp.Client is constructed with no message_handler/log_handler/progress_handler/elicitation_handler kwargs (toolset.py:1038); grep for those handler names across src returns zero hits. Inventory frozen at connect time. mcpext-2(a) 'live tools/list_changed' explicitly deferred in the progress log. Only child stderr is captured (_configure_mcp_client_stderr_log).

**Adopt.** Keep one persistent fastmcp client session per server (enter the context at connect, exit at cleanup — the close-timeout teardown already exists) and register fastmcp's log_handler/progress_handler/message_handler: route server logs to the pythinker logger with level mapping, and on tools/list_changed re-list and re-register that server's tools (with the conflict rules already in _register_mcp_tools). This also removes the per-call re-handshake latency for HTTP servers.

**Files.** `src/pythinker_code/soul/toolset.py`, `<ref>/rmcp-client/src/logging_client_handler.rs`, `<ref>/mcp/src/rmcp_client.rs`

### `multi-agent/best-of-n-parallel-attempts-on-one-task-with-comparison-and-` — missing, L, medium

**Today.** Missing. RunAgents (src/pythinker_code/tools/agent/__init__.py) fans out different child tasks; a 'judge' subagent type with a verify permission profile exists (src/pythinker_code/soul/permission.py:96) but there is no built-in mode that runs N attempts of the same prompt in isolated trees and compares/selects results.

**Verifier note.** Claim upheld. Also checked AgentLaunchSpec.variant (subagents/models.py:46) as a possible attempt-variant mechanism — it is persisted-only metadata (store.py:32,73) never set by the Agent tool, so not a best-of-N feature. The Implement->Review->Fix->Verify->Judge pipeline in agents/default/system.md is prompt guidance, not a built-in N-attempt compare/select mode.

**Adopt.** Add an attempts: int (1-4) option to RunAgents (or a BestOf tool) that clones one child spec N times with distinct codenames, requires worktree isolation, runs attempts in parallel, then auto-launches a judge child fed each attempt's diff+report to rank them — surfacing the ranking and per-attempt worktree paths for the user/orchestrator to apply. Depends on the isolation finding landing first.

**Files.** `src/pythinker_code/tools/agent/__init__.py`, `src/pythinker_code/subagents/usage.py`

### `multi-agent/data-driven-batch-job-fan-out-with-templated-instructions-an` — missing, L, medium

**Today.** Missing. RunAgents caps at 8 hand-written children with no data-driven templating, no per-item result schema, and no durable job record beyond individual background tasks (src/pythinker_code/tools/agent/__init__.py RunAgentsParams max_length=8; src/pythinker_code/background/store.py is per-task).

**Verifier note.** Claim upheld. Closest existing pieces: base_prompt shared-prefix on RunAgents (a fixed prepend, not per-item templating) and capacity-overflow handling that launches a fitting prefix and reports the rest as 'deferred' (tools/agent/__init__.py:704-713) — there is no durable job/queue record, no per-item result schema, and no template-over-dataset expansion.

**Adopt.** Add a RunAgentsOnRows tool: parse a CSV/JSONL, render an instruction template per row, run rows through the existing background agent pipeline under the session capacity semaphore with per-item timeout, give workers a ReportJobResult tool keyed by job_id/item_id, and write a job manifest plus output CSV under the session tasks dir for crash-safe resumption.

**Files.** `src/pythinker_code/tools/agent/__init__.py`, `src/pythinker_code/background/store.py`, `src/pythinker_code/background/manager.py`

### `observability-feedback/opt-in-local-raw-evidence-session-trace-bundle-with-offline-` — partial, L, medium

**Today.** No equivalent. Sessions persist message history JSONL (src/pythinker_code/session.py) and checkpoints, and an httpx recording client exists only for eval cassettes (src/pythinker_code/llm.py:139 _build_recording_http_client); there is no raw-event spine with payload refs, no model-visible vs runtime separation, and no offline reducer. Subagent metadata lives in per-agent meta.json (src/pythinker_code/subagents/store.py) without interaction-edge linkage.

**Verifier note.** Claimed 'missing' is wrong. Pythinker HAS a per-session raw-event spine, a model-visible vs runtime separation, an opt-in local export bundle, and offline reducers. Each session persists context.jsonl (model-visible message history) AND wire.jsonl (timestamped runtime event spine: TurnBegin/TurnEnd/StepBegin/StepRetry/ToolCall/ToolCallPart/ToolResult/ContentPart/ApprovalResponse/SubagentEvent...). Subagents get their own nested wire.jsonl under the parent session. `pythinker export` builds an opt-in ZIP (manifest.json system diagnostics, transcript.yaml reduction, all session files incl. subagents/, recent log files), and the vis app is an offline reducer that parses wire.jsonl into per-session timelines/summary stats and aggregate statistics, and accepts uploaded export ZIPs. What is genuinely absent: raw provider HTTP request/response payload capture with payload refs — the spine is post-parse wire events, and the httpx recording client only harvests rate-limit headers for the /usage panel (the analyst's 'eval cassettes' description of llm.py:139 is also wrong).

**Adopt.** Add an env-gated TraceWriter (e.g. PYTHINKER_TRACE_ROOT) that the soul, toolset, and subagent runner call best-effort: append seq-numbered JSONL events referencing payload files written first; record LLM request/response payloads, tool dispatch boundaries, and subagent spawn/result edges. Ship a `pythinker debug trace-reduce` CLI that replays the bundle into a state.json graph keyed by stable IDs. Keep all writes wrapped so tracing can never fail a session, and document the bundle as sensitive local-only data.

**Files.** `<ref>/rollout-trace/README.md`, `<ref>/rollout-trace/src/writer.rs`, `src/pythinker_code/session.py`, `src/pythinker_code/llm.py`, `src/pythinker_code/subagents/store.py`

### `patch-file-tools/shell-invoked-file-edit-interception-into-the-structured-app` — missing, L, medium

**Today.** src/pythinker_code/soul/permission.py classifies output redirection for gating (lines 507-514) and the memory-noted shlex tokenizer is blind to heredocs, so `cat <<EOF > file` style shell writes execute as opaque bash with no per-file diff, no create_file_restore_point snapshot, and coarse approval text; tools/shell has no edit-extraction layer.

**Verifier note.** Claim survives. Shell redirection/in-place-edit detection exists only as a coarse mutation classifier for permission gating; there is no extraction of shell-mediated file edits into per-file diffs, restore points, or structured approval display.

**Adopt.** Add a pre-execution inspector on the shell tool that detects simple heredoc/redirection write forms (a real bash parser e.g. tree-sitter-bash or bashlex, not shlex), extracts target path + content, and either (a) renders a proper diff block in the approval prompt and creates a restore point before running, or (b) returns a corrective error steering the model to WriteFile/StrReplaceFile. Start with the steer-to-tool variant (matches the reference's ImplicitInvocation pattern) as a cheap first step.

**Files.** `<ref>/apply-patch/src/invocation.rs`, `src/pythinker_code/soul/permission.py`, `src/pythinker_code/tools/shell/__init__.py`

### `persistence-resume/persisted-session-metadata-index-with-self-repairing-backfil` — partial, L, medium

**Today.** Partial. CLI Session.list/list_all re-scan every session directory and re-read wire heads to derive titles on every call (src/pythinker_code/session.py:272-331); the web store builds an index only as an in-memory TTL cache from a full disk scan, lost on restart, with limit/offset (not cursor) pagination (src/pythinker_code/web/store/sessions.py _build_sessions_index/_load_sessions_index_cached). Nothing is persisted, so launch-time listing cost grows linearly with history and titles/previews are recomputed repeatedly.

**Verifier note.** Claim confirmed as stated. There is an index abstraction (SessionIndexEntry) but it is built by a full disk scan and held only in module-global TTL caches (_sessions_cache/_sessions_index_cache, CACHE_TTL), lost on restart; nothing is ever persisted to disk (no json.dump/write of the index anywhere in web/store/sessions.py). Pagination is limit/offset list slicing, not cursor-based. CLI Session.list/list_all rescan every session dir and re-read wire heads via refresh() to derive titles on each call. The only 'self-repair' that exists is unrelated: legacy metadata.json->state.json migration (session_state.py _migrate_legacy_metadata) and torn-line tolerance in Session.is_empty.

**Adopt.** Persist an index (SQLite or a single index.jsonl) under the share dir keyed by session id with title, preview, updated_at, work_dir, archived state, and token count. Write-through from Session.save_state/refresh and fork/archive mutations; on startup run a flock-lease-guarded backfill that only scans sessions newer than a stored watermark and upserts. List paths read the index, drop/repair entries whose directories are missing, and fall back to today's full scan when the index is absent or corrupt (fail-open). Reuse web's SessionIndexEntry shape so web and TUI picker share one source.

**Files.** `src/pythinker_code/session.py`, `src/pythinker_code/web/store/sessions.py`, `<ref>/rollout/src/metadata.rs`, `<ref>/rollout/src/state_db.rs`, `<ref>/rollout/src/list.rs`

### `tools-registry-codemode/tools-as-code-orchestration-mode-script-cell-that-composes-n` — missing, L, medium

**Today.** No equivalent exists (rg for code mode / exec-cell / tool-script across src/pythinker_code is empty); every tool composition costs one model round-trip per call through PythinkerToolset.handle (src/pythinker_code/soul/toolset.py).

**Verifier note.** Claim survives. No code-mode/exec-cell/tool-script facility exists; searches for code_mode, exec_cell, script_cell, tool_script, RunCode/ExecuteCode across src/pythinker_code and packages/ return nothing relevant (only pythinker-review's run_code_review_pass, unrelated). The tools/ package contains no python-exec orchestration tool; every composition is one model round-trip through PythinkerToolset.handle.

**Adopt.** Opt-in RunToolScript tool: execute a model-authored Python snippet in a separate sandboxed interpreter process whose only capability is an RPC proxy back into PythinkerToolset (permission/approval checks still applied per nested call), with long scripts parked as background cells reusing the existing task store + TaskOutput wait path. Major token-efficiency win for MCP-heavy and data-shuttling work, but the sandbox boundary is the hard part in Python — keep it process-isolated, no ambient FS/network.

**Files.** `src/pythinker_code/soul/toolset.py`, `<ref>/code-mode/src/description.rs`, `<ref>/code-mode/src/service.rs`, `<ref>/tools/src/code_mode.rs`

## Tier 4 — low value (29 items)

### `config-features/config-json-schema-export-for-editor-ci-validation` — missing, S, low

**Today.** Missing. Config is Pydantic so model_json_schema() exists for free, but nothing exports or ships it (no json_schema usage found in src/pythinker_code).

**Verifier note.** Claim confirmed. Nothing exports or ships a JSON schema for the Config model; the only jsonschema code in the monorepo is pythinker_core tool-schema dereferencing, unrelated to config.

**Adopt.** Add a hidden CLI subcommand (or build step) that writes Config.model_json_schema() to a published schema file; document a taplo/even-better-toml association so ~/.pythinker/config.toml gets editor validation. Pairs with the unknown-key finding.

**Files.** `src/pythinker_code/config.py`, `<ref>/config/src/schema.rs`

### `config-features/configurable-project-root-markers` — missing, S, low

**Today.** Missing. _find_project_root in src/pythinker_code/config.py hardcodes .git and returns None otherwise, so non-git directories get no project/local config scope at all.

**Verifier note.** Claim confirmed. Root detection hardcodes .git everywhere; the config-scope variant returns None for non-git dirs so project/local scopes are skipped entirely, and no marker configuration exists.

**Adopt.** Add a `project_root_markers` list setting (default ['.git']) read from the user scope, consulted by _find_project_root; first ancestor containing any marker wins, falling back to None as today.

**Files.** `src/pythinker_code/config.py`, `<ref>/config/src/project_root_markers.rs`

### `config-features/legacy-key-aliasing-with-user-facing-deprecation-notices` — partial, S, low

**Today.** Partial. One ad-hoc AliasChoices (LoopControl.max_steps_per_turn) and a normalizing validator (FeedbackConfig.github_repo) exist in src/pythinker_code/config.py, but aliases are silent and there is no general mechanism or deprecation messaging when config keys get renamed.

**Verifier note.** Claim confirmed. Exactly one validation alias and one normalizing validator exist; both are silent, and there is no general rename/deprecation mechanism for config keys.

**Adopt.** Add a small (old_path -> new_path) alias table applied to the raw merged dict before validation, recording usages and printing 'X is deprecated, use Y' once at startup; gives a safe runway for future config renames.

**Files.** `src/pythinker_code/config.py`, `<ref>/config/src/key_aliases.rs`

### `context-mgmt/diff-based-mid-session-settings-environment-reinjection-agai` — partial, S, low

**Today.** Partial. Mode toggles are covered by dedicated injection providers with rearm semantics (src/pythinker_code/soul/dynamic_injections/auto_mode.py, plan_mode.py, goal_mode.py; provider lifecycle hooks on_context_compacted/on_auto_changed in soul/dynamic_injection.py:138-182), and injections are budgeted deterministically. There is no generic diffed environment/permission baseline, but pythinker's static-per-session system prompt plus fresh-session-on-model-switch makes most reference cases moot.

**Verifier note.** Claim confirmed as partial; the cited evidence is accurate (hook line numbers: on_context_compacted at dynamic_injection.py:153, on_auto_changed at :162).

**Adopt.** Low priority: if mid-session environment changes become possible (cwd change, --add-dir at runtime, approval-policy edits), add a small 'environment changed' injection provider that snapshots the relevant fields per turn and emits a one-line diff reminder when they change. Reuse the existing provider framework; no new architecture.

**Files.** `src/pythinker_code/soul/dynamic_injection.py`, `src/pythinker_code/soul/dynamic_injections/auto_mode.py`

### `context-mgmt/hardened-cross-session-prompt-input-history-file-byte-cap-so` — partial, S, low

**Today.** Partial. Per-workdir JSONL with O_APPEND single-write, 0600 enforcement, secret redaction, and consecutive-dup skip exists (src/pythinker_code/ui/shell/prompt.py:1526-1611, 3588-3615), but the file grows unbounded (no max-bytes/soft-cap trim) and there is no lock against concurrent pythinker instances on the same workdir.

**Verifier note.** Claim confirmed as partial. One softener: each entry is a single write through an O_APPEND fd, so concurrent-instance appends are mostly atomic at the POSIX level even without a lock — but the claim's core points (no max-bytes/soft-cap trim, no explicit lock) are factually right.

**Adopt.** After append, if file size exceeds a configurable cap (e.g. 1 MiB), rewrite to ~80% of the cap by dropping oldest lines via the existing tempfile+os.replace pattern from soul/context.py. Locking is optional given single-syscall appends under PIPE_BUF; skip unless interleaving is observed.

**Files.** `src/pythinker_code/ui/shell/prompt.py`

### `core-loop/turn-level-timing-telemetry-ttft-sampling-retry-counts-per-t` — partial, S, low

**Today.** Partial. Turn duration/step-count/stop-reason and per-LLM-call duration+token metrics exist (pythinkersoul.py _turn and _run_step_once via telemetry.metrics), but TTFT is not measured (no first-token timestamp in the stream path; rg ttft empty) and per-turn token deltas are not recorded (only cumulative usage).

**Verifier note.** Claim confirmed as stated, with one nuance: step retries ARE surfaced as StepRetry wire events (attempt number, max attempts, wait, error type/status) so the UI sees them, but they are not aggregated into a telemetry retry-count metric. TTFT is genuinely unmeasured (the only first-token timestamps are local TUI tokens-per-second sparkline math, not telemetry), and record_turn carries no token fields — token counters are per-LLM-call plus a cumulative session total only.

**Adopt.** Capture a first-content-part timestamp in the on_message_part path of _run_step_once and emit a ttft metric on the existing pythinker.llm span; snapshot cumulative_usage at _turn entry to record a per-turn token delta attribute on the pythinker.turn span.

**Files.** `src/pythinker_code/soul/pythinkersoul.py`, `src/pythinker_code/telemetry/metrics.py`

### `mcp/tool-metadata-trust-hygiene-strip-privileged-meta-keys-from-` — missing, S, low

**Today.** Missing, but mostly inapplicable: pythinker has no first-party connector ecosystem to spoof, and it already prepends its own trusted framing to every MCP tool description (MCPTool.__init__ in soul/toolset.py). Tool _meta is otherwise ignored entirely.

**Verifier note.** Verdict stands as claimed. No _meta handling anywhere (rg '_meta' in toolset.py/mcp_resource returns nothing); tool annotations/visibility metadata are ignored — MCPTool consumes only name, description, and inputSchema. The trusted framing prefix the claim mentions is confirmed: MCPTool.__init__ prepends pythinker's own 'This is an MCP tool from the already-connected MCP server `X`...' text before the server-supplied description.

**Adopt.** Low priority: if tool _meta ever starts influencing behavior or rendering, honor the visibility convention (hide tools whose meta marks them non-model-visible) and ignore/strip unrecognized privileged-looking meta keys rather than forwarding them.

**Files.** `src/pythinker_code/soul/toolset.py`, `<ref>/mcp/src/rmcp_client.rs`

### `multi-agent/collaboration-mode-presets-bundling-model-reasoning-effort-i` — partial, S, low

**Today.** Largely present in different shape: plan mode is a first-class toggle with dynamic injection and permission gating (src/pythinker_code/soul/dynamic_injections/plan_mode.py, src/pythinker_code/app.py:381-383), execution profiles gate tools and subagent types per mode (src/pythinker_code/execution_profiles.py), and goal mode covers autonomous execution. The only delta is that modes do not bundle a model/reasoning-effort switch.

**Verifier note.** Claim upheld with one refinement: the 'modes do not bundle a model/reasoning-effort switch' delta is true only for session-level modes (plan/auto/goal — no model/effort refs in soul/dynamic_injections/plan_mode.py, auto_mode.py, goal_mode.py, or the plan-mode toggle path). Per-SUBAGENT-TYPE presets already bundle model + instructions + tool policy: AgentTypeDefinition.default_model (subagents/models.py:33), markdown agent frontmatter `model:` with validation fallback (subagents/discovery.py:128,186-192), and AgentLaunchSpec carries thinking/thinking_effort applied at build time (subagents/models.py:44-45; subagents/builder.py:26-27,44). So model+effort bundling exists in the subagent preset layer, just not as session collaboration modes.

**Adopt.** Optionally let plan mode (and execution profiles) carry a model/thinking-effort override applied on mode entry and restored on exit — a small config field plus a swap in the mode toggle path. Low urgency; current modes already gate behavior correctly.

**Files.** `src/pythinker_code/soul/dynamic_injections/plan_mode.py`, `src/pythinker_code/execution_profiles.py`

### `multi-agent/interrupted-turn-guidance-marker-in-child-history` — partial, S, low

**Today.** Partial. Pending tool calls get synthetic 'Tool call interrupted by user.' results on interrupt (src/pythinker_code/soul/pythinkersoul.py:1768-1784), which makes interruption visible at tool-result granularity, but there is no turn-level guidance message; mostly relevant once mid-run steering (interrupt+redirect) exists for children.

**Verifier note.** Claim upheld; minor line-ref drift only — the synthetic-marker block sits at src/pythinker_code/soul/pythinkersoul.py:1801-1817 in the current (locally modified) working tree rather than 1768-1784. Mechanism matches the claim.

**Adopt.** When the steering tool interrupts a child, append a short system-reminder style marker ('previous turn was interrupted by the orchestrator; new instructions follow') ahead of the injected message so the child does not treat the truncation as its own failure. Piggyback on the existing synthetic-marker write path.

**Files.** `src/pythinker_code/soul/pythinkersoul.py`, `src/pythinker_code/subagents/runner.py`

### `multi-agent/persisted-parent-child-spawn-edge-graph-with-open-closed-lif` — partial, S, low

**Today.** Partial. parent_agent_id is persisted in each AgentLaunchSpec (src/pythinker_code/subagents/models.py:47, store.py:33,74) and crash recovery reconciles instance statuses (src/pythinker_code/background/manager.py recover()/reconcile_stale_agent_record), but there is no edge-status concept, no descendant/tree query, and the graph is flat anyway because only root may spawn.

**Verifier note.** Claim upheld as stated, including the flat-graph point: nested spawning is hard-blocked ('Subagents cannot launch other subagents.', src/pythinker_code/tools/agent/__init__.py:264), so persisted parent_agent_id edges are depth-1. SubagentStore.list_instances (subagents/store.py:181) is a flat list with no parent filter or tree/descendant query, and there is no edge open/closed status distinct from instance status.

**Adopt.** Low priority while depth is capped at 1. If deeper delegation ever lands, add list_children(parent_agent_id, status) to SubagentStore using the existing meta files, plus an open/closed flag flipped on terminal reconciliation, keeping deterministic ordering by created_at then id.

**Files.** `src/pythinker_code/subagents/store.py`, `src/pythinker_code/subagents/models.py`

### `observability-feedback/metric-tag-hygiene-validation-and-bounded-cardinality-normal` — partial, S, low

**Today.** Partial. The sink asserts primitive-only attributes (src/pythinker_code/telemetry/sink.py:_assert_primitive), model names are bucketed into a bounded family dimension (telemetry/metrics.py classify_model_family), and resource attrs carry version/ui_mode/device. But wire/ACP client names/versions pass through verbatim with validation explicitly deferred to the backend (telemetry/__init__.py set_client_info), and no charset/allowlist normalization exists for free-form tag values.

**Verifier note.** Claim stands; all cited details verified. _assert_primitive enforces primitive-only event properties/context and the sink drops schema violations without retry; classify_model_family buckets model names into a bounded family dimension; set_client_info passes wire/ACP client name/version through verbatim with an explicit comment deferring validation/normalization to the backend; no charset/allowlist normalization for free-form tag values exists (only Sentry path/message scrubbing and error-ring message redaction, which are privacy redaction, not cardinality hygiene).

**Adopt.** Add a small sanitize_tag_value helper (charset filter + length cap) plus an allowlist mapping for client names (known editors -> canonical value, else 'other') applied in set_client_info and track_session_started_once; optionally assert tag-value length in _assert_primitive.

**Files.** `<ref>/otel/src/metrics/validation.rs`, `<ref>/otel/src/metrics/tags.rs`, `src/pythinker_code/telemetry/sink.py`, `src/pythinker_code/telemetry/__init__.py`

### `prompts-instructions/configurable-personality-presets-injected-into-a-prompt-slot` — partial, S, low

**Today.** The slot mechanism exists — system.md has ${ROLE_ADDITIONAL} wired through agent.yaml system_prompt_args — but it ships empty; there are no curated personality presets and no config key or slash command to select one. Communication style is fixed in system.md §8 (src/pythinker_code/agents/default/system.md lines 18, 183-189; agent.yaml lines 5-6).

**Verifier note.** Claim survives with one nuance: the ${ROLE_ADDITIONAL} slot is not dormant — it is the active persona slot for every subagent yaml (plan.yaml, review.yaml, coder.yaml, etc. all fill it with role personas) and is fillable by user-authored agents via --agent-file (documented in skills/customize-pythinker/SKILL.md and skills/agent-creator/SKILL.md). What is genuinely absent is curated end-user personality presets and any runtime selector: the root default agent ships ROLE_ADDITIONAL: "" (agents/default/agent.yaml line 6), the only builtin agent variants are --agent {default,ask,debug,okabe} (cli/__init__.py lines 565-571, 745-755) and okabe/agent.yaml is a tool-subset only with no persona text, and no config key or slash command selects a personality (rg 'personality|persona|output style' across src; config.py has no persona/system_prompt key; soul/slash.py registers init/recap/compact/clear/yolo/auto/plan/goal/learn/best-practices/add-dir/export/import only).

**Adopt.** Ship 2-3 curated personality overlay files (e.g. pragmatic default, supportive/pairing, terse) with Values/Tone/Escalation structure; add a config key (and optionally a slash command) that fills ROLE_ADDITIONAL from the chosen preset. Default stays empty so existing prompt pins/tests are unaffected.

**Files.** `src/pythinker_code/agents/default/agent.yaml`, `src/pythinker_code/agents/default/system.md`

### `prompts-instructions/frontend-design-anti-genericism-guidance` — missing, S, low

**Today.** Absent: no frontend/design guidance in system.md, best_practices.md, or any subagent spec (rg for frontend/design/UI across src/pythinker_code/agents and prompts finds only unrelated 'Design and implementation' engineering headings).

**Verifier note.** Claim survives. No frontend/design-quality guidance exists in any prompt surface.

**Adopt.** Add a short conditional 'Frontend work' subsection to system.md §6 or best_practices.md (5-7 rules + the existing-design-system exception), or ship it as a built-in skill the model loads for UI tasks to avoid bloating the cached prompt for a review-first product.

**Files.** `src/pythinker_code/agents/default/system.md`, `src/pythinker_code/prompts/best_practices.md`

### `protocol-headless/exported-protocol-schemas-for-external-client-codegen` — partial, S, low

**Today.** Partial. Wire protocol types are pydantic models with a versioned initialize handshake (src/pythinker_code/wire/jsonrpc.py protocol_version + ClientCapabilities; types.py WireMessageEnvelope with a v1 back-compat alias), and an e2e handshake snapshot pins the slash-command list (tests_e2e), but no JSON Schema fixtures are generated/checked in for wire or ACP types — external clients must read Python source.

**Verifier note.** Claim confirmed with one naming nit. Versioned handshake exists: src/pythinker_code/wire/jsonrpc.py:85 ClientCapabilities, :109-113 InitializeParams.protocol_version. WireMessageEnvelope exists (src/pythinker_code/wire/types.py:722-749, untagged {type, payload}); the 'v1 back-compat alias' the claim cites is actually the _compat_legacy_fields validator (types.py:304-310) normalizing task_tool_call_id -> parent_tool_call_id — there is no literal 'v1' tag. The e2e handshake inline-snapshot pin is real (tests_e2e/test_wire_protocol.py:test_initialize_handshake, snapshot includes slash_commands). The core gap stands: no JSON Schema fixtures are generated or checked in for wire/ACP types — find for *.schema.json hits only external reference agent_x (the vendored upstream clone, not pythinker), and rg for model_json_schema across src/tests/tests_e2e/docs returns nothing.

**Adopt.** Add a small generator (make target) that dumps model_json_schema() for the WireMessage envelope union and JSON-RPC message types into a checked-in schema/ dir, plus a snapshot test that regeneration is clean — giving wire clients a codegen artifact and CI drift detection for protocol changes.

**Files.** `src/pythinker_code/wire/types.py`, `src/pythinker_code/wire/jsonrpc.py`, `<ref>/app-server-protocol/src/schema_fixtures.rs`

### `protocol-headless/version-control-trust-gate-before-unattended-runs` — partial, S, low

**Today.** Partial. Pythinker runs --print anywhere; the mitigation is per-session safe_mode defaulting to True (src/pythinker_code/session_state.py, soul/approval.py) which fail-closes approval-required actions, but auto-approvable edits in a non-VCS directory remain unrecoverable; file_restore.py provides checkpoint-based undo which softens this further. No git-repo check exists in cli/__init__.py or ui/print.

**Verifier note.** Every fact in the claim verified; could not refute. There is no VCS-awareness anywhere in the run gate: the only .git checks in cli/__init__.py (lines 187-197, 218-225) walk to the repo root for MCP-config discovery, not trust gating; is_git_repo exists only in web/models.py for the web UI. Mitigations are as claimed: safe_mode defaults True (src/pythinker_code/session_state.py:23) and fail-closes approval-required actions (src/pythinker_code/soul/approval.py:226-245), and file_restore.py implements per-session FileRestorePoint checkpoints (file_restore_points dir). Caveat on grading: the named capability (a version-control check) is absent in any form — 'partial' is defensible only under the claim's explicit framing that safe_mode/file_restore partially cover the same risk; under a literal reading the verdict would be 'missing'.

**Adopt.** On --print (and --auto/--yolo) startup, detect absence of a git repo at work_dir and emit a prominent stderr warning naming the risk and the file-restore checkpoint fallback; optionally a config knob to escalate the warning to a refusal for unattended profiles. Avoid hard-blocking by default to preserve existing UX.

**Files.** `src/pythinker_code/cli/__init__.py`, `src/pythinker_code/session_state.py`, `<ref>/exec/src/lib.rs`

### `review-mode/review-mode-ui-state-signaling-banner-token-usage-snapshot-r` — partial, S, low

**Today.** Subagent activity is surfaced through the task browser/status feed and per-subagent usage accounting (src/pythinker_code/ui/shell/task_browser.py, src/pythinker_code/subagents/usage.py), but there is no dedicated review-mode banner or usage snapshot/restore — acceptable because pythinker reviews are ordinary subagent runs rather than a modal takeover.

**Verifier note.** Claim CONFIRMED factually. Cited surfaces exist (task_browser.py status/preview feed; subagents/usage.py per-subagent accumulate_usage/format_usage_lines/summarize_batch). No review-mode banner: statusline segments are cwd/git/model/context/tokens/effort (statusline.py line1/line2 segment sets at L97-101, effort badge L265-279, git badge L282+); prompt.py carries a plan-mode status indicator but nothing review-specific. No usage snapshot/restore mechanism found (no usage_snapshot/restore_usage/snapshot_usage symbols anywhere in src).

**Adopt.** Only worth doing as part of the /review command: show a transient 'review in progress — <hint>' status line scoped to the dispatched review run, reusing the existing status feed. Skip token snapshotting; pythinker already accounts subagent usage separately.

**Files.** `src/pythinker_code/ui/shell/task_browser.py`, `src/pythinker_code/subagents/usage.py`

### `skills-hooks-memories/explicit-inline-skill-mention-resolution-with-ambiguity-guar` — partial, S, low

**Today.** Pythinker resolves /skill:<name> slash commands at message start (soul/pythinkersoul.py:1259-1370) and handles mid-message /command references via agent-mediated guidance (soul/dynamic_injections/inline_commands.py) — a deliberate repo decision (mechanical mid-message splitting was proven to discard the surrounding task). No $-style mention sigil or pre-turn body injection for inline references.

**Verifier note.** Claim confirmed as stated. /skill:<name> resolves as a message-start slash command (SKILL_COMMAND_PREFIX at pythinkersoul.py:136, registration :1259-1276, active-skill persistence :1337-1346). Mid-message references are handled agent-mediated via InlineCommandReminderProvider (soul/dynamic_injections/inline_commands.py: _TOKEN_RE + _REMINDER_TEMPLATE instructing ReadSkill-and-apply), wired at pythinkersoul.py:448, reinforced by system.md:128 — a deliberate design (mechanical splitting was rejected; see tests/core and repo memory). rg for $-style mention sigils or pre-turn body injection of skill content: nothing found. Verdict 'partial' is right.

**Adopt.** Keep agent mediation as the execution path, but borrow the detection rigor: extend inline_commands.py to also recognize skill names referenced mid-message, with the env-var blocklist and only-when-unambiguous name matching, and have the injected guidance name the exact matched skill (path included) so the agent reliably reads it. Do not auto-split or auto-inject bodies.

**Files.** `src/pythinker_code/soul/dynamic_injections/inline_commands.py`, `src/pythinker_code/soul/pythinkersoul.py`, `<ref>/core-skills/src/injection.rs`

### `tools-registry-codemode/cancellation-contract-with-teardown-aware-standardized-abort` — partial, S, low

**Today.** Interruption cancels tool futures; pythinkersoul.py (~1787-1800) preserves real outputs of already-completed calls and synthesizes interrupted markers only for pending ones, and Shell kills its child inside its CancelledError handler (src/pythinker_code/tools/shell/__init__.py:381-384). But teardown-waiting is per-tool convention rather than a registry contract, and abort texts are ad hoc without elapsed-time info.

**Verifier note.** Claim survives as stated. On interrupt, pythinkersoul keeps real outputs of completed calls and synthesizes a static 'Tool call interrupted by user.' ToolRuntimeError for pending ones (no elapsed-time info), with shielded context writes so no unanswered tool_calls persist. Per-tool teardown (Shell kill-on-CancelledError, grep_local, agent) is convention, not a registry contract; abort texts are ad hoc ('Tool call interrupted by user.' vs background 'Interrupted by user').

**Adopt.** Add an optional waits_for_cancellation attribute honored by the interrupt path (shield the tool task and await it with a bounded timeout before substituting the marker) and standardize the aborted-result text to include wall time. Small change to the CancelledError branch in pythinkersoul plus the marker constant.

**Files.** `src/pythinker_code/soul/pythinkersoul.py`, `src/pythinker_code/tools/shell/__init__.py`, `<ref>/core/src/tools/parallel.rs`

### `config-features/nested-project-config-layers-from-project-root-down-to-cwd` — missing, M, low

**Today.** Missing. _load_scoped in src/pythinker_code/config.py reads exactly one project file at the .git root (.pythinker/config.toml + config.local.toml); subdirectory .pythinker dirs are ignored. Precedent exists elsewhere: AGENTS.md context already merges root->cwd.

**Verifier note.** Claim confirmed. Exactly one project config file pair is read at the .git root; subdirectory .pythinker/ configs are ignored. The cited AGENTS.md root->cwd precedent is real.

**Adopt.** Walk cwd ancestors up to the project root collecting .pythinker/config.toml files, merge root-first so deeper dirs override, applying the same scope-lock/sanitize guard and provenance label per directory. Should land after (or together with) trust gating since it widens the repo-controlled surface.

**Files.** `src/pythinker_code/config.py`, `<ref>/config/src/loader/mod.rs`

### `context-mgmt/model-initiated-fresh-context-window-tool-requested-history-` — missing, M, low

**Today.** Missing. /clear exists as a user command (src/pythinker_code/soul/slash.py clear) and checkpoints exist (soul/context.py), but the model has no way to declare 'this exploration is done, give me a clean window'; nothing in src/pythinker_code/tools/ or soul/toolset.py exposes a reset.

**Verifier note.** Claim confirmed. Adjacent-but-different capability that exists: subagent delegation (tools/agent) gives the model fresh child windows for delegated work, but there is no way for the model to reset ITS OWN history.

**Adopt.** Add a tool that sets a 'fresh-window requested' flag; after the current step's tool results are appended, rebuild context as system prompt + a model-authored handoff note (the tool's required argument) + compaction-restore reminders, reusing the clear+rebuild primitive from compact_context. Gate behind config since it is destructive.

**Files.** `src/pythinker_code/soul/slash.py`, `src/pythinker_code/soul/context.py`

### `exec-safety/per-host-network-rules-with-deny-tier-and-justification` — partial, M, low

**Today.** Partial. First-class web tools enforce a configurable host allowlist plus an SSRF guard with per-redirect re-validation (src/pythinker_code/tools/web/fetch.py, _allowlist.py, config.py web.allowed_domains); shell-level network access is binary — _NETWORK_COMMANDS blocked under restricted profiles, unrestricted otherwise (src/pythinker_code/soul/permission.py:166-178) — with no deny-with-justification tier and no per-host control for shell commands.

**Verifier note.** Claim confirmed. Web tools have per-host control: config web.allowed_domains (config.py:567-593 with hostname validation), label-aware subdomain matching in tools/web/_allowlist.py (host_in_allowlist; None/empty = unrestricted for these tools), and fetch.py's SSRF guard with manual redirect-following that re-validates every hop against both the SSRF check and the allowlist (_get_revalidating_redirects, fetch.py:93-109; private/link-local/multicast/reserved blocked at :61). Shell-level network is binary exactly as claimed: _NETWORK_COMMANDS (permission.py:166-178) is blocked only under restricted (no-shell-mutation) profiles via shell_mutation_reason (:536) inside check_shell_command_allowed (:341-354); unrestricted profiles get normal approval with no per-host rules and no deny-with-justification tier.

**Adopt.** Extend web config with deny entries carrying justifications (surfaced in the ToolError so the model learns why), and reuse the same host rules to scope obvious shell network commands (curl/wget URL arguments are parseable from the existing token stream) in 'ask' profiles. Full enforcement for arbitrary shell programs requires the sandbox/proxy layer and should ride that finding instead.

**Files.** `src/pythinker_code/tools/web/fetch.py`, `src/pythinker_code/tools/web/_allowlist.py`, `src/pythinker_code/config.py`

### `exec-safety/semantic-command-summarization-for-approval-history-display` — missing, M, low

**Today.** Missing. Approval prompts and transcripts show the raw command string via ShellDisplayBlock (src/pythinker_code/tools/display.py:31, src/pythinker_code/tools/shell/__init__.py:158-163); no semantic classification exists (grep for parsed/summarize found only unrelated session-recap code).

**Verifier note.** Claim confirmed, with one minor nuance. Approval prompts carry the raw command via ShellDisplayBlock(language, command) (display.py:31-36; shell/__init__.py:158-164 foreground, :271-276 background); no parser-derived semantic summary exists. Nuance worth recording: background tasks accept a model-supplied 'description' param (shell/__init__.py:65-71, default auto-filled) surfaced in BackgroundTaskDisplayBlock/task views — that is model-authored free text for task listings, not semantic classification, and the approval prompt still shows the raw command.

**Adopt.** A small classifier over the already-tokenized segments (permission.py's tokenizer) mapping common commands to one-line intents ('Search for TODO in src/', 'Read file X', 'List files') attached to ShellDisplayBlock as an optional subtitle; keep raw command primary. Low safety impact for a developer audience, so only worth doing opportunistically.

**Files.** `src/pythinker_code/tools/display.py`, `<ref>/shell-command/src/parse_command.rs`

### `mcp/cached-tool-list-snapshots-served-while-a-server-is-still-co` — missing, M, low

**Today.** Missing. MCP tools only become visible to the model after the background connect finishes (_register_mcp_tools in soul/toolset.py); on the first turn of every session the model plans without knowledge of MCP tools that will appear seconds later.

**Verifier note.** The capability itself (a persisted/cached tool list served during connect) is indeed absent — no MCP tool-list cache exists anywhere (rg -i cache in toolset.py: zero hits; tools register only in _register_mcp_tools after connect). BUT the claim's stated failure mode is FALSE: the model never plans without MCP tools, because PythinkerSoul._agent_loop calls start_background_mcp_loading() and then AWAITS wait_for_background_mcp_loading() before step 1 of every turn (emitting MCPLoadingBegin/End to the UI while blocking). The real gap a cache would address in pythinker is first-turn latency / hung-connect blocking, not first-turn tool blindness.

**Adopt.** Persist each server's last successful tool list (name/description/schema) under the share dir; at startup register provisional MCPTool entries from the snapshot whose __call__ awaits server readiness (wait-for-connect with timeout) before invoking, then reconcile/replace once the live list arrives. Invalidate the snapshot when mcp.json for that server changes.

**Files.** `src/pythinker_code/soul/toolset.py`, `<ref>/mcp/src/rmcp_client.rs`

### `observability-feedback/cross-process-trace-context-propagation-and-user-configurabl` — partial, M, low

**Today.** Partial. The sampler is ParentBased anticipating upstream wire/ACP parents (src/pythinker_code/telemetry/otel.py:147), but no traceparent extraction/injection exists anywhere (rg 'traceparent|tracestate' over src/pythinker_code is empty); subagents run in-process so their spans already nest. Endpoint/token/sample-rate are env-overridable (telemetry/config.py) but custom span attributes are not.

**Verifier note.** Claim stands as 'partial'; core facts verified: ParentBased(TraceIdRatioBased) sampler exists, and no traceparent/tracestate extraction or injection exists anywhere in src/pythinker_code (the only context.attach is local span nesting in otel.start_span). Endpoint/token/sample-rate are env-overridable as claimed. One nuance on 'custom span attributes are not [configurable]': resources are built with Resource.create(), and the OTel SDK merges the standard OTEL_RESOURCE_ATTRIBUTES env var there, so users can inject custom resource-level attributes onto all spans via standard OTel env — incidental SDK behavior, not a pythinker feature, and there is no pythinker-specific span-attribute config.

**Adopt.** Accept an optional traceparent field in the wire/ACP initialize message and attach it as the parent context for turn spans of that session; expose a PYTHINKER_OTEL_SPAN_ATTRIBUTES env (k=v,k=v) merged into the Resource. Low urgency until external embedders ask for joined traces.

**Files.** `<ref>/otel/src/trace_context.rs`, `src/pythinker_code/telemetry/otel.py`, `src/pythinker_code/telemetry/config.py`

### `patch-file-tools/committed-change-delta-with-exactness-tracking-for-failure-s` — partial, M, low

**Today.** src/pythinker_code/file_restore.py snapshots the pre-image before each WriteFile/StrReplaceFile mutation and /restore (ui/shell/slash.py:1652-1798) replays it, covering the main undo need; per-call edits are atomic-per-file because replace.py validates the whole batch in memory first. But results don't report old/overwritten content, there is no aggregated session 'what changed this turn' delta, and exactness of failed writes is untracked.

**Verifier note.** Verdict 'partial' is right, but the claimed state overstates one gap: an aggregated per-turn 'what changed this turn' summary DOES exist at path/count granularity — the TUI turn recap collects every DiffDisplayBlock path from tool results and reports 'N files changed'; subagent blocks similarly aggregate changed-file lists. What is genuinely absent: content-level deltas (old/overwritten text is not in model-facing tool result messages — WriteFile reports only byte size, StrReplaceFile only replacement counts; diffs are UI display blocks), and any exactness/failure tracking on deltas.

**Adopt.** Extend tool results (or a session-side ledger keyed off restore points) with the applied-change record {path, op, old_digest, new_digest, overwritten} and an exact/inexact bit set when a write raises after partially executing; surface it in session recap and /restore listings so multi-file work and failures are precisely reconstructable. Low urgency while edits stay one-file-per-call.

**Files.** `<ref>/apply-patch/src/lib.rs`, `src/pythinker_code/file_restore.py`, `src/pythinker_code/ui/shell/slash.py`

### `persistence-resume/cold-session-compression-with-transparent-readers-and-atomic` — missing, M, low

**Today.** Missing. Pythinker resolves disk growth by deleting archived sessions older than 30 days outright (src/pythinker_code/session_cleanup.py sweep_old_sessions), making retention vs disk a hard tradeoff; context/wire files are always plain JSONL (src/pythinker_code/session.py, src/pythinker_code/wire/file.py).

**Verifier note.** Claim confirmed. Disk growth is handled by deletion, not compression: sweep_old_sessions removes archived session dirs older than max_age_days (default doc says 30-day retention) via shutil.rmtree, and only archived=True sessions are eligible. context.jsonl/wire.jsonl are always plain JSONL; rg for gzip/zstd/lzma/bz2/zlib across src finds only FastAPI GZipMiddleware (web/vis HTTP responses) and archive file-extension lists in tools/file — nothing compresses session storage, and there is no compressed-transparent reader or materialize-on-append path.

**Adopt.** Optional startup job (after the existing sweep) gzip-compresses context.jsonl/wire.jsonl of archived or long-idle sessions, guarded by a marker file; Session.find/Context.restore and the picker's title derivation transparently open .gz, and resuming decompresses atomically (mkstemp + os.replace, never clobbering an existing plain file) before append. Lets the retention sweep keep sessions resumable far longer at low disk cost. Low priority given recall + cleanup already manage history.

**Files.** `src/pythinker_code/session_cleanup.py`, `src/pythinker_code/session.py`, `<ref>/rollout/src/compression.rs`

### `protocol-headless/ephemeral-no-persistence-headless-runs` — missing, M, low

**Today.** Missing. Every run creates a persisted Session with context/wire files (src/pythinker_code/session.py is always disk-backed; rg for ephemeral/no-save in src/pythinker_code finds only unrelated hits); cleanup relies on empty-session deletion and session_cleanup.py pruning rather than an opt-out.

**Verifier note.** Claim confirmed for agent runs; could not refute. Session.create always mkdirs and persists under the session dir (src/pythinker_code/session.py:88-91 dir property mkdir, 188-203 create with metadata write), and the --print path in cli/__init__.py always constructs a Session before run_print (used for SessionStart/SessionEnd hooks at lines ~995-1060). No --ephemeral/--no-save flag exists on the main CLI (flag inventory lines 332-638). Cleanup is indeed deletion-based: _delete_empty_session (cli/__init__.py:~1119) plus session_cleanup.py sweeps. Nearest miss found while trying to refute: `pythinker review diff --no-save` / `pythinker secscan diff --no-save` (referenced in agents/default/code_reviewer.yaml:59 and security_reviewer.yaml:33, implemented in the delegated pythinker_review package per src/pythinker_code/cli/review.py) — but that is the standalone review pipeline's run-state, not a headless agent session, so it does not overturn the verdict.

**Adopt.** Add --ephemeral for print mode: create the session under a temp dir (or a null wire-file backend) and delete it in _post_run regardless of exit code, skipping metadata last_session_id updates and journal recap. Useful for CI fan-out runs that would otherwise pollute session listings and persist prompt contents containing secrets.

**Files.** `src/pythinker_code/session.py`, `src/pythinker_code/cli/__init__.py`, `<ref>/exec/src/cli.rs`

### `config-features/machine-level-config-plus-admin-constraint-requirements-laye` — missing, L, low

**Today.** Missing. Pythinker has no layer below user scope and no value-set constraints; SCOPE_LOCKED_PATHS only restricts which scope may set a key, not what values are allowed (src/pythinker_code/config.py).

**Verifier note.** Claim confirmed. Scope chain is user -> project -> local -> env only; no system/machine layer (no /etc or platform-wide path) and no value-set constraint mechanism — SCOPE_LOCKED_PATHS only restricts which scope may set a key.

**Adopt.** Only worth it for shared/CI machines: an optional /etc/pythinker/config.toml lowest-precedence scope plus a tiny requirements file that pins values like default_yolo=false or an approval-policy allowlist, rejecting overrides with an error naming the requirements file. Defer unless team/enterprise deployment becomes a goal.

**Files.** `src/pythinker_code/config.py`, `<ref>/config/src/config_requirements.rs`, `<ref>/config/src/constraint.rs`

### `patch-file-tools/streaming-edit-argument-parser-for-live-diff-preview` — partial, L, low

**Today.** rg over src/pythinker_code finds no partial tool-argument (input-json-delta) handling; diffs are computed only after the full tool call arrives, at approval time (utils/diff.py build_diff_blocks), so long writes render nothing until complete.

**Verifier note.** Claimed 'missing' is wrong on its core premise: pythinker DOES have partial tool-argument streaming parsing. streamingjson.Lexer is fed argument deltas as they stream and the parsed partial JSON live-updates the tool card's key argument (e.g. file path, command) in both the TUI and ACP frontends. What is missing is only the last mile: streamed old/new content is not turned into a live diff preview — build_diff_blocks runs after the full call arrives, at approval time. Correct verdict: partial.

**Adopt.** If/when the wire layer exposes streaming tool-arg deltas, add an incremental JSON-prefix parser for WriteFile/StrReplaceFile args that extracts path and growing content to render a live 'writing path (+N lines)' preview in the worklog. Requires wire-level plumbing first; cosmetic payoff only.

**Files.** `<ref>/apply-patch/src/streaming_parser.rs`, `src/pythinker_code/utils/diff.py`

## Refuted claims — do NOT implement (already covered)

- `core-loop/review-mode-with-machine-parsable-structured-findings`: Claim REFUTED on its core assertion. Pythinker DOES have a machine-parsable structured findings schema parsed with fallback: a fenced ```report JSON block (title, severity from the fixed critical|high|medium|low|info set, location 'path:line-range', body) defined as a base contract in agents/default

- `observability-feedback/startup-phase-duration-telemetry`: Claim REFUTED. 'No boot timing reaches telemetry' is factually wrong: PythinkerCLI.create() times the boot end-to-end and per phase, then emits track('startup_perf', duration_ms=..., config_ms=..., init_ms=..., mcp_ms=...) at the end of startup. The phase decomposition (config load / runtime init / 

- `tools-registry-codemode/streamed-tool-argument-diff-consumers-live-ui-rendering-from`: REFUTED — the TUI does have a per-tool partial-argument renderer. _live_view.append_tool_call_part feeds each ToolCallPart.arguments_part into _ToolCallBlock.append_args_part, which runs a streaming-JSON repair lexer (streamingjson.Lexer), live-updates the row's argument summary (_extract_worklog_ar

## Cluster design summaries (reference architecture, for orientation)

### config-features

The reference harness builds configuration as an ordered stack of layers (machine/system -> managed -> base user -> named profile overlay -> project layers from root to cwd -> per-key CLI/session overrides), each carried as a ConfigLayerEntry with a sha256 content fingerprint, per-field origin tracking, and an optional disabled_reason so layers can be loaded-but-inactive. Project-scope config is security-gated: a persisted per-project trust map decides whether repo-controlled config/hooks/exec-policies apply (untrusted layers are shown disabled with an actionable reason, never executed), and a denylist of sensitive keys is sanitized out of project config with a startup warning rather than a fatal error. A separate admin "requirements" layer expresses constraints (allowed value sets via Constrained<T> with source attribution) rather than values. Features are governed by a single registry of FeatureSpec entries with lifecycle stages (UnderDevelopment/Experimental/Stable/Deprecated/Removed), default-enabled bits, dependency normalization, legacy-alias deprecation notices, unknown-key diagnostics with file:line ranges (strict mode), and JSON-schema export for editors. Pythinker already has a solid simplified core (user/project/local scopes, type-based merge with provenance, env overlay, scope-locked secret paths), so the meaningful gaps are trust gating, graceful degradation, feature staging, and config observability/editing quality.

### context-mgmt

The reference harness treats the context window as a managed resource with explicit invariants and a full compaction state machine. A history manager (core/src/context_manager/history.rs, normalize.rs) records items with record-time truncation, enforces call/output pairing and modality invariants at prompt build, and estimates tokens from serialized bytes with calibrated discounts for images and encrypted content. Compaction (core/src/compact.rs, session/turn.rs) runs in distinct phases (pre-turn, mid-turn, manual) for distinct reasons (token limit under two scopes, model downshift, instruction-hash change), preserves verbatim user messages within a token budget plus a summary, self-trims and retries when the compaction request itself overflows, and re-injects initial context at model-expected positions. The model is made context-aware via injected token-budget fragments at usage thresholds, a remaining-context query tool, and a fresh-context-window request tool; settings/environment changes between turns are re-injected as diffs against a baseline snapshot (context_manager/updates.rs, context-fragments/). A hardened append-only JSONL file (message-history/src/lib.rs) persists cross-session prompt history with locking and byte caps. Pythinker already covers the compaction lifecycle, a cheap prune tier, hooks, rollback safety, and post-compaction restoration well; the meaningful gaps are overflow recovery, restore-time pairing repair, model-visible budget signals, verbatim user-message retention, and context carry-over on model switch.

### core-loop

The reference harness structures its core loop as session-owned tasks (regular/review/compact/user-shell kinds) driving a turn loop: each iteration drains queued mid-turn user input and inter-agent mail, builds a sampling request from history, streams the model response, executes tools, then re-evaluates token status. Robustness is layered in at every joint: pre-turn and mid-turn auto-compaction keyed to server-observed token usage (including compaction with the PREVIOUS model when switching models or downshifting context windows), retry/backoff with user-visible reconnect notices and transport fallback, reactive recovery from hard context-overflow and invalid-image errors, model-visible interrupted-turn markers, token-budget-remaining notices injected at usage thresholds, a per-turn aggregated diff tracker, and stop-hook continuation governance (key files: session/turn.rs, session/input_queue.rs, tasks/mod.rs, tasks/review.rs, session/token_budget.rs, responses_retry.rs, turn_diff_tracker.rs, state/auto_compact_window.rs). Pythinker's soul loop already matches most of this design — its gaps are concentrated in reactive (error-driven) context recovery, model-visible interruption/budget signals, model-switch continuity, and turn-level diff aggregation.

### exec-safety

The reference harness layers shell-exec safety in five tiers: (1) a declarative, user-extensible execution-policy engine (prefix rules with allow/prompt/forbidden decisions, per-rule justifications, load-time-validated match/not_match example tests, host-executable path pinning, strictest-decision aggregation, heuristics fallback when no rule matches); (2) a structural bash parser (tree-sitter grammar, whitelisted node kinds only) that auto-approves provably read-only commands — including `bash -lc "safe && safe"` composites — against a curated safelist with per-flag escape hatches (find -exec, rg --pre, git global-option bypass hardening); (3) OS sandboxes (seatbelt/landlock+bwrap/restricted token) with network disabled by default and a run-sandboxed-first, escalate-to-approval-on-denial lifecycle, plus an in-sandbox execve-interception protocol (Run/Escalate/Deny per nested command); (4) durable policy amendment — "always allow" appends a dedup-checked, file-locked allow rule (command prefix or network host) to the user's rules file; (5) pre-main process hardening (core dumps off, ptrace-attach denied, LD_/DYLD_ stripped). Pythinker already has a strong heuristic counterpart (profile-gated mutation/destructive/network/workspace-escape classifiers, signature-scoped session approval, deliberation backstop, secret-env scrubbing) but lacks the safe-command prompt-elision tier, any user-extensible policy language, durable cross-session rules, OS-level enforcement, and Windows/PowerShell-aware classification.

### mcp

The reference harness treats MCP as a managed connection fleet: a connection manager starts every enabled server concurrently under a cancellation token, emits per-server startup status events plus an aggregate completion summary, serves cached tool snapshots while servers are still connecting, and gates session start only on servers explicitly marked required. Robustness is layered into the client itself — per-server startup/tool timeouts with actionable error text, bounded retry/backoff for retryable HTTP initialize failures, OAuth discovery/scope resolution, per-server tool allow/deny filters enforced at both list and call time, and model-visible tool-name normalization (charset sanitize, collision hashing, 64-char cap) that preserves raw names for protocol routing. It also keeps persistent sessions so server-initiated traffic (elicitation requests with policy-based auto-accept/deny, log/progress/list-changed notifications) is handled, and it ships a reverse mode exposing the whole agent as an MCP tool over stdio. Pythinker (fastmcp-based) already covers non-blocking startup with status snapshots, OAuth login/pre-check, resource/prompt listing+reading, output budgeting, and hardened teardown, but connects once, freezes the tool list, drops the session between calls, and lacks required-server gating, startup timeouts, tool filtering, name normalization, and any server-initiated request handling.

### multi-agent

The reference harness treats child agents as long-lived, addressable threads rather than one-shot workers: a v1/v2 collaboration tool suite (spawn_agent, send_input with optional interrupt, wait on multiple targets, list/close/resume) lets the orchestrator steer running children mid-task; spawn supports role overlays (config layers with model/effort/instructions), full-history context forking, and depth/thread guardrails enforced by a registry with friendly model-facing errors. Around that core sit batch primitives (CSV-driven job fan-out with bounded concurrency and a worker-side result-reporting tool), a cloud best-of-N attempt surface (1-4 parallel attempts of the same prompt with sibling diff comparison and selection), a storage-neutral persisted parent/child spawn-edge graph with open/closed lifecycle and BFS descendant queries, and collaboration-mode presets (Plan/Default masks bundling model, reasoning effort, and developer instructions, with per-mode capability gates). Pythinker already covers most of the structural ground (typed subagents with permission profiles, foreground/background runners with resume, RunAgents fan-out with capacity slots and orchestration approval, crash recovery, completion notifications); the real gaps are interactivity (steering a live child), context forking at spawn, enforced workspace isolation for parallel writers, and multi-target waiting.

### observability-feedback

The reference harness splits observability into four layers: (1) an OTel layer with a session-scoped business-event emitter that stamps every event/metric with session metadata tags (auth mode, originator, session source, model, app version), a rich event taxonomy (conversation_starts, per-attempt api_request, per-stream-event sse/websocket health, tool_decision, sandbox_outcome, tool_result, startup_phase, turn TTFT), and strict metric tag validation/bounded-cardinality normalization; (2) a centralized analytics fact-reducer that consumes the protocol event stream on a bounded queue and emits consolidated per-turn rollup events (tool-type counts, token usage, steer count, error kind, resolved config, and a five-segment turn latency profile) plus privacy-hashed accepted-line counts parsed from unified diffs as a code-retention outcome metric; (3) a feedback subsystem with a process-wide full-fidelity log ring buffer independent of the console filter, a structured feedback-tags layer accumulating session diagnostics tags, connectivity diagnostics (proxy env detection), and consent-gated uploads with ordered attachments; and (4) opt-in local raw-evidence trace bundles (seq-ordered event log + payload files + offline reducer) that separate model-visible conversation from runtime observations for deep failure forensics, plus HTTP response debug-context extraction (request-id/gateway headers) with deliberately coarse telemetry error messages. Pythinker already has a mature equivalent of layer 1's core (OTel traces/metrics/logs with nested turn→llm→tool spans, GenAI semconv usage attrs, crash handlers, expected-error classification, recent-errors ring, broad track() taxonomy including approval decisions and compaction) and a strong consent-gated /feedback + /report-error pipeline with redaction; the meaningful gaps are per-turn rollup facts, turn latency decomposition/TTFT, code-retention analytics, feedback log/diagnostics attachments, and the local trace bundle.

### patch-file-tools

The reference harness ships a four-part file-manipulation stack: (1) apply-patch — a dedicated multi-file patch DSL (Add/Update/Delete/Move hunks) parsed strictly-but-leniently, located in files via a graduated fuzzy context matcher (exact → rstrip → strip → Unicode-punctuation normalization, EOF-anchored), applied with committed-change delta tracking (old/overwritten content + an `exact` flag that flips when a failed write may have mutated state), shell-invocation interception (AST-parses bash/pwsh/cmd heredoc forms incl. `cd dir &&` prefixes), and a streaming parser for live patch preview; (2) file-system — an async FS trait abstracting local/remote execution; (3) file-search — a persistent background walker + fuzzy-matcher session with cheap re-query, debounced top-N snapshots, cancellation, and highlight indices; (4) file-watcher — a refcounted shared OS watcher with per-subscriber coalesced/debounced/throttled receivers and missing-path ancestor fallback, used for skills hot-reload and fs-change notifications. Pythinker's str-replace-based editing stack is robust on single-file paths (batch in-memory validation, CRLF fallback, restore points, arg-shape normalization, symlink-resolved workspace checks) but lacks fuzzy edit-location recovery, first-class delete/rename, shell-mediated-edit interception, any file watcher, and large-repo-scalable fuzzy file search.

### persistence-resume

The reference harness persists each session as an append-only JSONL rollout of canonical model items plus a whitelisted subset of UI events (explicit persistence policy), written by a dedicated writer task with flush acks and latched terminal failure. A SQLite state runtime indexes thread metadata (title, preview, cwd, git provenance, token usage, archive state) and is kept honest by a lease-guarded, watermark-checkpointed backfill that re-extracts metadata from rollout files and self-repairs the index; listing supports stable timestamp+uuid cursors, sort/filter, and ripgrep-accelerated full-content search with snippets. Cold rollouts are transparently zstd-compressed and atomically rematerialized on append. Sessions created by a different agent harness are detected in that agent's home dir, converted into native rollout items as resumable threads, and deduplicated via a content-sha256 import ledger. Corrupt runtime SQLite stores are quarantined (DB + WAL/SHM moved to a timestamped backup dir) and rebuilt without touching sibling stores. Pythinker already has strong parity on the core log (context.jsonl/wire.jsonl with control records, torn-line resilience, flock single-writer), fork/undo, archive lifecycle, and resume replay; the real gaps are a persisted metadata index, session provenance (git/version/lineage), transcript content search, structured cross-agent import, and cold-session compression.

### prompts-instructions

The reference harness treats prompting as a layered, state-synchronized system rather than one static file: (1) distinct base system prompts per model family, scaled to model capability (a ~68-line minimal prompt for agent-tuned models vs ~300-line detailed prompts with worked planning examples for general models), plus a templated model-instructions file with a {{personality}} slot filled from curated personality presets; (2) switchable collaboration-mode developer-message templates (default / plan / pair-programming / execute), each explicitly voiding prior mode guidance, with mode changes driven only by developer messages and never user intent; plan mode is a conversational, decision-complete protocol with an explicit taxonomy for discoverable-fact vs preference unknowns and plan-document compactness rules; (3) dynamically composed permissions instructions rendered from the live sandbox/approval config — sandbox mode, writable roots, denied reads, approval-policy variant, already-approved command prefixes, command-segmentation semantics — plus a model-initiated escalation protocol (justification question + suggested scoped reusable allow-prefix with banned-prefix guidance); (4) hierarchical AGENTS.md scope/precedence guidance; (5) a review lifecycle with harness-resolved targets (uncommitted / base-branch with precomputed merge-base SHA / commit) feeding a strict bug-qualification rubric, and structured result re-entry into main history; (6) compaction and goal-mode prompt templates. Pythinker has already adopted the AGENTS.md hierarchy, the reviewer rubric, compaction/goal templates, todo discipline, dirty-worktree guardrails, and a stronger tool-enforced plan mode; the real gaps are the dynamic permissions-state prompt block, model-initiated escalation with justification/suggested rules, decision-complete plan interviewing, and the review-target command lifecycle.

### protocol-headless

The reference harness treats headless (non-interactive) runs as a first-class automation surface built on the same typed protocol as its interactive clients. A thin exec runner is a JSON-RPC client of an in-process app server (thread/start, turn/start, turn/interrupt, thread/read) and renders events through one of two strictly separated processors: a JSONL mode where stdout carries exactly one stable, versioned event per line (thread.started → turn.started → item.started/updated/completed → turn.completed{usage} / turn.failed / error, with normalized sequential item ids and typed item payloads), and a human mode where ALL progress goes to stderr and the final answer goes to stdout only when piped — making `$(...)` capture safe. Automation affordances include: structured final output validated against a caller-supplied JSON Schema (--output-schema), final-message-to-file (-o), nonzero exit on turn failure/interrupt, approvals forced to never with every interactive server request auto-rejected/auto-cancelled with an explanatory reason, a git-repo trust gate before unattended runs, --ephemeral no-persistence runs, resume-by-id/name/--last with cwd filtering, robust stdin prompt contract (`-` sentinel, piped-stdin-appended-as-tagged-block, BOM/UTF-16 detection), and turn-completed item backfill via thread/read when event delivery dropped items. Protocol types are exported as checked-in JSON Schema/TS fixtures so external clients can codegen against a versioned API.

### review-mode

The reference harness ships code review as a first-class session mode. A /review command opens preset pickers (uncommitted changes, base branch via a searchable branch list, one of the last 100 commits, or custom instructions); the chosen target is deterministically resolved into a synthesized prompt whose merge-base SHA is precomputed (preferring the branch's upstream when the remote is ahead), with a fallback prompt that teaches the model to compute the base itself. The review runs as a one-shot child thread that clones the parent config but force-disables web/collab/image tools, clamps approvals to never-ask, swaps in a dedicated review rubric as base instructions, and optionally uses a dedicated review model. The reviewer must emit strict JSON findings (priority-tagged titles P0-P3, per-finding confidence, file+line-range anchors, overall_correctness verdict); parsing is lenient (first {...} substring, then plain-text fallback into overall_explanation, never lost). On exit — including abort — the parent history records a synthetic findings block plus an explicit "review was interrupted, re-run" message, the UI flips a review-mode banner and restores the pre-review token display, and findings can be checkbox-selected to dispatch fixes. A separate "guardian" mechanism reuses the same delegate pattern as an LLM approval reviewer: it rebuilds a token-capped transcript, assesses the exact planned action into strict JSON {risk_level, user_authorization, outcome, rationale}, fails closed on 90s timeout or malformed output, and has circuit breakers (max 3 consecutive denials/turn, denial windows, special developer prefix when the user manually overrides a denial). Pythinker already covers most of the reviewer-quality surface (rubric-calibrated reviewer subagents, offline fail-closed review profiles, a full deterministic diff-review engine with confidence-scored schema-validated findings, report-block rendering, per-agent model overrides); the real gaps are the interactive TUI entry point with git pickers, deterministic target-to-prompt synthesis for agent-mediated review dispatch, upstream-aware merge-base, findings triage-to-fix, and the LLM approval guardian.

### skills-hooks-memories

The reference harness treats skills, hooks, and durable memories as three tightly engineered subsystems. Skills: layered SKILL.md discovery (repo/user/system/admin scopes, bounded scan, frontmatter + metadata sidecar with per-skill policy), a token-budgeted prompt listing (2% of context window, graceful description truncation, aliased root paths, omission warnings + telemetry), explicit $-mention resolution with ambiguity guards, implicit-invocation detection (model reading a skill doc or running its scripts counts as invocation), and a detailed "how to use skills" doctrine (trigger rules, read-to-EOF, no subagent delegation of skill reading, minimal-set + announce). Hooks: 10 lifecycle events with matchers and a JSON-schema stdin/stdout contract whose outputs can block, rewrite tool input, decide permission requests, inject additional model context, return post-tool feedback, and force turn continuation from Stop hooks; handlers carry a hash-based trust identity (managed/trusted/modified/untrusted) so repo/plugin-provided hooks never execute until trusted, and oversized hook output spills to disk with a head/tail preview plus recovery path. Memories: a two-phase background pipeline — Phase 1 LLM extraction per past session (DB-leased jobs, concurrency caps, retry backoff, strict no-op gate, outcome triage, preference-signal-first prompts, secret redaction) and Phase 2 a sandboxed consolidation agent operating on a git-baselined memory workspace whose diff drives incremental update and forgetting, producing an always-loaded summary, a greppable handbook, rollout summaries, and even auto-authored skills — with a read-path usage/citation tracker that feeds usage counts back into retention ranking, and a rate-limit guard that skips background work when quota is low.

### tools-registry-codemode

The reference harness factors model-visible tooling into a dedicated layer: ToolSpec/ToolName models with namespacing, a registry of typed tool executors carrying per-tool metadata (exposure, parallel-safety, cancellation semantics, hook payload contracts, search text), and a router that dispatches calls under a read/write concurrency gate with teardown-aware abort handling. Around it sit three robustness subsystems: JSON-schema sanitization plus budgeted compaction for foreign (MCP/dynamic) tool schemas, deferred tool loading with an on-demand tool-search loader, and a unified interactive PTY exec manager (persistent process ids, stdin writes with clamped yield windows, head+tail capped transcripts, LRU pruning of up to 64 sessions). On top is a tools-as-code mode: an exec tool runs model-written script in an isolated runtime where every nested tool is a callable, with resumable long-running cells (wait/terminate), cross-cell store/load, and incremental notify streaming. Pythinker already has a solid registry (dedup, hooks, telemetry, policy-based visibility) and a strong background-task subsystem, but lacks the concurrency policy, schema sanitization, tool-search deferral, PTY interactivity, head+tail retention, and any code mode.
