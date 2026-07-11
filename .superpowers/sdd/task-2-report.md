# Task 2 Report: Exhaustive SkillCatalog seam

## SUMMARY

Implemented the exhaustive `SkillCatalog` seam without Task 3 runtime, rendering, soul, or
`ReadSkill` integration. The catalogue owns one immutable normalized winning index, exact and
plugin-style alias resolution, deterministic exhaustive search, the exhaustive prompt projection
contract, and retained safe discovery diagnostics.

Discovery remains the parsing source. A narrow optional collector on `discover_skills` records
unreadable roots/sources and malformed metadata while preserving the existing list return type and
all legacy discovery callers.

## TDD EVIDENCE

Each behavior was introduced as a vertical red-green slice using real `tmp_path` filesystem state:

- Module and exact lookup RED: `uv run pytest -q tests/core/test_skill_catalog.py` failed during
  collection with `ModuleNotFoundError: No module named 'pythinker_code.skill.catalog'`.
  GREEN: the same command passed `1 passed` after the minimal catalogue and exact index were added.
- Case-insensitive lookup RED:
  `uv run pytest -q tests/core/test_skill_catalog.py::test_resolve_is_case_insensitive` failed
  because `resolve("dEpLoY")` returned `None`. GREEN: the catalogue normalized the requested name;
  the catalogue suite passed `2 passed`.
- Plugin-style alias RED:
  `uv run pytest -q tests/core/test_skill_catalog.py::test_resolve_accepts_plugin_style_alias`
  failed because `designer-skill:designer-skill` returned `None`. GREEN: normalized raw, suffix,
  and prefix candidates resolved through the same winning index; the suite passed `3 passed`.
- First-root precedence and compatibility mapping RED:
  `uv run pytest -q tests/core/test_skill_catalog.py::test_discover_keeps_first_root_winner_in_exhaustive_mapping`
  failed with missing `exhaustive_mapping`. GREEN: the immutable normalized mapping exposed the
  project-root winner; the suite passed `4 passed`.
- Reversed-input determinism RED:
  `uv run pytest -q tests/core/test_skill_catalog.py::test_search_is_deterministic_when_root_input_is_reversed`
  failed with missing `search`. GREEN: canonical name/path ordering produced identical
  `("alpha", "zeta")` output; the suite passed `5 passed`.
- Malformed-source diagnostics RED:
  `uv run pytest -q tests/core/test_skill_catalog.py::test_discovery_retains_unavailable_diagnostic_for_malformed_source`
  failed because the diagnostic types were absent. GREEN: discovery retained a structured
  `unavailable` diagnostic with safe reason code/path and no source contents; `1 passed`.
- Projection/status contracts RED:
  `uv run pytest -q tests/core/test_skill_catalog.py::test_prompt_view_exposes_frozen_exhaustive_projection`
  failed because `SkillProjectionStatus` was absent. GREEN: frozen match/view/outcome contracts and
  exhaustive READY projection were added; the catalogue suite passed `7 passed`.
- Package seam RED:
  `uv run pytest -q tests/core/test_skill_catalog.py::test_skill_catalog_is_exported_from_skill_package`
  failed because `SkillCatalog` was not exported. GREEN: the package re-export was added; the
  catalogue suite passed `8 passed`.

Final verification:

- `uv run pytest -q tests/core/test_skill_catalog.py tests/core/test_skills_prompt.py tests/tools/test_skill_tool.py`
  -> `28 passed, 1 warning`.
- `uv run pytest -q tests/core/test_skill_catalog.py tests/core/test_skill.py tests/core/test_skills_prompt.py tests/tools/test_skill_tool.py`
  -> `97 passed, 1 warning`.
- `make check-pythinker-code` -> Ruff/format passed, Pyright `0 errors`, Ty passed.
- `make test-pythinker-code` -> exit 0; the 6,679-item main suite completed, then
  `tests_e2e` reported `65 passed, 4 skipped`.
- `git diff --check` -> clean.

Warnings were limited to an existing Loguru dependency deprecation and pytest temporary-directory
cleanup warnings; neither came from the Task 2 code.

## CHANGES

- `src/pythinker_code/skill/catalog.py`: added frozen catalogue contracts, diagnostic/status enums,
  deterministic discovery/indexing, exact/alias resolution, exhaustive search/projection, and an
  immutable compatibility mapping.
- `src/pythinker_code/skill/__init__.py`: added the narrow discovery diagnostic collector while
  preserving existing callers and re-exported `SkillCatalog`.
- `tests/core/test_skill_catalog.py`: added real-filesystem coverage for every Task 2 contract.

## RISKS

- `discover_skills` now accepts an optional diagnostic collector. Existing callers are unchanged;
  collector exceptions intentionally propagate rather than silently discarding diagnostic failure.
- Search relevance tiers, character-cap admission, prompt rendering, runtime sharing, request-only
  injection, and `ReadSkill` unavailable/suggestion behavior remain intentionally deferred to Task 3.
- The exhaustive `prompt_view` exposes the approved seam but does not enforce Task 3's bounded
  admission policy.

## BLOCKERS

None.

## SELF-REVIEW

- Scope is limited to the three Task 2 implementation/test files plus this required report.
- No mocks, `Any`, type suppressions, new dependencies, telemetry, runtime wiring, or Task 3 behavior
  were introduced.
- Diagnostics use categorized safe reasons and never copy malformed skill contents.
- The collector preserves legacy discovery return values, ordering, and first-root behavior.
- Test guard: all tests assert caller-visible behavior against real filesystem state; no internal
  mocks, framework guarantees, or duplicate parameter-only scenarios were added.
- Pythinker guard: focused and full gates pass; changed lines trace to catalogue indexing,
  diagnostics, contracts, or their tests.

## SHA

This report is included in the Task 2 commit. Its authoritative SHA is the repository's current
`HEAD` (`git rev-parse HEAD`); embedding that hash inside the commit would change the hash itself.
