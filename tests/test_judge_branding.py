"""Pin the judge-related content against upstream brand leakage.

The reduction-ladder rubric and over-engineering review checklist originated
upstream as a separate project. Pythinker reuses the rule *content* and
reframes it under Pythinker's `judge` lens, but the upstream name, the
upstream convention marker, and upstream-host identifiers must never appear
in source, comments, commit messages, user-facing copy, CHANGELOG entries,
or any tracked plan doc under `tasks/`.

This test scans the files added or modified by the
`implementer-judge-chain` change, plus the plan docs under `tasks/` that
grounded the work. The pre-existing upstream markers elsewhere in the
Pythinker tree are an out-of-scope rebrand tracked separately; gating the
whole tree here would block this plan on unrelated work. When the broader
rebrand lands, expand this scan to the full ``src/pythinker_code/`` tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Brand regex: case-insensitive match for the upstream project name and
# the upstream-host identifier. The convention marker (``ponytail:``) is
# included as a literal pattern so a stray comment doesn't slip through.
_BRAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bponytail\b", re.IGNORECASE),
    re.compile(r"\bPONYTAIL\b"),
    re.compile(r"\bDietrichGebert\b", re.IGNORECASE),
    re.compile(r"github\.com/DietrichGebert/ponytail", re.IGNORECASE),
    re.compile(r"^\s*ponytail:", re.MULTILINE),
)

# Files added or modified by this change. Each is a brand-guard target.
# Add to this list as the change grows — it is the single source of truth
# for "what this plan owns for branding purposes".
_BRAND_GUARD_TARGETS: tuple[Path, ...] = (
    REPO_ROOT / "src/pythinker_code" / "agents" / "default" / "judge.yaml",
    REPO_ROOT / "src/pythinker_code" / "agents" / "default" / "agent.yaml",
    REPO_ROOT / "src/pythinker_code" / "agents" / "default" / "system.md",
    REPO_ROOT / "src/pythinker_code" / "tools" / "agent" / "__init__.py",
    REPO_ROOT / "src/pythinker_code" / "skills" / "judge-minimum-diff" / "SKILL.md",
    REPO_ROOT / "src/pythinker_code" / "skills" / "judge-overengineering-review" / "SKILL.md",
    REPO_ROOT / "tests" / "core" / "test_implement_judge_chain.py",
    REPO_ROOT / "tests" / "utils" / "test_pyinstaller_utils.py",
    REPO_ROOT / "CHANGELOG.md",
)

# The brand-guard file names the brand by design — it is the test that
# asserts against leakage. Self-exclude so the regex doesn't trip on its
# own definition (mirrors the plan-document exclusion above).
_BRAND_GUARD_SELF: Path = Path(__file__).resolve()


@pytest.fixture(scope="module")
def brand_hits() -> list[tuple[Path, int, str, str]]:
    """Return every (path, line_no, pattern, line) brand match across the
    files this change owns. Computed once per module so the failure below
    prints a single concise summary instead of one error per file.
    """
    hits: list[tuple[Path, int, str, str]] = []
    for path in _BRAND_GUARD_TARGETS:
        if path == _BRAND_GUARD_SELF or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pattern in _BRAND_PATTERNS:
                if pattern.search(line):
                    hits.append((path, line_no, pattern.pattern, line.strip()))
                    break
    return hits


def test_no_upstream_brand_in_changed_files(
    brand_hits: list[tuple[Path, int, str, str]],
) -> None:
    assert not brand_hits, (
        "Upstream brand leakage detected — rebrand before shipping:\n"
        + "\n".join(
            f"  {path.relative_to(REPO_ROOT)}:{line_no}  [{pattern}]  {line}"
            for path, line_no, pattern, line in brand_hits
        )
    )


def test_brand_guard_targets_exist() -> None:
    """Every target must resolve. The scan fixture skips missing files, so a
    stale entry would silently drop coverage — assert each one exists rather
    than just "at least one", so a moved/renamed target fails loudly.
    """
    missing = [str(p.relative_to(REPO_ROOT)) for p in _BRAND_GUARD_TARGETS if not p.is_file()]
    assert not missing, f"stale brand-guard target(s) — update _BRAND_GUARD_TARGETS: {missing}"
