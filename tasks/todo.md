# Tasks

## Active

### Deterministic reviewer target resolution (2026-07-15)

- [x] Reconcile the adoption ledger with current code and Git history.
- [x] Select the narrow structured-target design; keep `/review` out of scope.
- [x] Review and approve
      `docs/superpowers/specs/2026-07-15-review-target-resolution-design.md`.
- [x] Write the implementation plan with TDD and verification checkpoints.
- [ ] Implement the approved plan in the isolated feature worktree.
- [ ] Add the required `CHANGELOG.md` Unreleased entry.
- [ ] Run focused tests, `make check-pythinker-code`, `make test-pythinker-code`, and final review.

Acceptance: every fresh reviewer receives one pre-resolved authoritative Git target; invalid or
empty explicit targets fail before child allocation; `Agent` and per-child `RunAgents` behavior is
identical across foreground and background execution; generic Git context cannot contradict the
resolved target; non-reviewer and resume behavior remains compatible.

### TUI thinking Markdown and activity motion (2026-07-11)

- [x] Execute `docs/superpowers/plans/2026-07-11-tui-thinking-markdown-and-activity-motion.md`
      with TDD and the Pythinker guard checkpoints.
- [x] Render complete thinking-preview Markdown without leaking top-level HTML comments.
- [x] Keep activity-tree detail text static and reserve coral shimmer for the verb spinner.
- [x] Add the required `CHANGELOG.md` Unreleased entry.
- [x] Run focused UI tests, `make check-pythinker-code`, and `make test-pythinker-code`.
- [x] Run clean-code, test, docs, Pythinker guard, verification, and final diff review passes.

Acceptance: complete Markdown emphasis renders without delimiters; complete top-level HTML comments
are hidden; malformed Markdown and comments remain readable; fenced literal comment examples remain
visible; activity-tree details do not shimmer; the bottom verb spinner retains its coral shimmer;
reduced-motion and lifecycle-marker contracts remain green.

#### Review: TUI thinking Markdown and activity motion

Executed subagent-driven (implementer → task review → fix loop → final whole-branch review) across
four commits `3505c47c..be252dbf` on `a20bfda0`.

**Resulting behavior (all verified by tests):**
- Complete Markdown: the live thinking preview renders through `render_agent_body`, so `**bold**`
  and other emphasis delimiters no longer leak into the preview.
- Complete top-level HTML comments: a line that is entirely a top-level `<!-- ... -->` comment is
  removed before the Markdown renderable is built.
- Malformed/incomplete input: an unterminated `<!-- ...` (and other partial markup) is preserved as
  readable streaming text; nothing raises out of the Live loop.
- Fenced literal examples: a `<!-- ... -->` inside a fenced code block renders verbatim (fence-aware
  split via `iter_fence_aware_lines`).
- Mixed prose+comment line: left fully intact (markers included) — the stripper is line-anchored, so
  it only removes whole-line comments and never deletes visible prose.
- Stable tree rows: every activity-tree running detail renders with `shell_style(ShellTone.MUTED)`;
  the lifecycle marker running pulse (`blink_visible`) is unchanged.
- Verb-spinner shimmer: `activity_status_line` / `_todo_activity_line` remain the only coral verb
  shimmer, including reduced-motion and no-color fallbacks.

**Two confirmed root causes:** (1) `_ContentBlock._compose_thinking_stream` built a plain
`Text(preview, ...)`, bypassing Markdown; (2) `render_activity_tree` called `shimmer_text` on each
`running` detail, animating one tree row.

**Evidence (fresh terminal output):**
- Focused TUI set (6 modules): `178 passed, 1 warning` (the warning is pre-existing pytest temp-dir
  cleanup from unrelated `knowledge_base` tests, not this change).
