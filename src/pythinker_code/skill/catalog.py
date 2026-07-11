"""Deterministic discovery and exact resolution for skills."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from pythinker_host.path import HostPath

from pythinker_code.skill import (
    ScopedSkillsRoot,
    Skill,
    SkillDiscoveryIssue,
    SkillScope,
    discover_skills,
    normalize_skill_name,
)


@dataclass(frozen=True, slots=True)
class SkillMatch:
    skill: Skill
    score: int
    reasons: tuple[str, ...]


class SkillDiagnosticCategory(StrEnum):
    UNAVAILABLE = "unavailable"


class SkillSourceKind(StrEnum):
    ROOT = "root"
    DIRECTORY = "directory"
    FLAT_FILE = "flat_file"


@dataclass(frozen=True, slots=True)
class SkillSourceDiagnostic:
    name: str
    source_kind: SkillSourceKind
    source_id: str
    scope: SkillScope
    category: SkillDiagnosticCategory
    reason_code: str
    safe_reason: str


class SkillProjectionStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SkillPromptView:
    matches: tuple[SkillMatch, ...]
    total_count: int
    omitted_count: int
    overflowed_priority_count: int
    rendered_characters: int


@dataclass(frozen=True, slots=True)
class SkillProjectionOutcome:
    status: SkillProjectionStatus
    view: SkillPromptView | None
    reason_code: str | None


class SkillCatalog:
    """Own the winning skill index for a resolved root sequence."""

    def __init__(
        self,
        skills_by_name: Mapping[str, Skill],
        diagnostics: Sequence[SkillSourceDiagnostic],
    ) -> None:
        ordered_skills = sorted(skills_by_name.values(), key=lambda skill: skill.name)
        self._skills_by_name = MappingProxyType(
            {normalize_skill_name(skill.name): skill for skill in ordered_skills}
        )
        self.diagnostics = tuple(diagnostics)

    @classmethod
    async def discover(cls, roots: Sequence[ScopedSkillsRoot]) -> SkillCatalog:
        skills_by_name: dict[str, Skill] = {}
        diagnostics: list[SkillSourceDiagnostic] = []
        for scoped_root in roots:
            issues: list[SkillDiscoveryIssue] = []
            skills = await discover_skills(
                scoped_root.root,
                scope=scoped_root.scope,
                diagnostic_collector=issues.append,
            )
            for skill in skills:
                skills_by_name.setdefault(normalize_skill_name(skill.name), skill)
            diagnostics.extend(_public_diagnostic(issue, scoped_root.root) for issue in issues)
        return cls(skills_by_name, diagnostics)

    def resolve(self, name: str) -> Skill | None:
        for candidate in _lookup_names(name):
            skill = self._skills_by_name.get(normalize_skill_name(candidate))
            if skill is not None:
                return skill
        return None

    def exhaustive_mapping(self) -> Mapping[str, Skill]:
        """Return the immutable normalized compatibility mapping."""
        return self._skills_by_name

    def search(self, query: str, *, limit: int) -> tuple[SkillMatch, ...]:
        """Defer relevance ranking until the Task 3 policy is implemented."""
        raise NotImplementedError("Skill search ranking is deferred to Task 3.")

    def prompt_view(self, query: str, *, max_characters: int) -> SkillProjectionOutcome:
        """Report that bounded projection remains deferred to Task 3."""
        return SkillProjectionOutcome(
            status=SkillProjectionStatus.FAILED,
            view=None,
            reason_code="skill_projection_deferred",
        )


def _lookup_names(name: str) -> tuple[str, ...]:
    raw_name = name.strip()
    if not raw_name:
        return ()
    if ":" not in raw_name:
        return (raw_name,)
    suffix = raw_name.rsplit(":", 1)[-1].strip()
    prefix = raw_name.split(":", 1)[0].strip()
    return tuple(dict.fromkeys(candidate for candidate in (raw_name, suffix, prefix) if candidate))


def _public_diagnostic(issue: SkillDiscoveryIssue, root: HostPath) -> SkillSourceDiagnostic:
    return SkillSourceDiagnostic(
        name=issue.name,
        source_kind=SkillSourceKind(issue.source_kind),
        source_id=str(issue.path.relative_to(root)) or ".",
        scope=issue.scope,
        category=SkillDiagnosticCategory.UNAVAILABLE,
        reason_code=issue.reason_code,
        safe_reason=issue.safe_reason,
    )
