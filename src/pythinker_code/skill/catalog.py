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
    match_work_units: int
    sort_items: int
    sort_comparison_bound: int


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
        skill = self._resolve_internal(name)
        return skill.model_copy(deep=True) if skill is not None else None

    def _resolve_internal(self, name: str) -> Skill | None:
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
        result = self._search_internal(query, limit=limit)
        return SkillSearchResult(
            matches=tuple(_public_match(match) for match in result.matches),
            metrics=result.metrics,
        )

    def _search_internal(self, query: str, *, limit: int) -> SkillSearchResult:
        if limit <= 0:
            return SkillSearchResult((), SkillSearchMetrics(0, 0, 0, 0))
        query_tokens = _tokens(query)
        query_token_set = frozenset(query_tokens)
        matches: list[SkillMatch] = []
        work_units = 0
        exact_alias = _exact_search_alias(query, self._skills_by_name)
        exact_name: str | None = None
        if exact_alias is not None:
            exact_name = normalize_skill_name(exact_alias.name)
            matches.append(
                SkillMatch(
                    skill=exact_alias,
                    tier=SkillRelevanceTier.EXACT_NAME,
                    score=len(_tokens(exact_alias.name)),
                    reasons=("exact_alias",),
                )
            )
        for skill in self._skills_by_name.values():
            if normalize_skill_name(skill.name) == exact_name:
                continue
            match, candidate_work = _match_skill(skill, query_tokens, query_token_set)
            work_units += candidate_work
            if match is not None:
                matches.append(match)
        matches.sort(key=_match_sort_key)
        sort_items = len(matches)
        return SkillSearchResult(
            matches=tuple(matches[:limit]),
            metrics=SkillSearchMetrics(
                candidates_evaluated=len(self._skills_by_name),
                match_work_units=work_units,
                sort_items=sort_items,
                sort_comparison_bound=sort_items * max(0, sort_items - 1) // 2,
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
            skill = self._resolve_internal(name)
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

        for match in self._search_internal(query, limit=len(self._skills_by_name)).matches:
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
                view=_public_view(view),
                reason_code="priority_candidates_overflowed",
            )
        return SkillProjectionOutcome(
            status=SkillProjectionStatus.READY,
            view=_public_view(view),
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
    query_token_set: frozenset[str],
) -> tuple[SkillMatch | None, int]:
    name_tokens = _tokens(skill.name)
    description_tokens = _tokens(skill.description)
    exact, work_units = _sequence_equal(query_tokens, name_tokens)
    if exact:
        return (
            SkillMatch(
                skill=skill,
                tier=SkillRelevanceTier.EXACT_NAME,
                score=len(name_tokens),
                reasons=("exact_name",),
            ),
            work_units,
        )
    phrase, phrase_work = _contains_sequence(query_tokens, name_tokens)
    work_units += phrase_work
    if phrase:
        return (
            SkillMatch(
                skill=skill,
                tier=SkillRelevanceTier.NAME_PHRASE,
                score=len(name_tokens),
                reasons=("name_phrase",),
            ),
            work_units,
        )
    name_overlap = sum(token in query_token_set for token in frozenset(name_tokens))
    work_units += len(frozenset(name_tokens))
    if name_overlap:
        return (
            SkillMatch(
                skill=skill,
                tier=SkillRelevanceTier.NAME_TOKEN,
                score=name_overlap,
                reasons=("name_token",),
            ),
            work_units,
        )
    description_token_set = frozenset(description_tokens)
    description_overlap = sum(token in query_token_set for token in description_token_set)
    work_units += len(description_token_set)
    if description_overlap:
        return (
            SkillMatch(
                skill=skill,
                tier=SkillRelevanceTier.DESCRIPTION_TOKEN,
                score=description_overlap,
                reasons=("description_token",),
            ),
            work_units,
        )
    return None, work_units


def _sequence_equal(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[bool, int]:
    work_units = 1
    if len(left) != len(right):
        return False, work_units
    for left_token, right_token in zip(left, right, strict=True):
        work_units += 1
        if left_token != right_token:
            return False, work_units
    return True, work_units


def _contains_sequence(haystack: tuple[str, ...], needle: tuple[str, ...]) -> tuple[bool, int]:
    if len(needle) > len(haystack):
        return False, 1
    work_units = 1
    for index in range(len(haystack) - len(needle) + 1):
        matched = True
        for offset, token in enumerate(needle):
            work_units += 1
            if haystack[index + offset] != token:
                matched = False
                break
        if matched:
            return True, work_units
    return False, work_units


def _exact_search_alias(query: str, skills: Mapping[str, Skill]) -> Skill | None:
    raw_query = query.strip()
    if raw_query.startswith("$"):
        requested = raw_query[1:]
    elif raw_query.casefold().startswith("/skill:"):
        requested = raw_query[len("/skill:") :]
    elif ":" in raw_query and not any(character.isspace() for character in raw_query):
        requested = raw_query
    else:
        return None
    if not requested or any(character.isspace() for character in requested):
        return None
    for candidate in _lookup_names(requested):
        if skill := skills.get(normalize_skill_name(candidate)):
            return skill
    return None


def _match_sort_key(match: SkillMatch) -> tuple[int, int, int, str, str]:
    skill = match.skill
    return (
        int(match.tier),
        -match.score,
        _SCOPE_ORDER[skill.scope],
        normalize_skill_name(skill.name),
        str(skill.skill_md_file.canonical()),
    )


def _public_match(match: SkillMatch) -> SkillMatch:
    return replace(match, skill=match.skill.model_copy(deep=True))


def _public_view(view: SkillPromptView) -> SkillPromptView:
    return replace(view, matches=tuple(_public_match(match) for match in view.matches))


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
