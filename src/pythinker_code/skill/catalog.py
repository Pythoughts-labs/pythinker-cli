"""Deterministic discovery and exact resolution for skills."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import IntEnum, StrEnum
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
    tier: SkillRelevanceTier
    score: int
    reasons: tuple[str, ...]


class SkillRelevanceTier(IntEnum):
    EXACT_NAME = 0
    NAME_PHRASE = 1
    NAME_TOKEN = 2
    DESCRIPTION_TOKEN = 3


@dataclass(frozen=True, slots=True)
class SkillSearchMetrics:
    candidates_evaluated: int
    token_comparisons: int
    matches_sorted: int


@dataclass(frozen=True, slots=True)
class SkillSearchResult:
    matches: tuple[SkillMatch, ...]
    metrics: SkillSearchMetrics


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
    descriptions: tuple[str | None, ...] = ()


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
        self._compatibility_mapping = MappingProxyType(
            {normalize_skill_name(skill.name): skill for skill in ordered_skills}
        )
        self._skills_by_name = MappingProxyType(
            {
                normalize_skill_name(skill.name): skill.model_copy(deep=True)
                for skill in ordered_skills
            }
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
        return self._compatibility_mapping

    def search(self, query: str, *, limit: int) -> tuple[SkillMatch, ...]:
        """Return deterministic matches from the complete winning catalogue."""
        return self.search_with_metrics(query, limit=limit).matches

    def search_with_metrics(self, query: str, *, limit: int) -> SkillSearchResult:
        """Return matches with immutable, call-local deterministic work metrics."""
        if limit <= 0:
            return SkillSearchResult((), SkillSearchMetrics(0, 0, 0))
        query_tokens = _tokens(query)
        matches: list[SkillMatch] = []
        comparisons = 0
        for skill in self._skills_by_name.values():
            match, candidate_comparisons = _match_skill(skill, query_tokens)
            comparisons += candidate_comparisons
            if match is not None:
                matches.append(match)
        matches.sort(key=_match_sort_key)
        return SkillSearchResult(
            matches=tuple(matches[:limit]),
            metrics=SkillSearchMetrics(
                candidates_evaluated=len(self._skills_by_name),
                token_comparisons=comparisons,
                matches_sorted=len(matches),
            ),
        )

    def prompt_view(
        self,
        query: str,
        *,
        max_characters: int,
        explicit_names: Sequence[str] = (),
        active_names: Sequence[str] = (),
    ) -> SkillProjectionOutcome:
        """Build a bounded task view without reading skill bodies."""
        if max_characters <= 0:
            return SkillProjectionOutcome(
                status=SkillProjectionStatus.FAILED,
                view=None,
                reason_code="invalid_projection_budget",
            )

        ordered: list[SkillMatch] = []
        seen: set[str] = set()

        def _add_priority(name: str, reason: str) -> None:
            skill = self.resolve(name)
            if skill is None:
                return
            normalized = normalize_skill_name(skill.name)
            if normalized in seen:
                return
            seen.add(normalized)
            ordered.append(
                SkillMatch(
                    skill=skill,
                    tier=SkillRelevanceTier.EXACT_NAME,
                    score=500,
                    reasons=(reason,),
                )
            )

        for name in explicit_names:
            _add_priority(name, "explicit")
        for name in reversed(tuple(active_names)):
            _add_priority(name, "active")
        priority_count = len(ordered)

        for match in self.search(query, limit=len(self._skills_by_name)):
            normalized = normalize_skill_name(match.skill.name)
            if normalized not in seen:
                seen.add(normalized)
                ordered.append(match)

        total_count = len(ordered)
        selected: list[SkillMatch] = []
        for match in ordered:
            candidate_matches = tuple((*selected, match))
            candidate = _view(
                candidate_matches,
                total_count,
                overflowed_priority_count=max(0, priority_count - len(candidate_matches)),
            )
            if len(render_skill_prompt_view(candidate)) > max_characters:
                break
            selected.append(match)

        overflowed_priority_count = max(0, priority_count - len(selected))
        descriptions: list[str | None] = [None] * len(selected)
        for index, match in enumerate(selected):
            descriptions[index] = _concise_description(match.skill.description)
            candidate = _view(
                tuple(selected),
                total_count,
                overflowed_priority_count=overflowed_priority_count,
                descriptions=tuple(descriptions),
            )
            if len(render_skill_prompt_view(candidate)) > max_characters:
                descriptions[index] = None

        view = _view(
            tuple(selected),
            total_count,
            overflowed_priority_count=overflowed_priority_count,
            descriptions=tuple(descriptions),
        )
        rendered = render_skill_prompt_view(view)
        if len(rendered) > max_characters:
            return SkillProjectionOutcome(
                status=SkillProjectionStatus.FAILED,
                view=None,
                reason_code="projection_budget_too_small",
            )
        view = replace(view, rendered_characters=len(rendered))
        if overflowed_priority_count:
            return SkillProjectionOutcome(
                status=SkillProjectionStatus.DEGRADED,
                view=view,
                reason_code="priority_candidates_overflowed",
            )
        return SkillProjectionOutcome(
            status=SkillProjectionStatus.READY,
            view=view,
            reason_code=None,
        )

    def unavailable_diagnostic(self, name: str) -> SkillSourceDiagnostic | None:
        """Return a matching safe source diagnostic for an exact request."""
        requested = {normalize_skill_name(candidate) for candidate in _lookup_names(name)}
        return next(
            (
                diagnostic
                for diagnostic in self.diagnostics
                if normalize_skill_name(diagnostic.name) in requested
            ),
            None,
        )


_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_SCOPE_ORDER: Mapping[SkillScope, int] = {
    "project": 0,
    "user": 1,
    "extra": 2,
    "builtin": 3,
}


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(text.casefold()))


def _match_skill(
    skill: Skill,
    query_tokens: tuple[str, ...],
) -> tuple[SkillMatch | None, int]:
    name_tokens = _tokens(skill.name)
    description_tokens = _tokens(skill.description)
    comparisons = len(query_tokens) + len(name_tokens) + len(description_tokens)
    if query_tokens == name_tokens:
        return (
            SkillMatch(
                skill=skill,
                tier=SkillRelevanceTier.EXACT_NAME,
                score=len(name_tokens),
                reasons=("exact_name",),
            ),
            comparisons,
        )
    if name_tokens and _contains_sequence(query_tokens, name_tokens):
        return (
            SkillMatch(
                skill=skill,
                tier=SkillRelevanceTier.NAME_PHRASE,
                score=len(name_tokens),
                reasons=("name_phrase",),
            ),
            comparisons,
        )
    query_token_set = frozenset(query_tokens)
    name_overlap = len(query_token_set & frozenset(name_tokens))
    if name_overlap:
        return (
            SkillMatch(
                skill=skill,
                tier=SkillRelevanceTier.NAME_TOKEN,
                score=name_overlap,
                reasons=("name_token",),
            ),
            comparisons,
        )
    description_overlap = len(query_token_set & frozenset(description_tokens))
    if description_overlap:
        return (
            SkillMatch(
                skill=skill,
                tier=SkillRelevanceTier.DESCRIPTION_TOKEN,
                score=description_overlap,
                reasons=("description_token",),
            ),
            comparisons,
        )
    return None, comparisons


def _contains_sequence(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if len(needle) > len(haystack):
        return False
    return any(
        haystack[index : index + len(needle)] == needle
        for index in range(len(haystack) - len(needle) + 1)
    )


def _match_sort_key(match: SkillMatch) -> tuple[int, int, int, str, str]:
    skill = match.skill
    return (
        int(match.tier),
        -match.score,
        _SCOPE_ORDER[skill.scope],
        normalize_skill_name(skill.name),
        str(skill.skill_md_file.canonical()),
    )


def _concise_description(description: str) -> str:
    return " ".join(description.split())


def _view(
    matches: tuple[SkillMatch, ...],
    total_count: int,
    *,
    overflowed_priority_count: int = 0,
    descriptions: tuple[str | None, ...] = (),
) -> SkillPromptView:
    return SkillPromptView(
        matches=matches,
        total_count=total_count,
        omitted_count=max(0, total_count - len(matches)),
        overflowed_priority_count=overflowed_priority_count,
        rendered_characters=0,
        descriptions=descriptions or (None,) * len(matches),
    )


def render_skill_prompt_view(view: SkillPromptView) -> str:
    """Render a prompt view using safe metadata only."""
    lines = [
        (
            f"Task-relevant skills: {len(view.matches)} of {view.total_count} shown; "
            f"{view.omitted_count} omitted."
        ),
        "Call ReadSkill with a complete skill name before applying it.",
    ]
    if view.overflowed_priority_count:
        lines.append(
            f"Priority candidates omitted by the hard cap: {view.overflowed_priority_count}."
        )
    lines.append("Candidates:")
    descriptions = view.descriptions or (None,) * len(view.matches)
    for match, description in zip(view.matches, descriptions, strict=True):
        line = f"- `{match.skill.name}` [{match.skill.scope}]"
        if description:
            line = f"{line}: {description}"
        lines.append(line)
    return "\n".join(lines)


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
