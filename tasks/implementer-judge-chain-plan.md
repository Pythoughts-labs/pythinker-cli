# Plan: auto-chain `implementer` → `judge` with the **judge** minimum-diff rubric

**Goal:** make the agent build flow automatically invoke `implementer` for scoped
edits and `judge` for the final quality gate, with the judge explicitly applying
a *minimum-diff* rubric so the diff is the smallest rung of the reduction ladder
that solves the brief — not just a working one.

**Branding rule (HARD):** the words "ponytail", `PONYTAIL`, the
`ponytail:` convention marker, and any upstream-host identifiers **must not
appear** in Pythinker source, comments, commit messages, user-facing copy, or
CHANGELOG entries. Internally we borrow the rule *content* from upstream and
reframe it as the **judge** lens (Pythinker's quality-gate vocabulary). The
upstream origin is recorded only in this plan document — never in source,
comments, commits, or user-visible text.

**Source of truth for the rule content:** `blackbox/pythinker-judge/` is the
upstream skill repo. The Pythinker codebase treats it as **read-only upstream
data**, not as a vendored library. Key artifacts we read from it (and never
copy verbatim into user-facing copy):

- `blackbox/pythinker-judge/skills/ponytail/SKILL.md` — the ladder, the
  minimum-diff rules, the convention marker. We rebrand to `judge:` in any code
  we ask the model to emit, and to "judge lens" / "minimum-diff rubric" in
  user-facing text.
- `blackbox/pythinker-judge/skills/ponytail-review/SKILL.md` — over-engineering
  review checklist, reframed as the **judge over-engineering review** skill.
- `blackbox/pythinker-judge/hooks/ponytail-instructions.js` — only the *shape*
  of the prompt-builder is referenced; we do not import the JS module.

## What already exists (so we don't rebuild it)

- `src/pythinker_code/agents/default/agent.yaml` — registers 12 subagents,
  including `implementer` (`./implementer.yaml`) and `judge` (`./judge.yaml`).
- `src/pythinker_code/agents/default/implementer.yaml` — scoped-edit specialist
  that emits a `<coding_artifact>` block. Already has a `Context Gate`,
  `Workflow`, `Untrusted Content`, and `Role Exit Checklist`. Output contract is
  `SUMMARY / EVIDENCE / CHANGES / RISKS / BLOCKERS` + `<coding_artifact>`.
- `src/pythinker_code/agents/default/judge.yaml` — offline, read-only LLM-as-
  judge. Rubric: evidence, currency, fidelity, verification, safety, scope,
  production guardrails, findings quality. Output contract is
  `SUMMARY / EVIDENCE / REQUIRED FIXES / ADVISORY / BLOCKERS` with a leading
  `PASS` / `NEEDS_WORK` / `BLOCKED` verdict.
- `src/pythinker_code/agents/default/system.md` §5 (Tools & Orchestration) —
  currently tells the parent to call `judge` manually before delivering
  high-stakes work. §4.4 (Implementation) tells the parent to use `coder` /
  `implementer` for scoped edits.
- `src/pythinker_code/tools/agent/__init__.py` — `RunAgents` tool (line 760) is
  the existing multi-agent orchestrator. Already supports per-child prompts
  and a fingerprint for change detection.
- The judge subagent is **read-only** (`exclude_tools` strips write tools) and
  the implementer is **write-allowed**. Their tool profiles are the right
  boundary — keep them.

## The gap

1. `implementer` and `judge` are *manual* — the parent has to remember the
   sequence. Easy to forget; the §5 "Judge gate" trigger list is prose, not
   enforcement.
2. Neither subagent's system prompt mentions the ponytail ladder. A junior
   implementer will over-build; the judge has no rubric dimension for "did the
   diff take rung 1–6 first?".

**Honest note on gap #1:** the new tool reduces friction but does not
eliminate this gap — the parent still chooses `ImplementAndJudge` over bare
`implementer`, and the enforcement is still prose in system.md. Gap #1 is
relocated one level up. If hard enforcement is ever needed, the right fix is
to call judge from within `implementer.yaml`'s exit checklist — that would
close it structurally.

## Plan (3 layers, ~10 file changes)

### Layer 1 — judge adopts the ponytail ladder as a rubric dimension
**File:** `src/pythinker_code/agents/default/judge.yaml`

Add one block to `ROLE_ADDITIONAL` after the existing `Workflow` rubric list:

```text
- Minimum-diff: the diff takes the smallest rung of the reduction ladder
  (skip-need → reuse-stdlib → use-native → use-installed-dep → one-line →
  minimum) before adding new code; no abstractions, dependencies, config
  keys, files, or error paths that the brief did not ask for. New
  dependencies require a one-line justification; new config keys require
  a one-line consumer.
```

Add to the same role's `Context Gate` requirement: the parent's packet must
include the implementer's `<coding_artifact>` block (already does, but make it
explicit). No new tools, no new permission — the judge stays offline and
read-only.