- Full package unit `tests`: `6927 passed, 9 skipped, 1 xfailed`.
- Separate `tests_e2e`: `65 passed, 4 skipped`. `make test-pythinker-code` exit 0, no failures.
- Static gate `make check-pythinker-code`: ruff `All checks passed!`, `1250 files already formatted`,
  pyright `0 errors, 0 warnings, 0 informations`, ty clean, `All checks passed!`.
- `git diff --check`: clean (no output).

**Quality-review verdicts:** Task 1 review — spec ✅, one Important plan-mandated regex bug
(non-greedy `.*?` backtracking swallowed visible prose on a mixed line); fixed (tempered token
`(?:(?!-->).)*?`) + regression test, re-review ✅. Task 2 review — ✅ approved, no issues (RED
corroborated: `#c68d7e` = `activity_verb`). Task 3 docs review — ✅ accurate & truthful. Final
whole-branch review (Opus, C01–C15 + failure-truthfulness) — **Ready to merge: Yes**, zero
Critical/Important/Minor.

**Approved deviations:** the plan-mandated comment regex was tightened with maintainer approval to
stop same-line silent prose deletion. **Remaining blockers:** none. A line of two adjacent complete
comments with zero prose between them (`<!-- a --><!-- b -->`) is now preserved — an accepted
safe-direction narrowing (under-stripping a marker beats deleting prose), pinned by test.

### Agent core deepening program (2026-07-10)

- [x] Execute `docs/superpowers/plans/2026-07-10-agent-core-deepening.md` on
      `feat/agent-core-deepening` using TDD and subagent-driven task reviews.
- [x] Phase 1: characterize provider handoff, static prompt, JSONL, agent projections,
      and Toolset facade behavior.
- [x] Phase 2: ship bounded `SkillCatalog` discovery with exhaustive compatibility.
- [x] Phase 3: ship observable request assembly and `/prompt-manifest`.
- [x] Phase 4: ship transactional Context replacement and disk-first appends.
- [x] Phase 5: ship WARN-mode resolved agent catalogue with strict-mode tests.
- [x] Phase 6: characterize Toolset, record thresholds, and extract only if measured.
- [x] Run full guards, gates, two-axis review, and document the final result here.

#### Review: agent core deepening

- Outcome: all six approved phases shipped on the umbrella branch. Bounded skill
  discovery, observable request assembly, transactional Context persistence, and
  resolved agent definitions retain their documented compatibility projections.
  `/prompt-manifest` exposes only sanitized in-memory assembly metadata.
- Toolset decision: NO-GO on private extraction. The controlled execution-pipeline
  attempt worsened median framework overhead from 99.568352% to 99.606995% and was
  reverted. The reproducible schema-v2 report contains 18 scenarios and seven
  command-generated decisions. Five true 500-tool registry p95 values were
  3.245000, 2.934625, 3.091542, 4.716500, and 3.079834 ms; none crossed 5 ms.
- Deviation: deterministic fault characterization exposed an exception-atomicity
  defect in MCP registry publication. The surgical rollback preserves the previous
  registry and re-raises the original registration failure. Final review also
  replaced mock state with real `CompactionResult` and `TaskView` instances and
  bounded optimistic revert conflicts to three attempts before surfacing the typed
  generation conflict.
- Compatibility windows: `Runtime.skills` remains until internal exact lookups have
  migrated and repository search proves it removable. Unknown agent fields warn in
  this release and become errors in the following minor release. `LaborMarket`,
  `AgentTypeDefinition`, and generated Markdown wrappers remain through that
  strict-default release; their earliest removal is the next minor release, subject
  to direct-launch parity and migration checks.
- Verification: `make check-pythinker-code` passed Ruff, formatting, Pyright, and ty;
  `make test-pythinker-code` exited 0 after collecting 6,932 package tests and then
  passed 65 E2E tests with four skips; provider snapshots passed 39 tests; the
  Toolset/fault matrix passed 96 tests; focused guard fixes passed 32 tests; and
  `git diff --check` passed. Expected Loguru/Python deprecation and pytest temporary
  cleanup warnings remain non-blocking.
