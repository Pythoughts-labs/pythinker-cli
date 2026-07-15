# Deterministic reviewer target resolution design

**Status:** Approved on 2026-07-15

## Problem

Reviewer subagents already receive a best-effort repository summary, but their actual review scope
is still expressed only in free-form prompt text. The model must infer whether to inspect local
changes, a branch diff, or one commit, and it may recompute a different merge base. The standalone
review engine resolves its diff before model execution, so the two review paths do not provide the
same deterministic scope.

The agent-mediated path needs a small preflight contract that turns a structured target into exact
Git identifiers and an authoritative prompt block before a child instance or background task is
created.

## Goals

- Give `review`, `code-reviewer`, and `security-reviewer` a structured review target.
- Resolve every fresh reviewer target to immutable commit anchors before model execution.
- Support automatic selection, uncommitted changes, a base branch, and a single commit.
- Fail explicitly on invalid or unresolvable explicit targets.
- Apply identical behavior to `Agent` and each `RunAgents` child.
- Keep generic repository orientation while preventing a second, conflicting merge-base hint.
- Preserve exact resolved anchors and explicit live-worktree semantics in prompt snapshots and
  background task payloads.
- Keep the implementation small, async, provider-independent, and shell-free.

## Non-goals

- Adding or changing an interactive `/review` command.
- Routing reviewer subagents through the standalone review engine.
- Generating or embedding the complete diff before dispatch.
- Changing reviewer rubrics, finding schemas, or final-response formats.
- Adding range, staged-only, file-glob, or arbitrary custom-prompt target modes.
- Changing non-reviewer subagent semantics beyond shared neutralization of prompt-visible Git
  metadata.
- Supporting repositories with no resolvable `HEAD` commit in this slice.

## Approaches considered

### 1. Structured target resolved before dispatch — selected

Add a small target model to the agent tools. Resolve it through async Git commands before allocating
the child, preserve the caller prompt and resolved target as separate transport fields, and compose
one authoritative prompt in the shared subagent preparation path.

This is explicit, testable, works for both agent tools, and keeps Git discovery outside the model.

### 2. Parse target instructions from free-form prompts — rejected

Recognizing phrases such as "review against main" would preserve the existing schema, but prompt
parsing is ambiguous, locale-dependent, and vulnerable to accidental or hostile text. It also
cannot provide a reliable validation boundary.

### 3. Reuse the standalone review engine as the dispatch layer — rejected

That engine materializes and validates patches for its own review workflow. Making it a prerequisite
for subagent dispatch would couple two separate execution systems, duplicate model-facing context,
and expand this small adoption item into review-engine integration.

## Public tool contract

The agent tool module gains one reusable Pydantic model:

```python
class ReviewTarget(BaseModel):
    kind: Literal["auto", "uncommitted", "base", "commit"] = "auto"
    ref: str | None = None
```

`Params` and `AgentRunConfig` each gain:

```python
review_target: ReviewTarget | None = None
```

The validation contract is:

- `commit` requires a non-blank `ref`.
- `base` accepts an explicit `ref`; when omitted, the resolver tries the repository's fixed default
  base candidates.
- `auto` and `uncommitted` reject `ref` because it would have no defined meaning.
- A supplied ref is at most 1,024 characters, has no leading/trailing whitespace, does not start
  with `-`, and contains no control character. Validation rejects it rather than normalizing it to
  a different revision.
- A fresh reviewer with no supplied target is treated as `auto`.
- A non-reviewer rejects a supplied target instead of silently ignoring it.
- A resumed agent rejects a supplied target. Its original resolved block is already persisted in
  its conversation and changing it mid-session would create two authorities.

`RunAgents` keeps targets per child rather than at batch level. Mixed batches remain possible, and
each reviewer can inspect a different scope. The requested target is included in the orchestration
fingerprint so approval reuse cannot conflate different requested scopes.

The reviewer-type set and ordered default-base candidates each have one shared definition used by
validation, resolution, and prompt composition. The implementation must not copy those lists into
the two tools or runners.

## Resolution model

Resolution returns an immutable value containing:

```python
@dataclass(frozen=True, slots=True)
class ResolvedReviewTarget:
    kind: Literal["uncommitted", "base", "commit"]
    prompt: str
    hint: str
```

The concrete value may also retain resolved full SHAs and the selected base ref for tests and prompt
rendering. Callers consume the rendered prompt and a short safe hint; they do not reconstruct Git
commands themselves.

All Git operations use the existing async host process boundary with argv elements, never a shell.
The current best-effort Git-context helper deliberately collapses command failures to `None`; the
resolver instead uses a checked wrapper at that same boundary so it can distinguish timeout,
non-zero exit, and unavailable Git while still exposing only safe categorized errors. Reads are
bounded. Timeout and cancellation paths make bounded kill, drain, and reap attempts only when a
process handle exists; cleanup failures never replace the primary failure, and a failed host kill
can leave the child unreaped.

