# Lessons

Repo-specific rules accumulated from corrections and post-session reviews.
Format: trigger → rule.

## Subagent orchestration

- **When testing that subagent preparation failed before a prompt snapshot was written**, assert
  that `prompt.txt` remains empty rather than absent — `SubagentStore` intentionally pre-creates
  instance files during allocation, so path existence is not evidence of a snapshot write.

- **When dispatching subagents whose results you will immediately synthesize**
  (review + report, parallel analysis with no interleaved work), use
  **foreground fan-out** (`RunAgents` foreground mode) — results return inline,
  no polling or notification parsing. Reserve background mode for when the
  orchestrator has other work to do while children run.
- **When a non-blocking `TaskOutput` returns `retrieval_status: not_ready`**,
  do not snapshot-poll again. Either call `TaskOutput` with `block=true` and a
  realistic timeout, or continue other work until the completion notification
  arrives. Repeated non-blocking polls waste turns and tokens.
- **When deciding whether a background task is done**, trust only
  `status`/`retrieval_status` from a tool result. Notifications are a wake
  signal, not a state assertion — never claim "both agents completed" from a
  glimpsed notification.
- **When tempted to read a subagent's live output file mid-run**, don't. The
  `tasks/agent-*.md` log is only authoritative after the task is terminal;
  reading it early yields truncated content and wasted reasoning. Completion
  notifications now carry `output_path` + `output_size_bytes` — read the file
  after `terminal_reason: completed`.

## Review scoping

- **When asked to review/scan "the branch" and `git status` shows a dirty
  tree**, scope the diff as committed work PLUS the working tree
  (`git diff main` against the worktree, or `main...HEAD` + `git diff HEAD`),
  or explicitly state that uncommitted changes are excluded. `git diff
  main...HEAD` alone silently skips the newest code.
- **When writing a report to a path that already exists**, check it first —
  date-stamp the filename or append a run section instead of silently
  overwriting prior results.

## Bookkeeping honesty

- **Never narrate a bookkeeping action** ("let me update the todo list",
  "saving a note") without the corresponding tool call in the same turn.
  Narrated intentions that never execute are phantom state.

## Shell hygiene

- **When running repo commands**, the working directory persists between Bash
  calls — don't prefix every command with `cd <repo>`. Batch related read-only
  recon (e.g. `git log` + `git diff --stat`) into one call.

## Review orchestration

- **When asked to apply all PR review feedback**, wait for the review bot's status on the current
  head to become terminal, fetch unresolved thread-level state, and verify each recommendation
  against runtime contracts before editing; after the push, re-check the new head rather than
  treating the prior bot success as transferable.
- **When lower-authority text is framed beside an authoritative structured prompt block**, escape
  markup before interpolation and assert there is exactly one authoritative boundary block and that
  it remains last; ordering alone does not prevent a forged earlier block.

- **When running review/security subagents**, use the project-scoped agents in
  `.claude/agents/` (global `~/.claude/agents/security-reviewer.md` and
  `planner.md` describe the *other* Pythinker project — FastAPI/Vue/Mongo —
  and produce phantom attack-surface analysis here).
- **When waiting on background agents**, make exactly one blocking
  `TaskOutput(block=true, timeout=600s)` call per agent — never interleave
  non-blocking polls or read prior sessions' task logs.
- **When deep-scanning**, run `/deep-scan`: pin the base SHA via
  `git merge-base`, launch both reviewers in one parallel block, verify every
  High/Medium finding against the real code before reporting, and write the
  report to a dated, sha-suffixed file (never overwrite).

## Dependency & docs research

- **When checking library versions**, registries (PyPI JSON API / `npm view`)
  are the only source of truth; docs MCPs are for migration notes and API
  usage only, after the delta is established. Verify "feature X added in
  version Y" claims against release notes before asserting them.
- **When recommending an upgrade**, first grep direct imports with
  `--include="*.py"` (excluding `external reference ` and `__pycache__`) — a dep with
  zero direct imports gets no API-migration advice — and read pin-reason
  comments / git blame before calling a pin an "upgrade opportunity".
- **Never claim an artifact was persisted** ("report saved", "todo updated")
  without having made the Write call. Promise → tool call → claim, in that
  order. Use `/dep-audit` for dependency reports.

## Layer discipline