- Review: Task 13 rereview approved the reproducible decision builder, true p95
  samples, handle-level cancellation recovery, and MCP observability. Final Standards
  and Spec reviews found no Critical/Important implementation defect; their release
  metadata blockers were resolved here. Pythinker, clean-code, test, and docs guard
  passes found no remaining ship blocker.
- Blockers: none.

### Plan: publishable benchmark comparison (2026-07-05)

- [ ] Execute `docs/superpowers/plans/2026-07-05-publishable-benchmark-comparison.md`
      with `/tdd` discipline. Scope: multi-model comparison, coding activity
      metrics, publishability warnings, exportable reports, and safe online
      source discovery for provisional benchmark manifests. Guardrail: online
      content stays untrusted and must not execute verifier commands without
      explicit `--trusted-dataset true`.

### Review: benchmark hardening (2026-07-05)

- [x] Enforce per-task benchmark `max_steps` by temporarily applying it to the
      underlying soul loop control and restoring the previous limit after the
      run.
- [x] Require explicit `--trusted-dataset true` for `/benchmark:swe`, because
      SWE-style local fixture datasets contain verification shell commands.
- [x] Label SWE-style tasks as local fixtures, not full SWE-bench Docker
      evaluation.
- [x] Strengthen bundled `pythinker-core` tasks with reviewed edge cases for
      atomic rollback, generator de-duplication, falsey explicit metadata, and
      absolute/sibling/symlink path escapes.
- [x] Document the benchmark integration architecture across the public
      reference docs, repository map, changelog, and slash-command docs.
- Verification: red tests confirmed the benchmark gaps first. Final focused
  gates passed locally: benchmark pytest group, pyinstaller manifest tests,
  benchmark task/suite metadata load, ruff format/check, pyright, ty, docs
  build, and targeted `git diff --check`. Full `make check-pythinker-code`
  is blocked by unrelated dirty formatting in
  `src/pythinker_code/ui/shell/prompt.py`.

- [ ] Agent-harness adoption arc (`feat/agent-harness-enhancements`): executing
      `tasks/agent-harness-adoption-plan.md` (124 verified items, tiers 1-4).
      DONE: all 5 Tier-1 high/S + first high/M — `047a0b29` orchestration
      provider+scrub, `b40cdb71` ACP question-tool hide, `36cedafd` plan,
      `e722278c` restore-time history invariant repair, `388da2d3`
      decision-complete plan mode, `f5b9b06a` print channel discipline,
      `c8d82d38` review git-context+merge-base, `e2e74b70` parallel-tool
      concurrency policy, `1615cfbd` reactive overflow recovery (loop +
      SimpleCompaction halving; classify_api_error → soul/api_errors.py),
      `0f39a3b1` per-project trust gating of project hooks (project_trust.py
      store + /trust persistence + untrusted-TOML tolerance), `9d1178f4`
      unknown-config-key diagnostics (unknown_config_key_paths +
      PYTHINKER_STRICT_CONFIG).
      `bf549c28` model-switch carry-over (summarize_all with the outgoing
      model seeds the new session; model_switch_carryover flag).
      `5dc87aaf` known-safe command auto-approval (is_known_safe_command
      positive allowlist; root-only elision, deny-gate preserved).
      `5dc87aaf`+`59deceff` known-safe command elision (+env-prefix
      allowlist security fix; e2e approval pins moved to wrapper commands
      `5dadb1c6`), `05f86428`+`df4801f5` MCP startup timeout + actionable
      failure diagnostics (/mcp shows classified error lines).
      `60fc8b16` MCP per-server tool filtering (enabledTools/disabledTools,
      list-time + call-time).
      `a01e5940` spawn-time context fork (Agent fork_context=true seeds
      foreground children with the filtered conversational spine; background
      fork is a tracked follow-up).
      Workspace isolation: design note at tasks/worktree-isolation-design.md;
      P1 seam `56d6fa53` + P2 lifecycle `8bc6c697` DONE (background
      write-profile children get per-agent worktrees, diff-summary reports,
      recovery-aware cleanup). P3 DONE (chain verified end-to-end; descriptions state enforced semantics). Isolation item CLOSED.
      `eb13fdae` fuzzy edit-recovery ladder (rstrip→strip→unicode-punct
      line-window seek in StrReplaceFile; ambiguity contract kept).
      `cde3c726` live permissions-state injection (posture-fingerprinted
      provider; Approval read accessors).
      NEXT (Tier-1 high/M, plan order): MCP startup
      timeout+diagnostics; MCP per-server tool filtering; subagent context
      fork; workspace isolation for parallel writers; turn rollup analytics;
      feedback diagnostics; fuzzy edit ladder; permissions-state
      instructions; model escalation w/ justification; JSONL lifecycle
      stream; schema-constrained final output; /review command; hook trust
      gating; PostToolUse feedback to model; deferred tool loading; foreign
      schema sanitization. Discipline: TDD + clean-code-guard + make check
      per checkpoint; single writer now.
