## 9. Definition of Done

Walk this exit checklist before calling any coding task complete. Sessions with no file changes skip the diff and verification items rather than reporting them as blockers. Anything that applies but fails or cannot run goes under **BLOCKERS** — never into silence.

1. **Verification ran** per Rule 3, and the actual commands and results are stated in the response.
2. **Diff re-read** for scope creep, leftover debug output, commented-out code, placeholder text, broken imports, and accidental formatting churn.
3. **Edge cases named:** empty/null inputs, boundary values, error paths, and concurrent access considered; non-obvious ones listed in the response.
4. **Production guardrails checked:** the §6 pre-flight applied to production-facing code.
5. **Judge gate** run for qualifying deliverables (§5), or its checklist applied manually with the verification that actually ran stated.
6. **Claims match evidence:** every statement in the final summary is backed by something observed this session — a read, a diff, or command output.
7. **Task-spec checks walked:** when the work ran under a skill, spec, or plan with mandatory rules or a checklist, every item was checked against the artifact — mechanically where possible — and each compliance claim names the check that ran. Anything this environment could not execute or render (web pages, GUIs, external systems) is reported as unverified, never implied to work.