- **When asked to "enhance the agent" in this repo**, the target is the
  pythinker product itself: `src/pythinker_code/` (prompts, agents/default/*,
  soul/slash.py, tool hints) and `.pythinker/prompts/` for custom commands —
  NOT `.claude/` config. Transcripts showing `~/.pythinker/sessions/` paths
  are pythinker runs; behavioral fixes belong in the product.

## Typed policy boundaries

- **When a deep module classifies trusted and untrusted contributions**, represent source lifecycle
  (`provided` / `not_applicable` / `failed`) and trusted metadata permissions in the initial typed
  contract; identifier-shape validation is sanitization, not source authorization.
- **When a persisted prompt fragment is deduplicated**, scope its committed identity to both the
  provider registration and the current history generation; rearm, compaction, and revert must
  invalidate the relevant identity, and acknowledgement must happen synchronously only after the
  durable history append completes.

## TUI prompt chrome

- **When hiding the first-load editable input row to prevent ghost prompts**,
  keep the empty card visible: `_turn_starting` and the live-view first-commit
  gate may suppress editable content, but the top border and `❯` row should
  remain visible so the prompt bar does not disappear while the agent loads.
- **When routing live preview text through the existing Markdown renderer**, verify unsupported
  constructs against the installed library before treating the renderer as a complete cleanup
  boundary. Rich renders HTML comments literally, so a preview that must hide them needs a narrow,
  fence-aware filter while malformed comments remain visible.
- **When narrowing a regex that strips whole-line delimited blocks (HTML comments, fences)**, a
  non-greedy `.*?` between the open and close delimiters can backtrack across an embedded closer and
  silently swallow visible text on a mixed line (`<!-- a --> text <!-- b -->` collapsed to `""`).
  Bound the body with a tempered token `(?:(?!-->).)*?` so a failed end-anchor simply fails the
  match. Then derive test assertions from the *anchored* semantics: a line-anchored stripper leaves
  a mixed prose+comment line fully intact (markers included), so asserting the markers vanish is
  wrong — that was a self-contradictory test spec the implementer correctly blocked on.

## Spec/profile consistency

- **When adding or tightening a permission gate** (network, MCP, shell,
  visibility), sweep EVERY agent spec under `agents/default/` for instructions
  and `allowed_tools` entries that reference now-blocked tools — a spec that
  mandates a denied tool wastes steps on rejected calls and silently disables
  its own feature. The reverse holds too: a new spec must be written against
  its actual `_SUBAGENT_PROFILES` entry (unmapped subagent types default to
  offline `read_only`).
- **When a user reports an identity/naming feature "not working"**, first
  pin which surface they are looking at: subagent NAME (codenames),
  subagent instance id (`a<hex8>`), and background TASK id (`agent-…`) are
  three different identities; only the task id appears in TaskOutput/TaskStop
  headers.

## Verification gates

- **When teardown awaits supervised internal work before closing external resources**, preserve a
  caller `CancelledError` but do not let it skip the remaining resource closures; capture the
  cancellation, complete the teardown sequence, then re-raise it.

- **When adding an internal method that a sibling class must call under strict Pyright**, do not
  assume a leading underscore is harmless merely because both classes share a module. Use a
  documented method on the non-exported internal type and preserve `reportPrivateUsage`; never add
  a suppression just to retain protected-member spelling.

- **When a repo-required skill is absent from the advertised Codex skill roots**, check the
  project-documented legacy skill roots (especially `~/.claude/skills/`) before reporting it as
  unavailable; an incomplete root search is not evidence that the skill is missing.

- **When running a gate command (make check, pytest, ruff) through a pipe or
  in the background**, the pipeline exit code is the LAST command's (e.g.
  `tail`), and background notifications report that masked code. Never claim
  a gate passed from a notification summary — read the gate's own output for
  its verdict line, or run it unpiped with `; echo "EXIT=$?"`.
- **When a PreToolUse gate denies with a claim that contradicts observable
  state** (e.g. "changelog empty" while it plainly isn't), debug the hook
  script itself before working around it. Two traps from the changelog-gate
  incident: (1) `cmd | grep -q` under `set -o pipefail` SIGPIPEs the producer
  once output exceeds the pipe buffer — a *successful* match reads as exit
  141, so do presence checks inside awk or with `grep -c`; (2) hooks match on
  the FULL Bash command text, so a debug payload containing the trigger
  substring (`gh pr create`) re-triggers the gate on your own debug command —
  split the substring (`"gh pr %s" create`) when reproducing.
- **When a color-assertion test passes locally but fails on CI** (or vice
  versa) with quantized SGR codes (`38;5;N` where `38;2;r;g;b` was expected),
  suspect Rich's per-instance ANSI memoization: `Style.render` caches its SGR
  string at FIRST render with whatever console color system was active, and
  value-equal combined styles (`style + bold`) are shared process-wide via
  `lru_cache`. Whichever test renders a style first (under the suite's
  TERM/COLORTERM) poisons every later console. Reproduce with
  `env -u COLORTERM pytest tests/ui_and_conv <target>`; fix by asserting on
  span Style objects (color triplets), never on rendered ANSI.
- **asyncio `Process.wait()` needs EOF on every pipe, not just child exit.**
  `wait()` resolves only in `_call_connection_lost`, gated on ALL pipe
  transports being disconnected. A bounded `stdout.read(n)` that leaves the
  reader flow-control-paused on a full buffer blocks EOF forever — so
  `kill(); await proc.wait()` deadlocks even though the child is dead
  (Linux pipe dynamics hit this deterministically; macOS rarely). After
  killing a child with stdout=PIPE, drain the stream to EOF before waiting.