- [ ] Windows shell hardening (researched, not yet implemented): bash-first
      shell policy (Git Bash probe → pwsh → powershell, never cmd), Windows
      tool-description guidance (`;` not `&&` on PS 5.1, `$env:`, quoting),
      docker-daemon-down interceptor (`error during connect` +
      `pipe/docker` → actionable remediation incl. `docker desktop start`),
      POSIX-ism lint under PowerShell, `CTRL_BREAK_EVENT` + `taskkill /T`
      tree-kill, `-EncodedCommand` UTF-16LE for PowerShell args. Full brief in
      session notes 2026-06-12; permission tokenization is POSIX-blind for
      PowerShell syntax (gate review needed before shipping).
- [ ] Live MCP reconnect / `tools/list_changed` — the one real remnant left
      from the (now-deleted) reference-port and agent-enhancement plans. Today
      `cli/mcp.py` has list/remove/auth/reset-auth/test only and
      `toolset.py:1435` is just a forward-looking comment. Add
      `/mcp reconnect|disconnect|refresh` verbs + a `tools/list_changed`
      handler so a server's tool set can re-bind without a session restart.

Merged from `refactor/agent-contract-and-tool-metadata` — step 1 of
`tasks/design-adoption-blueprint.md` (agent-logic/coding-flow cleanup):

- [x] Task 1: FetchURL untrusted-envelope fix — DONE (ceeeeb78; 3 TDD tests,
      spec + quality review approved). Deferred: add `await
      builder.spill_to_disk()` at the trafilatura + fetch-service sites for
      event-loop hygiene (pre-existing asymmetry; no output difference).
      Original: `tools/web/fetch.py:232,277,338`
      write pre-rendered `UntrustedData(...).render_for_prompt()` into the
      builder, so truncation can cut the closing envelope tag and break
      `strip_untrusted_envelope` (endswith). Switch to raw write +
      `builder.mark_untrusted()` (idiom: `tools/web/search.py:174-177`).
      → verify: new TDD test reproducing tag truncation fails before / passes
      after; `tests/utils` untrusted-wrapping suite green.
- [x] Task 2: Public `turn()` contract on PythinkerSoul — DONE (7e96dfb8;
      thin delegate keeps `_turn` as the test patch point; 3 call sites
      migrated, suppressions removed; contract docstring distinguishes
      framed vs unframed callers; spec + quality review approved).
- [x] Task 3: Declarative tool metadata for the dispatch/permission gates —
      DONE (eaa96d6e; `external_side_effect_tool` ClassVar on the 3 adapters,
      `emits_tool_execution_started_after_approval` pinned on 8 classes,
      both string-match consumers rewritten; spec review, quality review,
      and security review (SAFE TO MERGE) all passed).

