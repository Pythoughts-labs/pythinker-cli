# Tasks

## Active

### Generic auth + API-key login providers, dynamic catalog, effort mapping (2026-07-18)

Branch: `feat/auth-login-providers`. Framing is generic (add auth + API-key login providers);
`blackbox/opencode/AUTH_PROVIDERS.md` is only the reference source, not user-facing branding.

**Source:** `blackbox/opencode/AUTH_PROVIDERS.md` + opencode source (**MIT**; pythinker is
Apache-2.0 — compatible; ported logic carries an attribution notice).
**Delivery:** plan-first, then **sequential** delegation to Codex `gpt-5.6-sol` (high). NOT parallel —
every workstream mutates the same core files (`config.py ProviderType`, `llm.py create_llm`,
`platforms.py PLATFORMS`/`refresh_managed_models`, both UI menus, both CLI dispatchers).
**Effort ceiling:** `max` (no `ultra`) — pythinker's `ThinkingEffort` union already covers it, so the
effort workstream needs **no union change / no new-enum snapshot churn**.

Decisions locked (AskUserQuestion): all four workstreams; plan-first; cap at `max`.

**Headline open decision (user's call): registry-first vs additive-first.**
Recon: adding one provider today touches **8–12 hand-edited enumeration files** (no registry).
- additive-first = cheapest per phase, lowest risk, but re-pays the 8–12-file tax per provider and
  that code is throwaway if a registry lands later.
- registry-first = build the abstraction up front so new providers are cheap/non-throwaway, but it is
  the highest-churn refactor of working, public-compat code.
Discriminator = how many providers wanted. 4 OAuth only → additive; full roster → registry-first.

**Size (honest):** multi-thousand-LOC across ~15–25 files; new `ProviderType` values, config keys,
CLI flags, persisted shape → tests + docs + CI snapshots (config-dump / pyinstaller / wire) each.

Phases (each = one verified Codex delegation, sequential):
- [x] P1 — Dynamic models.dev catalog (DONE, green: `make check-pythinker-code` + focused tests 342
      passed): new `auth/models_dev.py` (httpx fetch, 5-min TTL, `fcntl.flock`, atomic write,
      env override/disable, fail-open), `opencode_go.py` consumes it, tests + changelog added.
      Codex `gpt-5.6-sol` candidate (commit `ee6d8384`) + architect compat-fix (`e6adfead`).
      Generalize `auth/opencode_go.py` fetch into a provider-agnostic
      module (`GET https://models.dev/api.json`, env override + disable flag, disk cache, 5-min TTL +
      60-min bg refresh, **stdlib `fcntl.flock`**, atomic temp+rename, **fail-open** cached→static,
      never block startup). Wire one provider through it.
- [ ] P2 — Effort/family mapping: port opencode `variants()` tier-selection into the effort layer,
      **capped at max**, with attribution. Extend `openai_gpt_reasoning_levels` → per-family table.
- [~] P3 — OAuth providers (sub-phased, sequential; each provider touches shared enumeration files):
      - [x] P3a — shared `auth/oauth_flows.py` (device-code + loopback-PKCE) + tests. DONE, green (349
            passed). Codex candidate `cc59239a` + architect fix `79b64d42`.
      - [x] P3b — GitHub Copilot (device-code; github token → copilot bearer exchange). DONE,
            committed `5bd283e4`. Codex candidate `9ab0a577` (runId 9fe426d4), correctness-approved
            after the token-preservation fix (F-001/F-002); clean-room green: `All checks passed`
            (ruff+format+pyright+ty) + `458 passed`. Two-refresh regression test present. **PENDING
            LIVE VERIFICATION**: no offline gate exercises client_id/exchange/headers/URL — needs a
            real `pythinker login --copilot` + one chat call before "truly done". **Design
            FINAL (primary-source verified 2026-07-18):** client_id `Iv1.b507a08c87ecfe98` + scope
            `read:user` (the exchange-proven pair from ericc-ch/copilot-api; NOT opencode's
            `Ov23li…` which is proven only with the no-exchange direct-token path). Device:
            POST github.com/login/device/code + poll .../login/oauth/access_token (both need
            `Accept: application/json` → thread a `headers` param through oauth_flows
            `_post_form`/`request_device_code`/`poll_device_token`). Exchange:
            GET api.github.com/copilot_internal/v2/token, hdrs `Authorization: token <gh>`,
            `Editor-Version: vscode/<ver>`, `Editor-Plugin-Version: copilot-chat/0.26.7`,
            `User-Agent: GitHubCopilotChat/0.26.7`, `X-GitHub-Api-Version: 2025-04-01`
            → `{token, expires_at, refresh_in}`. Store OAuthToken(access=bearer,
            refresh=gh_token, expires_at); `_refresh_token_for_ref` re-runs exchange from gh_token.
            Provider: type `openai_legacy`, base_url `https://api.githubcopilot.com` (**NO /v1** —
            SDK appends /chat/completions to root), oauth ref `oauth/github-copilot`,
            custom_headers = the copilot chat headers (Copilot-Integration-Id: vscode-chat + editor
            hdrs + Openai-Intent: conversation-panel; **NO Authorization** — bearer flows via
            resolve_api_key→api_key). Skip-guard `managed:copilot` in refresh_managed_models.
            **Scope: github.com individual only.** Business/Enterprise (endpoints.api routing,
            api.business/individual.*) DEFERRED — do not claim exchange fixes Business (opencode
            #23540) while hardcoding the individual host. **Acceptance:** offline gates green ≠ done;
            none of client_id/exchange/headers/URL are gate-exercisable → requires live
            `pythinker login --copilot` + one real chat call before marking done.
      - [x] P3c — xAI/Grok (browser loopback + device-code). DONE, committed `b586080c`. Codex
            candidate `1419210a` (runId f12a8003) — verification-failed on strict pyright
            (`.get()` on an isinstance-narrowed bare `dict` → reportUnknownMemberType/Argument);
            salvaged via `git checkout <candidate> -- .`, added a cast'd `_error_description`
            helper, re-ran gates (`All checks passed` + `2741 passed`). Two OAuth methods only
            (loopback port 56121 + `plan=generic`/OIDC `nonce`; device-code); no API-key method;
            openai_legacy → api.x.ai/v1; rotating refresh persisted by OAuthManager. **PENDING LIVE
            VERIFICATION** (login flow untested against real auth.x.ai).
      - [x] P3d — DigitalOcean. DONE. Lane A `run_loopback_implicit_flow` helper committed `48a1fdbb`
            (salvaged after base-changed abort); Lane B provider+wiring+tests committed `962ef990`
            (salvaged after producer verify-fail, fixed reportUnnecessaryIsInstance/Cast +
            ruff-format nit). Gates green locally: make check-pythinker-code + pytest tests/auth
            tests/ui_and_conv tests/cli. **PENDING LIVE VERIFY** (implicit + browser-JS + real DO acct).
      - [~] P3d(orig) — DigitalOcean. **SCOPE = FULL ROBUST BUILD (user-confirmed 2026-07-18).** OAuth
            IMPLICIT flow (response_type=token; token in URL fragment) → needs a NEW reusable
            `run_loopback_implicit_flow` helper in oauth_flows.py that serves an HTML-bootstrap page
            (inline JS reads location.hash, POSTs {access_token,expires_in,state} to a pinned-port
            /auth/token) — P3a's authorization-code loopback does NOT cover this. Token stored AS AN
            API KEY (no refresh, ~30d; re-login on expiry) → NO oauth ref / NO _refresh_token_for_ref
            branch. Provider openai_legacy → base_url https://inference.do-ai.run/v1, api_key=token.
            Dynamic model catalog: GET https://api.digitalocean.com/v2/gen-ai/models/routers (Bearer)
            → model_routers[].name → seed 'router:<name>' models at login (login still succeeds if
            fetch fails; seed none + warn). Verified constants: client_id
            b1a6c5158156caac821fd1b30253ca8acb52454a48fa744420e41889cb589f82; authorize
            https://cloud.digitalocean.com/v1/oauth/authorize; redirect http://localhost:1456/auth/callback;
            scope 'genai:read inference:query'. Recon ad3fed51 DONE (advisor-confirmed); split into
            two serialized lanes to fit the 30-min Codex cap:
              - Lane A (DISPATCHED, task kz00tnywe, Codex gpt-5.6-sol/high): add
                `run_loopback_implicit_flow` + `ImplicitAuthorization` + content-type writer to
                oauth_flows.py + tests. Self-contained, no src caller yet.
              - Lane B (after A integrates): new auth/digitalocean.py (DeepSeek storage template:
                LLMProvider openai_legacy, api_key=SecretStr(token), NO oauth) + __init__/platforms/
                shell/cli wiring (mirror xai) + tests. platforms.py skip-guard for managed:digitalocean.
                Empty-routers guard: still persist key + save_config, seed 0 models, guard default_model.
            **PENDING LIVE VERIFY** (implicit + browser-JS + real DO account — largely untestable offline).
      - [ ] P3e — Snowflake Cortex (loopback PKCE).
- [ ] P4 — API-key providers: batch the models.dev env-keyed providers through the P1 catalog.
- [ ] P0/P5 — Registry refactor (only if registry-first chosen; else optional last).

Non-negotiables per Codex spec: full `make check-pythinker-code && make test-pythinker-code` gate;
`## Unreleased` changelog line; deliberate snapshot updates; models.dev fail-open + no new dep;
attribution on ported files; root-cause robust design (no workarounds).

Out of scope (logged): `ultra` effort level (dropped, cap at max); any external endpoint beyond
models.dev without approval.

Review: _pending first delegation._

### Implementer-agent deepening: lighter/smarter/more robust (2026-07-17)

Architecture review found: every implementer spawn carries ~7,300 words of prompt (root
`system.md` + 71-line ROLE_ADDITIONAL), 35–45 role lines duplicated verbatim across
coder/verifier/review, and the `<coding_artifact>` contract living as 5 unbound copies
(prose ×2, regex parser in `tools/agent/__init__.py:1179`, prose consumers in
verifier/judge YAML) with `utils/artifacts.py::CodingArtifact` unused by the parser.
Serialized delegation lanes (Codex / GPT-5.6 Sol, max reasoning) via claude-architect:

- [x] T1 — Typed artifact contract (commits `70b62107`, `bb419368`): CodingArtifact is
      the single source of truth — schema-derived prompt block, strict fail-closed
      extraction (one end-of-message block, duplicate/undeclared keys rejected,
      present/missing/malformed), chain wired with truthful malformed surfacing,
      verifier receipt names files_changed, consumer-binding invariant tests.
- [x] T2 — Chain extracted to `tools/agent/implement_judge.py` (commit `43c9ebb6`);
      `__init__.py` 1755→1195 lines; full import surface preserved; hiddenimports
      snapshot updated; zero chain-test edits.
- [x] T3 — Leaf prompt profile (commits `13fc2d3c`, `598e09fd`, `9539f759`): system.md
      split into 12 Jinja partials with byte-identical root render (verified by
      fixed-args render diff against a pre-change baseline); `system_leaf.md` added;
      implementer/coder migrated — implementer prompt ~7,270 → ~4,240 words (−42%).
- [x] T4 — Remaining 10 roles migrated to the leaf profile (commit `455fa6ed`); all
      12 roster roles now render system_leaf.md; e2e snapshots unmoved.
- [x] T5 — Prose snapshots → semantic invariants (commit `372b292a`; −246/+107 lines);
      CHANGELOG Unreleased entries added; docs checked (only generic examples reference
      system.md — still valid). Full gate on composed tree: see review below.

#### Review: implementer-agent deepening (2026-07-18)

- Delivered via serialized claude-architect delegatePipeline lanes (Codex / GPT-5.6
  Sol; max reasoning where the 30-min attempt cap allowed, high on mechanical lanes),
  each candidate clean-room verified, reviewer-gated, integrated, and re-verified
  locally before commit.
- Outcomes: artifact contract has one owning module (deletion test passes); chain is
  a 547-line module with a compatibility re-export surface; all 12 leaf roles ship a
  ~40% smaller prompt with template-owned preamble/artifact sections; role-spec tests
  pin sections/flags/ownership instead of full prose.
- Deviations: three pipeline candidates were accepted with trivially repairable
  format/import-sort defects introduced by the pipeline's own fix stage (repaired
  locally with the repo formatter before commit, gates re-run); two "human-decision-
  required" gates were decided by the architect under the session's autonomous
  mandate, with the blocking "cannot-verify" findings resolved against runtime
  verification logs.
- Out of scope (observed, not touched): `test_default_agent.py` roster-line snapshot
  kept as-is; role type-name string coupling across `agent.yaml`/`subagents/core.py`
  noted in the architecture report as a candidate-4 leftover; `LaborMarket` remains a
  thin registry.

### PR #207 cancellation-state review fix (2026-07-15)

- [x] Execute `docs/superpowers/plans/2026-07-15-tool-execution-cancellation-state-rollback.md`
      with a failing regression test before production edits.
- [x] Keep cancelled/failed batch fingerprints out of committed dedup and consecutive-call state.
- [x] Preserve completed-result snapshots, callback suppression, bounded cancellation, timeout
      poisoning, and successful finalized summaries.
- [x] Run focused tests, core and CLI package gates, and `git diff --check`.
- [x] Bound cancellation-resistant async result callbacks under the StepResult ownership deadline.
- [x] Persist completed results and explicit completion-unknown markers when cancellation times out.
- [x] Connect engine late-drain ownership to bounded toolset/runtime cleanup.
- [x] Preserve caller cancellation until after MCP teardown completes.
- [x] Update the public architecture flow from per-call Soul dispatch to the batch engine path.
- [x] Complete task-scoped and whole-branch reviews with no open Critical/Important findings.
- [ ] Push the PR head, confirm the latest CodeRabbit review succeeds, reply to the remaining false
      positive with evidence, and resolve it through GitHub GraphQL.

Acceptance: after successful call A and cancelled call B, retrying B with A as the authoritative
prior context is not a cross-step duplicate and starts a fresh consecutive streak at 1. All PR
review threads are resolved only after the tested fix is present on GitHub.

#### Review: PR #207 cancellation-state review fix

- Root cause: `_ExecutionBatch._run()` finalized the engine step before watcher settlement. A
  cancelled or failed batch therefore committed fingerprints that `PythinkerSoul` correctly kept
  out of authoritative conversation state, causing a later retry to appear duplicated.
- Behavior: successful watcher settlement now commits the step exactly once. Cancellation or
  failure preserves previously committed fingerprints and completed-result snapshots while
  discarding only the current uncommitted step state before re-raising the original exception.
- TDD evidence: the new regression first failed because the retry reported
  `dedup_triggered is True` and consecutive count 2; after the fix it passed with no duplicate,
  consecutive count 1, and two real blocking-tool invocations. The final callback and
  engine/Soul cancellation regressions passed 92 focused tests across the core and CLI packages.
- Package evidence: `make test-pythinker-core` reported 433 passed. `make check-pythinker-core`
  passed Ruff, formatting, and Pyright with 0 errors; its repository-configured non-blocking `ty`
  step retained 62 existing provider/third-party diagnostics outside the changed files.
  `make check-pythinker-code` passed Ruff, formatting, Pyright with 0 errors, and blocking `ty`.
  The final cancellation/MCP set reported 57 passed. The main CLI suite reported 7,073 passed, 9
  skipped, and 1 expected xfail; separate `tests_e2e` reported 65 passed and 4 skipped. The
  VitePress documentation build and `git diff --check` also passed.
- Review: the task-scoped review approved the transactional rollback and the follow-up Pyright
  correction. A leading-underscore sibling call initially triggered strict `reportPrivateUsage`;
  the final `abort_step()` seam is documented and remains internal through the non-exported engine,
  without a suppression. The first whole-branch review correctly blocked publication on three
  Important cancellation-lifecycle gaps: callback settlement was unbounded, cancellation timeout
  skipped context lineage repair, and engine late-drain work was absent from runtime cleanup. The
  implementation now bounds all owned cancellation under one deadline, preserves truthful timeout
  lineage, joins retained engine work during cleanup, and documents the batch path. The latest-head
  CodeRabbit review then found that caller cancellation during engine cleanup could skip MCP
  teardown. A real cancellation regression reproduced the leak before the fix and now proves MCP
  closure precedes re-raising `CancelledError`. Fresh local re-review has no remaining
  Critical/Important issue; final GitHub re-review and closeout remain pending.

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

### Windows auto-update lifecycle redesign (2026-07-17)

Branch: `fix/windows-auto-update-lifecycle` (off origin/main @ c5c0bc92)

Root cause (3-lane scout, confirmed): silent startup auto-update on Windows runs the Inno
installer inline mid-session (`ui/shell/update.py:1239` -> `_run_native_installer` ->
`/SILENT /CLOSEAPPLICATIONS` + `sys.exit(0)`); shell swallows SystemExit, installer's 15s
WaitForLauncherExit times out, Restart Manager force-closes the session. No Windows
stage-and-promote path (macOS/Linux have one via atexit).

Scope (user): FULL redesign. Producer: Codex (GPT-5.6 Sol) via claude-architect MCP pipeline.

- [x] Core redesign (implemented by Claude directly — both claude-architect Codex lanes are
      broken in plugin v0.18.0, see blackbox/scratchpad.md): typed UpdateIntent
      (CHECK/STAGE_FOR_RESTART/INSTALL/INSTALL_AND_EXIT) through do_update/run_update_job;
      Windows staged-update manifest (atomic write, digest re-verified at apply, fail-closed
      discard); pre-session bootstrap apply in cli/__init__.py before config/session creation;
      config enum policy off|notify|download|apply_on_exit (default download, legacy bool
      true->download false->notify, env compatible); background task can never spawn an
      installer/package upgrade or raise SystemExit (contained in _run_silent_update_job);
      /update stages on Windows; standalone `pythinker update` keeps install-and-exit;
      apply_on_exit armed from both silent and /update stages; smoke check skipped for the
      Windows staged path (old exe would falsely certify it).
      -> verified: make check-pythinker-code green; full make test-pythinker-code green;
      codex (GPT-5.6 Sol, high) adversarial review — 4 real majors found and fixed
      (manifest supersession guard, /update apply_on_exit arming, off-mapping docstring,
      staged-path smoke check), 2 non-issues documented.
- [ ] Follow-up (Delegation B): packages/windows-installer/installer.iss tuning +
      multi-session concurrency guard (Session.acquire_ownership is fcntl no-op on Windows,
      session.py:53); /CLOSEAPPLICATIONS can affect other live Pythinker processes.
- [x] Full gate, PR opened.

Out of scope (log): broader Windows session locking beyond updater guard; install.ps1 mirrors;
`pythinker info` JSON `auto_update_config` changed bool->string (documented in changelog).