Every user-provided ref must pass the shape rules above and resolve with
`git rev-parse --verify --end-of-options <ref>^{commit}`. The explicit option boundary remains even
after shape validation as defense in depth. Prompt-visible refs are bounded to 1,024 characters and
commit titles to 200 characters; both are control-normalized and escaped as data so repository
metadata cannot close the block or be interpreted as instructions. Read-only probes disable any
configured filesystem monitor, and rendered diff/show hints include `--no-ext-diff` and
`--no-textconv` so the prescribed inspection path does not invoke repository-configured external
processors.

### Auto selection

`auto` follows one deterministic policy:

1. Resolve `HEAD` to a full commit SHA and read worktree status.
2. Try `origin/main`, `main`, then `master` in that order. A candidate is eligible only when both
   the ref and its merge base resolve; `auto` continues past an ineligible candidate.
3. The first candidate with a resolvable merge base wins. If that merge base differs from `HEAD`,
   select `base`. This scope includes branch commits plus current tracked worktree/index changes;
   the prompt also tells the reviewer to inspect untracked files reported by status. If the winning
   candidate's merge base equals `HEAD`, do not inspect lower-priority, potentially stale refs.
4. Otherwise, if the worktree has staged, unstaged, or untracked changes, select `uncommitted`.
5. Otherwise, select `commit` for `HEAD`.

The resolved block and returned hint state which path `auto` selected. If no default base with a
merge base resolves, that fact is recorded before `auto` selects uncommitted changes or `HEAD`; it
is not presented as if a base comparison succeeded.

### Uncommitted target

The resolver records the full `HEAD` SHA and uses bounded probes to prove that at least one staged,
unstaged, or untracked path exists without materializing the diff. The prompt directs the reviewer
to inspect staged and unstaged tracked changes from `HEAD`, then inspect every relevant untracked
path reported by status. A clean worktree is an explicit empty-target error.

The recorded `HEAD` is immutable; the index and worktree are explicitly live. The target block says
`worktree_state: live` and warns that concurrent edits can change the inspected patch. Prompt
preparation rechecks `HEAD` immediately before model execution and fails if it moved after
resolution. It does not claim to freeze content during the review. Immutable patch semantics would
require a materialized snapshot and remain outside this slice.

### Base target

For an explicit ref, resolution is strict and never falls back to another branch. For an omitted
ref, the resolver tries `origin/main`, `main`, then `master` and records the selected candidate.

The resolver computes the full merge-base SHA between `HEAD` and the selected base. The prompt uses
that SHA as the sole comparison anchor and instructs the reviewer to inspect the diff from that
commit through the live index/worktree, plus relevant untracked files. As with uncommitted mode,
prompt preparation rejects a moved `HEAD` before model execution but does not freeze later worktree
content. If no base, merge base, or reviewable change resolves, dispatch fails explicitly.

### Commit target

The resolver canonicalizes the requested ref to a full commit SHA, resolves its ordered parent
SHAs, and reads its bounded one-line title as untrusted metadata. Current worktree changes are
outside this target.

- A root commit uses a root-safe `git show` form.
- A single-parent commit compares that parent with the target commit.
- A merge commit compares its first parent with the target commit and labels the scope as the net
  first-parent merge result, not a per-parent conflict analysis. The prompt includes all parent SHAs
  so this choice is visible.

## Prompt contract

The resolver renders a compact `<review-target>` block with:

- requested mode and ref, if any;
- resolved mode;
- full `HEAD`, target, and merge-base SHAs as applicable;
- selected base ref and explicit automatic fallback information;
- a plain-language scope boundary;
- exact safe Git command hints;
- an instruction that metadata values are data, not instructions;
- an instruction to report that the target is empty or inaccessible rather than substituting a
  different scope.

Every prompt-visible value collected from Git—working directory, remote/project, branch, status
entry, ref, commit subject, and recent-log subject—is length-bounded, control-normalized, and
escaped through one shared data renderer. Both `<git-context>` and `<review-target>` identify
repository metadata as untrusted data, never instructions. This shared hardening also protects
explorer prompts without changing their repository-orientation semantics.

For a fresh reviewer, the final prompt order is:

1. output-language instruction;
2. generic `<git-context>` orientation without its default merge-base line;
3. caller task prompt inside `<review-task>`;
4. the runtime-generated authoritative `<review-target>` block.

The final block explicitly wins for Git range and revision selection. The caller task may narrow
files or add a review rubric, but it cannot replace or expand the resolved Git target. Keeping the
runtime block last also prevents a conflicting base or fake `<review-target>` in the caller prompt
from becoming the final scope instruction.

`explore` keeps the current generic merge-base hint. Reviewer agents suppress that hint because an
explicit base target may intentionally differ from `origin/main`; emitting both would create two
conflicting scopes.

## Dispatch integration

The agent tool performs target validation and async resolution after the effective subagent type is
known but before its per-child start journal, instance creation, or background task creation. A
`RunAgents` batch journal may already exist, but a failed target never produces a child instance or
task record.

The original caller prompt and the resolved target remain separate internal fields through dispatch:

- `ForegroundRunRequest` and `SubagentRunSpec` carry the caller prompt plus an optional resolved
  target.