Review: branch `refactor/agent-contract-and-tool-metadata` (3 commits on top
of bff54f94) final-reviewed READY TO MERGE. Full `make test-pythinker-code`:
5279 passed, 1 failed — the failure is
`tests/e2e/test_shell_pty_e2e.py::test_shell_cancel_running_command_kills_process_and_recovers`,
verified PRE-EXISTING and machine-local: fails identically in isolation on
main (bff54f94) and on 7caeca33 / d51ef649 / 2904de00, all of which merged
with green CI. It is the ONLY test that sends ESC, so the local ESC-interrupt
PTY path has no corroborating coverage. Needs its own debugging session
(suspect ESC flush timing in the local macOS/Python 3.14 PTY environment).

Out of scope this PR (logged): pricing display move out of core, the
`extract_key_argument` name→spec registry, larger soul/toolset splits
(blueprint P1a/P2a/P2b).

### Deferred from this branch's reviews
- [x] FetchURL spill awaits — DONE: `await builder.spill_to_disk()` added at
  the trafilatura + fetch-service sites.
- [x] MCPTool event ordering — DONE: `emits_tool_execution_started_after_approval`
  set on MCPTool (Approval.request emits after resolution, idempotent per
  call id); pinned in test_toolset.py alongside the other 8 classes.
- No structural enforcement that FUTURE external adapters declare
  `external_side_effect_tool` (pin tests cover the current three only).
  Deliberately NOT bolted on now: the three adapters share no in-package
  base class to hang an `__init_subclass__` hook on; revisit when the
  toolset split (blueprint P2a) introduces an adapter base.

Done: `mythos-enhancements` PR #118 merged (d51ef649).

### Deferred (documented, not silently dropped)

- Arc-review finding (medium, deliberate deferral): the same-step exclusive
  gate is held across a mutating tool's approval wait, blocking sibling
  parallel-safe reads in that batch. Proper fix = split approval from
  execution per tool (approval ungated, only the mutation gated) — an
  invasive per-tool refactor; the cost today is bounded (elision/auto modes
  remove most waits). Revisit with the approval-split refactor.

- Read-only MCP doc-lookup carve-out for offline roles — today ALL MCP tools
  are fail-closed below the implement profile (deliberate); reviewer specs
  route doc needs to the parent/`scout` instead. Revisit only with a real
  allowlist mechanism design.
- Per-profile shell timeout caps: rejected — long `pythinker review`/`secscan`
  runs are legitimate; spec-level timeout discipline shipped instead.
- Codename polish: a background RunAgents child can show a name codename AND a
  different task-id codename (each distinctive, mildly redundant); aligning
  them needs a preferred-suffix param through `manager.launch_agent_task`.
- Narrow race: a subagent launched while the parent's background MCP connect is
  still in flight misses shared MCP tools (map populated later) — needs a
  re-bind or wait at subagent build.
- Test-design debt: test_learn_slash mocks soul._turn; test_soul_status_cost
  asserts an import — black-boxing them is its own task.
- From the review-safety plan: full ShellReadPolicy allowlist (would deny every
  unclassified command — needs maintainer call); ReviewCapabilityRegistry +
  scanner ladder (no external scanner subsystem exists yet); Phase 5
  heartbeats/token budgets (own PR); Phase 6 full TaskEventStore renderer
  rewrite (own PR); OS-level sandboxing (Seatbelt/Landlock).
- Non-interactive Rich Live path forces `live.update(refresh=True)` on every
  wire message in `_live_view.py`, bypassing its 10fps clock — separate
  flicker contributor in that mode.
- Provider compat: 400 "enable_thinking restricted to True" lives in
  pythinker_core openai_legacy (external); Bugsink keeps reporting it (4xx
  stays unexpected) until fixed upstream — desired.
- Infra: edge OTLP collector accepts any bearer token; SigNoz SMTP may need
  configuring for alert email delivery.

## Recently completed

Completed-work logs through 2026-06-11 (agent robustness arc, statusline v2,
review-safety hardening, telemetry sync, CodeRabbit triage) were trimmed on
repush of PR #118 — see git history of this file for the full record.
