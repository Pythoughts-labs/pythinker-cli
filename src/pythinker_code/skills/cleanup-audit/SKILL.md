---
name: cleanup-audit
description: Whole-repo audit for over-engineering and accidental complexity. Scans the entire codebase (not just a diff) and returns a ranked, read-only list of what to delete, simplify, or replace with standard-library or platform equivalents. Use when the user asks to "audit the codebase", "find bloat", "what can I delete", or wants a repo-wide simplification pass. For a diff-scoped pass use `pythinker review diff --mode deslopify` instead. One-shot report; applies no fixes.
---

# Cleanup Audit

A read-only, whole-repo pass that hunts accidental complexity and over-engineering. The diff-scoped
version of this job already ships as `pythinker review diff --mode deslopify`; this skill is its
repo-wide complement — scan the whole tree, rank the biggest cut first.

Scope is complexity only. Correctness bugs, security holes, and performance belong to a normal
review or security pass — note them in one line if you trip over them, but do not chase them here.

## What to hunt

- Dead code, unused flexibility, and speculative features no caller needs.
- Hand-rolled logic the standard library already ships — name the function that replaces it.
- A dependency (or hand-written code) doing what the language, runtime, or framework already does.
- Single-implementation interfaces, one-product factories, wrappers that only delegate, a module
  that exports one trivial thing, dead flags and config nobody sets.
- The same logic spelled out long-hand where a shorter, equally clear form exists.

## How to work

1. Map before judging: read the tree, the manifest/lockfile, and entry points; use `Grep`/`Glob`
   (or `LSP` for references and call hierarchy) to confirm a thing is actually unused before
   proposing its deletion. A deletion proposed without checking callers is a guess.
2. Rank findings biggest cut first.
3. Apply nothing. This is a report.

## Output

One line per finding, ranked, each tagged and citing a path:

- `delete:` — dead code / speculative feature. Replacement: nothing.
- `stdlib:` — hand-rolled thing the standard library ships. Name the function.
- `native:` — dependency or code doing what the platform/framework already does. Name the feature.
- `yagni:` — abstraction with one implementation, config nobody sets, layer with one caller.
- `shrink:` — same logic, fewer lines. Show the shorter form.

Format: `<tag> <what to cut>. <replacement>. [path:line]`
End with an estimate: `net: ~-<N> lines, -<M> deps possible.` Nothing to cut: `Lean already.`

## Guardrails

Never propose cutting trust-boundary validation, error handling that prevents data loss, security
measures, or accessibility — minimal is not the same as unsafe. When a "simplification" would touch
one of those, leave it and say why. Verify each deletion candidate is truly unreferenced before
listing it; an audit that proposes deleting live code is worse than no audit.