- The background task payload and runner carry the same two values.
- `prepare_soul` owns the single prompt-composition order and the pre-execution `HEAD` recheck.
- Foreground `SubagentStart` hooks continue to receive the original caller prompt, so the generated
  target cannot consume the existing 500-character hook preview.

This adds transport fields but no duplicate target logic to the foreground and background runners.
Background tasks persist both values for truthful recovery/audit, and both paths write the same
final prompt snapshot.

`RunAgents` passes each child's target into `Params`. A target failure produces an error for that
child without allocating its instance or task; other children continue under the batch tool's
existing failure-isolation contract.

The returned tool result includes the safe target hint for successful fresh reviewer launches so an
automatic choice or base fallback is visible to the parent. No new telemetry event or persisted
schema is required.

## Error contract

`ReviewTargetResolutionError` represents recoverable preflight failures. The tool converts it to a
`ToolError` with a stable brief and an actionable message. Categories include:

- not a Git repository or no resolvable `HEAD`;
- invalid target/ref shape;
- missing explicit commit or base ref;
- no default base candidate;
- no merge base;
- empty uncommitted/base target;
- Git command timeout or execution failure.

Explicit targets never degrade to a different target. `auto` may select its documented next policy
step, but the selected path and unavailable-base condition remain visible in the target block and
hint. Errors contain no prompt content, remote credentials, raw environment data, or unrestricted
Git stderr.

This deliberately replaces the adoption ledger's proposed model-side backup prompt for an
unresolved base. Asking the reviewer to recompute a merge base would recreate the nondeterminism
this change removes and could present a guessed scope as authoritative. A failed explicit base is
therefore an actionable preflight error; only `auto` may follow its documented selection policy.

## Test design

Tests are written before implementation and must fail for the missing behavior.

### Resolver tests

- `auto` selects a divergent branch base, dirty worktree, and clean `HEAD` commit in order.
- Default base candidates are tried in fixed order and the selected fallback is surfaced.
- The first candidate with a merge base wins; a candidate whose merge base equals `HEAD` prevents
  lower-priority stale refs from changing the mode.
- Explicit base resolution never falls back.
- Base mode records the exact full merge-base and `HEAD` SHAs.
- Uncommitted mode covers staged, unstaged, and untracked status and rejects a clean tree.
- Commit mode canonicalizes abbreviated refs, includes an escaped bounded title, and pins root,
  single-parent, and first-parent merge semantics.
- Blank, overlong, whitespace-padded, option-shaped, control-containing, missing, and non-commit
  refs fail explicitly.
- Non-repository, unborn repository, missing base, missing merge base, timeout, and empty-target
  paths return typed failures.
- Strict Git probes bound captured output, disable configured filesystem monitors, and kill and reap
  timed-out processes.
- Hostile branch names, paths, project names, refs, and commit subjects cannot break out of either
  runtime Git block or become instructions.

### Tool and runner tests

- `Agent` accepts targets only for the three reviewer types.
- A fresh reviewer with no target resolves `auto`.
- Resume with a target fails; resume without one does not add a second target block.
- Resolution failure creates no foreground instance or background task.
- Foreground and background prompt snapshots contain the same resolved block.
- `RunAgents` forwards per-child targets, includes them in the fingerprint, supports mixed batches,
  and isolates one child's target error.
- Reviewer prompts contain one authoritative target after generic context; explorer orientation
  remains unchanged apart from metadata neutralization.
- A conflicting caller range or fake target block cannot override the final runtime target.
- Foreground start hooks still receive the original caller prompt.
- A moved `HEAD` between resolution and model start fails; live worktree edits are labeled rather
  than misrepresented as an immutable snapshot.

### Verification gates

- Focused resolver, Git-context, agent-tool, background-runner, and subagent-core tests.
- `make check-pythinker-code`.
- `make test-pythinker-code`, including `tests_e2e`.
- `git diff --check` and deliberate review of any schema or snapshot changes.

## Compatibility, documentation, and rollout

The new tool fields are optional, so saved agent records and background task payloads need no
migration. Existing fresh reviewer calls intentionally become deterministic through the `auto`
default. Resume continues from its persisted conversation without re-resolving repository state.

Implementation updates the agent tool description with concise target examples and adds an
`## Unreleased` changelog entry because shipped source behavior changes. It does not edit review CLI
documentation or advertise `/review` support.

The change ships without a feature flag. If it must be rolled back, revert the target fields,
resolver, prompt composition, and tests together; do not retain a second model-inferred fallback
path.

## Acceptance criteria

- Every fresh reviewer receives one runtime-generated deterministic target block, after its task
  prompt, with immutable commit anchors and truthful live-worktree labeling.
- Full SHAs, not model-computed refs, define base and commit scopes.
- Explicit invalid or empty targets fail before child allocation.
- `Agent` and `RunAgents`, foreground and background, share the same semantics.
- Generic Git context cannot contradict the authoritative review target.
- Existing non-reviewer execution and resume behavior remains unchanged; explorer metadata is only
  neutralized for prompt safety.
- Focused and full repository gates pass with no new dependency.