**Skip:** a full mode-switcher (`lite`/`full`/`ultra` for the judge). The judge
applies the ladder uniformly; the *implementer* is the one that gets mode
toggles if we ever want them (Layer 3, optional).

### Layer 2 — automatic `implementer → judge` chain tool
**File:** `src/pythinker_code/tools/agent/__init__.py`

Add a new `ImplementAndJudgeTool(CallableTool2[…])` (call name
`ImplementAndJudge`), roughly 80 lines. It calls the underlying `AgentTool`
**twice sequentially** — implementer first, then judge with the first agent's
output baked into the packet. It does **not** wrap `RunAgents`; `RunAgents`
runs children concurrently via `asyncio.gather` and has no mechanism for
feeding one child's output into the next. The chain tool is the load-bearing
piece — the parent should call this instead of `Agent: implementer` +
`Agent: judge`.

Shape:

```python
class ImplementAndJudgeParams(BaseModel):
    brief: str                              # the change the user asked for
    scope: list[str] = []                   # allowed paths
    acceptance: list[str] = []              # pass conditions
    base_prompt: str | None = None          # shared across both children
    implementer_model: str | None = None    # default = parent model
    judge_model: str | None = None          # default = parent model
    max_revisions: int = 1                  # 0 = single pass, 1 = auto-revise on NEEDS_WORK
```

Pipeline:
1. Call `implementer` with the brief + scope + acceptance. Capture the
   `<coding_artifact>` block from the final message.
2. Build the judge packet: original brief, `git diff` (scoped to `scope`),
   the implementer's full output, and the artifact.
3. Call `judge` with that packet. Capture the verdict line (`PASS` /
   `NEEDS_WORK` / `BLOCKED`) and the `REQUIRED FIXES` section.
4. If `max_revisions >= 1` and verdict is `NEEDS_WORK`: re-invoke `implementer`
   with the judge feedback appended under a new `## Revision brief` section,
   re-judge, and stop. Cap at 2 implementer invocations total to keep
   deterministic.
5. If verdict is `BLOCKED`: stop. Return the packet and the BLOCKERS list.
6. Return a single `ToolReturnValue` with the implementer's `CHANGES` +
   `<coding_artifact>` + the judge verdict + (if revision happened) the
   revision trail. Do **not** silently swallow the verdict; the parent
   surfaces it.

Wire it into `default/agent.yaml` as one more `tools:` entry:
`pythinker_code.tools.agent:ImplementAndJudge`.

**Skip:** building a general "chain DSL." One chain, hard-coded, 80 lines, no
config layer. If we need a second chain later, extract then.

### Layer 3 — system prompt + default tool wiring
**File:** `src/pythinker_code/agents/default/agent.yaml`

Add the new tool:
```yaml
    - "pythinker_code.tools.agent:ImplementAndJudge"
```

**File:** `src/pythinker_code/agents/default/system.md`

Two surgical edits:

- §4.4 *Implementation* (add one paragraph at the end of the section): for
  non-trivial code changes, use `ImplementAndJudge` instead of calling
  `implementer` and `judge` separately. Mention the auto-revise-on-`NEEDS_WORK`
  behavior and the 2-implementer cap.
- §5 *Tools & Orchestration* — replace the manual "Judge gate" bullet with:
  "Default to `ImplementAndJudge` for non-trivial scoped edits; reserve a bare
  `judge` call for non-implementation reviews (reports, audits, answers)."

**Skip:** changing the `coder` subagent. `coder` is the older generalist
("general software-engineering work when the brief still needs judgment") and
the system prompt already says to prefer `implementer` for scoped edits. Leave
`coder` as the broad-fallback for ambiguous briefs.

### Default-on, bundled skill content

**Files (new, ~30 lines of markdown each, no JS):**

- `src/pythinker_code/skills/judge-minimum-diff/SKILL.md` — the bundled rule
  content. Frontmatter: `name: judge-minimum-diff`; `description: Reduction
  ladder and minimum-diff checks the Pythinker judge subagent applies to
  every non-trivial diff.` Body: the 7-rung ladder, reframed in Pythinker's
  voice; the `judge:` convention marker; the "When NOT to be lazy" guardrails
  (input validation, error handling, security, accessibility, hardware
  calibration). No mode-switch table; the judge applies the full ladder.
- `src/pythinker_code/skills/judge-overengineering-review/SKILL.md` — the
  review checklist the parent runs via `/skill:judge-overengineering-review`.
  Same rebranding.

Both files are **static, manually-authored Pythinker-branded markdown** — no
generation script, no coupling to upstream normalization functions, no version
constant. They are written once and updated with Pythinker releases when the
rubric content changes. The judge subagent loads them via `ReadSkill` the same
way it loads every other skill; they are offline-safe with no network
dependency.


## Files touched

