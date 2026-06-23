---
name: judge-minimum-diff
description: Reduction-ladder and minimum-diff checks the Pythinker judge subagent applies to every non-trivial diff.
---

# Judge Minimum-Diff Lens

Use when judging a code change, diff, or implementation report. This is the
rubric dimension the Pythinker judge subagent applies to every non-trivial
diff — the same ladder that lives in the base system prompt's §6 (Code
Standards), surfaced here as an explicit rubric so the judge, an
implementer doing pre-flight, or a reviewer running the over-engineering
skill apply it uniformly.

The judge applies the full ladder. There is no mode switch on the judge
(`lite` / `full` / `ultra`); the implementer is the role that would carry a
mode toggle if one is ever introduced.

## The ladder — walk it before writing code; stop at the first rung that holds

1. **Does this need to exist at all?** A speculative need is skipped, said so
   in one line. YAGNI is the highest rung.
2. **Does the standard library do it?** Use it.
3. **Does a native platform or framework feature cover it?** A database
   constraint over an app-level check, a built-in form control over a picker
   library, the language's own construct over a hand-rolled one — use it.
4. **Does a dependency already in the manifest solve it?** Use it; never add
   a new dependency for what a few lines cover.
5. **Can it be one line?** Make it one line.
6. **Only then** write the minimum code that works.

When two rungs both hold, take the higher one and move on. The ladder is a
reflex, not a research project. None of this overrides the guards below:
trust-boundary validation, error handling that prevents data loss, security,
and accessibility stay in even at rung 5.

## Minimum-diff rubric

A diff passes the minimum-diff check when **all** of these hold:

- The change takes the smallest rung of the ladder above before adding new
  code.
- There are **no abstractions** (no interface with one implementation, no
  factory for one product, no config layer for a value that never changes)
  that the brief did not ask for.
- There are **no new dependencies** unless the brief asked for one; when one
  is added, a one-line justification names why an already-installed
  dependency or the standard library couldn't cover it.
- There are **no new config keys** unless a consumer is named in the same
  diff (a value with no consumer is over-engineering).
- There are **no new files** unless the brief asked for them; reuse the
  nearest neighbor's module.
- There are **no error paths for impossible scenarios**; validate at
  boundaries, not deep in business logic.
- There is **no reformatting, renaming, or wrapping churn** outside the
  changed lines. Formatting churn is scope creep.
- **Comments only where they earn their keep**: non-obvious algorithms,
  deliberate simplifications whose ceiling matters, business rules, or
  genuine `TODO:` technical debt. No self-evident comments.

When a deliberate simplification has a known ceiling (a coarse lock, an
O(n²) scan, a naive heuristic), mark it with a `judge:` comment naming the
ceiling and the upgrade path. The judge reads these comments as evidence,
not as license.

## When NOT to be lazy

Never simplify away:

- Input validation at trust boundaries.
- Error handling that prevents data loss (rollback, idempotency, atomic
  state, listener cleanup).
- Security measures (parameterized SQL, canonicalized paths, encoded output,
  hand-off-crypto, secret hygiene).
- Accessibility basics (semantic HTML, alt text, focus order, keyboard
  reachability, contrast).
- Anything the user explicitly requested.

A junior-friendly check: would a senior engineer call this over-engineered?
If yes, simplify. If no, the diff is at the right rung.

## How the judge applies this rubric

When the judge receives an `implementer` packet:

- **Evidence**: the implementer's `<coding_artifact>` block is the
  implementer's claim; the judge verifies it against the diff.
- **Currency / Fidelity / Verification / Safety / Scope / Production
  guardrails / Findings quality**: per the standard judge rubric in the base
  prompt.
- **Minimum-diff** (this rubric): the judge checks every bullet under
  *Minimum-diff rubric* above. A NEEDS_WORK verdict is required when the
  diff adds any of the over-engineering shapes, even if the change works.

The verdict contract is unchanged: `PASS` / `NEEDS_WORK` / `BLOCKED` as the
first word of SUMMARY. The chain tool parses the verdict verbatim and
optionally re-invokes the implementer once on `NEEDS_WORK`.

## Hardware and physical-world caveat

Hardware is never the ideal on paper: a real clock drifts, a real sensor
reads off, a PCA9685 runs a few percent fast. Leave the calibration knob,
not just less code. The physical world needs tuning a minimal model can't
see.
