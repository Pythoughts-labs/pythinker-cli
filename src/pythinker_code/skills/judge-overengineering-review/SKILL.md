---
name: judge-overengineering-review
description: Over-engineering review checklist the parent runs before declaring non-trivial code changes done.
---

# Judge Over-Engineering Review

Use when reviewing a non-trivial diff for over-engineering before declaring
the work done. This skill is the parent-facing companion to the
`judge-minimum-diff` rubric — the parent runs it as a pre-flight pass, and
the `judge` subagent applies the same rubric as a quality-gate dimension.

## When to run

- Before declaring any non-trivial code change complete.
- During code review of a pull request that touches more than one file or
  introduces new abstractions.
- When a previous implementer or coder's diff feels heavier than the
  brief required.

For trivial one-line fixes or pure typo corrections, skip this skill —
YAGNI applies to review overhead too.

## Workflow

1. **Read the brief** the implementer was given. Note the explicit asks and
   the explicit non-goals.
2. **Read the diff** scoped to the allowed paths. Note every new file, new
   abstraction, new dependency, and new config key.
3. **Walk the reduction ladder** from `judge-minimum-diff`:

   - Did the diff take the highest rung that holds?
   - Is there stdlib / native / installed-dep reuse the diff missed?
   - Could the change be one line and isn't?

4. **Score each finding** under the rubric below.
5. **Report** in the parent-facing review block format: `path:line` for
   every finding, severity per §4.1 of the base prompt, the suggested
   smallest fix.

## Review checklist

A diff fails the over-engineering review when it has **any** of:

- **Speculative abstraction.** Interface with one implementation, factory
  for one product, abstract base class with one subclass, config layer for
  a value that never varies.
- **New dependency without justification.** The standard library, a
  native platform feature, or an already-installed dependency could have
  covered it. A new dep needs a one-line justification in the diff or PR
  body.
- **New config key without a consumer in the same diff.** A new key with no
  reader is dead configuration.
- **New file without brief backing.** A new module/folder the brief did not
  ask for, even if "tidier". Reuse the nearest neighbor first.
- **Error handling for impossible scenarios.** Validating deep in business
  logic for inputs the boundary already filtered; re-validating after a
  function the caller controls.
- **Boilerplate scaffolding "for later".** TODOs that fill a stub, fixtures
  the test doesn't use, helper modules with no callers yet.
- **Formatting churn outside the changed lines.** Reformat, rename, or
  rewrap on lines the brief did not ask to change.
- **Self-evident comments.** Restating what the next line of code already
  says. Comments only earn their keep when they document non-obvious
  algorithms, deliberate simplifications with a known ceiling, business
  rules, or genuine `TODO:` debt.
- **Clever over boring.** Clever is what someone decodes at 3am. Boring is
  the default; clever needs justification.
- **Length-as-quality.** A 500-line diff to replace a 50-line problem is a
  finding, not an accomplishment.

A diff **passes** when none of the above apply, every non-trivial design
choice has a one-line reason in the diff or PR body, and the deliberate
simplifications (if any) carry a `judge:` comment naming the ceiling.

## When NOT to flag

- **Trust-boundary validation.** Always keep — never call this
  over-engineering.
- **Error handling that prevents data loss** (rollback, idempotency,
  atomic state, listener cleanup). Always keep.
- **Security measures** (parameterized SQL, canonicalized paths, encoded
  output, secret hygiene). Always keep.
- **Accessibility basics**. Always keep.
- **User-explicit asks.** If the brief says "add a factory", add the
  factory. Don't second-guess explicit requirements.

## Output

A parent-facing review block:

```text
SUMMARY
OVER-ENGINEERING FINDINGS
- severity: path:line — what + why + smallest fix
NOT OVER-ENGINEERED (deliberate)
- path:line — why this is at the right rung
VERDICT
PASS | NEEDS_WORK | BLOCKED
```

`PASS` = diff is at the right rung; ship it. `NEEDS_WORK` = at least one
finding above applies; revise and re-review. `BLOCKED` = the brief and the
diff disagree about scope; clarify with the user before continuing.