| Path | Change |
|---|---|
| `src/pythinker_code/agents/default/judge.yaml` | add "Minimum-diff" rubric dimension; require `<coding_artifact>` in packet |
| `src/pythinker_code/agents/default/agent.yaml` | register `ImplementAndJudge` tool |
| `src/pythinker_code/agents/default/system.md` | §4.4 + §5 doc update |
| `src/pythinker_code/tools/agent/__init__.py` | add `ImplementAndJudgeTool` (~80 lines, calls `AgentTool` twice sequentially) |
| `src/pythinker_code/skills/judge-minimum-diff/SKILL.md` (new) | bundled default rule content |
| `src/pythinker_code/skills/judge-overengineering-review/SKILL.md` (new) | bundled default review skill |
| `tests/test_judge_branding.py` (new) | assert zero upstream-brand mentions in `src/pythinker_code/` and `skills/` |
| `tests/test_implement_judge_chain.py` (new) | chain test: PASS / NEEDS_WORK revises / BLOCKED stops |
| `tests/utils/test_pyinstaller_utils.py` | add bundled skill paths to `datas` |
| `CHANGELOG.md` | add `## Unreleased` bullet for the chain + rubric addition |

Total: **4 new files, 6 edits.**

## Verification

Per the pre-PR gate in `AGENTS.md`:

1. `make check-pythinker-code && make test-pythinker-code` (full, not partial).
2. New chain test passes deterministically with a stub model.
3. Snapshot tests under `tests/test_pyinstaller_utils.py` still pass — the
   new tool is `pythinker_code.tools.agent:ImplementAndJudge` and may need to
   be added to `hiddenimports`.
4. Existing `judge` tests still pass — the rubric addition is additive, the
   verdict contract is unchanged.
5. Manual smoke: trigger a non-trivial change in a real session; observe the
   implementer → judge sequence and the auto-revise path.

## Non-goals (deliberate)

- **No general chain DSL.** One hard-coded chain. If we need a second, extract
  then — YAGNI.
- **No mode switcher on the judge.** The ladder applies uniformly; mode
  belongs on the implementer if anywhere, and we don't have a use case yet.
- **No changes to `coder`, `code-reviewer`, `review`, or `security-reviewer`.**
  They keep their current roles; `ImplementAndJudge` is a new path for scoped
  implementation only.
- **No telemetry.** Per `AGENTS.md` "no new telemetry without explicit
  maintainer approval" — and the chain tool's revisions are observable in the
  parent's transcript already.
- **No auto-update plugin.** The bundled SKILL.md files work offline and ship
  with Pythinker releases. A session-start network fetch to an external repo
  adds latency, coupling, and a new module tree for zero proven benefit — the
  rubric is prompt content, not a security patch. Add the refresh mechanism
  only if upstream churn proves painful.
- **No user-facing mention of the upstream brand.** Skill names are
  `judge-minimum-diff` and `judge-overengineering-review`; the convention
  marker in generated code is `judge:`; CHANGELOG prose names only Pythinker
  features. No upstream URL appears in user-visible text.

## Delivery

The rule content ships inside the Pythinker package as static, Pythinker-branded
markdown files at `src/pythinker_code/skills/judge-minimum-diff/SKILL.md` and
`src/pythinker_code/skills/judge-overengineering-review/SKILL.md`. Both files
are authored manually — no generation script, no upstream coupling. They are
what `tests/test_judge_branding.py` asserts on (zero upstream-brand mentions).

Updates come with Pythinker releases. If upstream churn ever becomes painful,
a refresh mechanism can be added then.

## Risk register

- **Chain approval flow:** `ImplementAndJudgeTool` calls `AgentTool` twice
  sequentially. Each call goes through its own approval; the fingerprint is
  per-invocation of the chain tool (brief + scope + revision index), so a
  retry-with-revision gets a distinct fingerprint and doesn't reuse the first
  call's approval. Adapt the `_run_agents_fingerprint` shape (line 697) to
  include the revision index.
- **Verdict parsing fragility:** the judge's verdict is the first word of
  `SUMMARY`. Parse defensively — match `^PASS\b`, `^NEEDS_WORK\b`, `^BLOCKED\b`
  case-insensitively, ignore any preamble, fail-closed if no verdict is
  detected (return `BLOCKED` upstream).
- **Auto-revise loop:** the 2-implementer cap is non-negotiable. If
  implementer still returns `NEEDS_WORK` after revision, surface the
  contradiction to the parent and stop.
- **Untrusted content:** the judge's prompt now embeds the implementer's full
  output. Re-state the `Untrusted Content` rule in the chain tool's wrapper
  so the judge treats the implementer's text as data, not as instructions.
- **Branding leakage in bundled skill content:** the SKILL.md files are the
  most likely place for an upstream mention to slip in. Pin them with
  `tests/test_judge_branding.py` that scans the entire `src/` and `skills/`
  tree for the brand regex and fails on any hit.
