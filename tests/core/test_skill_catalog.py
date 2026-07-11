from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import pytest
from pythinker_host.path import HostPath

from pythinker_code.skill import ScopedSkillsRoot, SkillScope
from pythinker_code.skill import SkillCatalog as PublicSkillCatalog
from pythinker_code.skill.catalog import (
    SkillCatalog,
    SkillDiagnosticCategory,
    SkillProjectionStatus,
    SkillSourceKind,
    render_skill_prompt_view,
)


def _write_skill(root: Path, directory: str, *, name: str, description: str) -> None:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n",
        encoding="utf-8",
    )


def _root(path: Path, scope: SkillScope = "user") -> ScopedSkillsRoot:
    return ScopedSkillsRoot(
        root=HostPath.unsafe_from_local_path(path),
        scope=scope,
    )


def test_skill_catalog_is_exported_from_skill_package() -> None:
    assert PublicSkillCatalog is SkillCatalog


@pytest.mark.asyncio
async def test_resolve_finds_exact_skill_name(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "deploy", name="deploy", description="Deploy applications")

    catalog = await SkillCatalog.discover([_root(skills_root)])

    skill = catalog.resolve("deploy")
    assert skill is not None
    assert skill.description == "Deploy applications"


@pytest.mark.asyncio
async def test_resolve_is_case_insensitive(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "deploy", name="Deploy", description="Deploy applications")

    catalog = await SkillCatalog.discover([_root(skills_root)])

    assert catalog.resolve("dEpLoY") is not None


@pytest.mark.asyncio
async def test_resolve_accepts_plugin_style_alias(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root,
        "designer-skill",
        name="designer-skill",
        description="Design interfaces",
    )

    catalog = await SkillCatalog.discover([_root(skills_root)])

    assert catalog.resolve("designer-skill:designer-skill") is not None


@pytest.mark.asyncio
async def test_discover_keeps_first_root_winner_in_exhaustive_mapping(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    user_root = tmp_path / "user"
    _write_skill(project_root, "shared", name="shared", description="Project version")
    _write_skill(user_root, "shared", name="shared", description="User version")

    catalog = await SkillCatalog.discover(
        [_root(project_root, "project"), _root(user_root, "user")]
    )

    assert catalog.exhaustive_mapping()["shared"].description == "Project version"
    assert tuple(catalog.exhaustive_mapping()) == ("shared",)


@pytest.mark.asyncio
async def test_exhaustive_mapping_uses_legacy_global_skill_name_order(tmp_path: Path) -> None:
    first_root = tmp_path / "first-root"
    second_root = tmp_path / "second-root"
    _write_skill(first_root, "zeta", name="zeta", description="Zeta workflow")
    _write_skill(second_root, "alpha", name="alpha", description="Alpha workflow")

    forward = await SkillCatalog.discover([_root(first_root), _root(second_root)])
    reversed_catalog = await SkillCatalog.discover([_root(second_root), _root(first_root)])

    expected_names = ("alpha", "zeta")
    assert tuple(forward.exhaustive_mapping()) == expected_names
    assert tuple(reversed_catalog.exhaustive_mapping()) == expected_names
    assert tuple(skill.name for skill in forward.exhaustive_mapping().values()) == expected_names
    assert (
        tuple(skill.name for skill in reversed_catalog.exhaustive_mapping().values())
        == expected_names
    )


@pytest.mark.asyncio
async def test_discovery_retains_unavailable_diagnostic_for_malformed_source(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    malformed_dir = skills_root / "broken"
    malformed_dir.mkdir(parents=True)
    malformed_path = malformed_dir / "SKILL.md"
    malformed_path.write_text(
        "---\nname: broken\ntype: unsupported\nsecret: do-not-leak\n---\n",
        encoding="utf-8",
    )

    catalog = await SkillCatalog.discover([_root(skills_root)])

    assert catalog.resolve("broken") is None
    assert len(catalog.diagnostics) == 1
    diagnostic = catalog.diagnostics[0]
    assert diagnostic.name == "broken"
    assert diagnostic.source_kind is SkillSourceKind.DIRECTORY
    assert diagnostic.category is SkillDiagnosticCategory.UNAVAILABLE
    assert diagnostic.source_id == "broken/SKILL.md"
    assert str(tmp_path) not in diagnostic.source_id
    assert not Path(diagnostic.source_id).is_absolute()
    assert diagnostic.reason_code == "invalid_skill_metadata"
    assert "do-not-leak" not in diagnostic.safe_reason


@pytest.mark.asyncio
async def test_unreadable_flat_source_retains_unavailable_diagnostic(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    (skills_root / "unreadable.md").symlink_to(skills_root / "missing-target.md")

    catalog = await SkillCatalog.discover([_root(skills_root)])

    assert catalog.resolve("unreadable") is None
    assert len(catalog.diagnostics) == 1
    diagnostic = catalog.diagnostics[0]
    assert diagnostic.source_id == "unreadable.md"
    assert diagnostic.reason_code == "unreadable_skill_source"


@pytest.mark.asyncio
async def test_search_ranks_relevance_before_scope_with_stable_ties(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "exact", name="deploy", description="unrelated")
    _write_skill(skills_root, "phrase", name="release-deploy", description="unrelated")
    _write_skill(skills_root, "tokens", name="deploy-service", description="unrelated")
    _write_skill(skills_root, "description", name="alpha", description="deploy service safely")
    catalog = await SkillCatalog.discover([_root(skills_root, "builtin")])

    assert [match.skill.name for match in catalog.search("deploy", limit=10)] == [
        "deploy",
        "deploy-service",
        "release-deploy",
        "alpha",
    ]


@pytest.mark.asyncio
async def test_search_is_stable_under_reversed_insertion_order(tmp_path: Path) -> None:
    skills = {
        "zeta": _skill_for_catalog(tmp_path / "zeta" / "SKILL.md", "zeta", "release deploy"),
        "alpha": _skill_for_catalog(tmp_path / "alpha" / "SKILL.md", "alpha", "release deploy"),
    }

    forward = SkillCatalog(skills, ())
    reversed_catalog = SkillCatalog(dict(reversed(tuple(skills.items()))), ())

    assert forward.search("release deploy", limit=10) == reversed_catalog.search(
        "release deploy", limit=10
    )


def _skill_for_catalog(path: Path, name: str, description: str, scope: SkillScope = "user"):
    from pythinker_code.skill import Skill

    return Skill(
        name=name,
        description=description,
        dir=HostPath.unsafe_from_local_path(path.parent),
        skill_md_file=HostPath.unsafe_from_local_path(path),
        scope=scope,
    )


def test_prompt_view_prioritizes_explicit_then_newest_active_and_respects_hard_cap(
    tmp_path: Path,
) -> None:
    skills = {
        name: _skill_for_catalog(
            tmp_path / name / "SKILL.md",
            name,
            "x" * 4_000,
        )
        for name in ("implicit", "active-old", "active-new", "explicit-one", "explicit-two")
    }
    catalog = SkillCatalog(skills, ())

    outcome = catalog.prompt_view(
        "work on implicit with $explicit-one then /skill:explicit-two",
        max_characters=500,
        explicit_names=("explicit-one", "explicit-two"),
        active_names=("active-old", "active-new"),
    )

    assert outcome.view is not None
    assert [match.skill.name for match in outcome.view.matches] == [
        "explicit-one",
        "explicit-two",
        "active-new",
        "active-old",
        "implicit",
    ]
    rendered = render_skill_prompt_view(outcome.view)
    assert len(rendered) == outcome.view.rendered_characters
    assert len(rendered) <= 500
    assert "x" * 100 not in rendered
    assert str(tmp_path) not in rendered
    assert outcome.view.omitted_count == 0


def test_priority_overflow_is_degraded_and_never_truncates_names(tmp_path: Path) -> None:
    names = tuple(f"priority-skill-{index:03d}" for index in range(100))
    catalog = SkillCatalog(
        {
            name: _skill_for_catalog(tmp_path / name / "SKILL.md", name, "description")
            for name in names
        },
        (),
    )

    outcome = catalog.prompt_view(
        "task",
        max_characters=500,
        explicit_names=names,
    )

    assert outcome.status is SkillProjectionStatus.DEGRADED
    assert outcome.reason_code == "priority_candidates_overflowed"
    assert outcome.view is not None
    assert outcome.view.overflowed_priority_count > 0
    rendered = render_skill_prompt_view(outcome.view)
    assert len(rendered) <= 500
    assert all(match.skill.name in rendered for match in outcome.view.matches)
    rendered_names = {
        line.split("`", 2)[1] for line in rendered.splitlines() if line.startswith("- `")
    }
    assert rendered_names == {match.skill.name for match in outcome.view.matches}


def test_projection_budget_too_small_fails_without_oversized_view(tmp_path: Path) -> None:
    catalog = SkillCatalog(
        {"alpha": _skill_for_catalog(tmp_path / "alpha" / "SKILL.md", "alpha", "desc")},
        (),
    )

    outcome = catalog.prompt_view("alpha", max_characters=1)

    assert outcome.status is SkillProjectionStatus.FAILED
    assert outcome.reason_code == "projection_budget_too_small"
    assert outcome.view is None


def test_recall_fixture_and_warm_search_performance(tmp_path: Path) -> None:
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "skill_catalog_recall.json").read_text(
            encoding="utf-8"
        )
    )
    skills = {
        f"fixture-{index:04d}": _skill_for_catalog(
            tmp_path / f"fixture-{index:04d}" / "SKILL.md",
            f"fixture-{index:04d}",
            f"generic capability {index}",
        )
        for index in range(1_000)
    }
    for item in fixture:
        skills[item["name"]] = _skill_for_catalog(
            tmp_path / item["name"] / "SKILL.md",
            item["name"],
            item["description"],
        )
    catalog = SkillCatalog(skills, ())

    for item in fixture:
        names = {match.skill.name for match in catalog.search(item["query"], limit=8)}
        assert set(item["expected"]) <= names

    catalog.search("release deployment", limit=8)
    durations: list[float] = []
    for _ in range(25):
        started = time.perf_counter()
        matches = catalog.search("release deployment", limit=8)
        durations.append(time.perf_counter() - started)
        assert catalog.last_search_operation_count <= len(skills) * 3
        assert len(matches) <= 8
    assert statistics.median(durations) < 0.1


def test_root_and_subagent_runtime_share_catalogue_identity(runtime) -> None:
    child = runtime.copy_for_subagent(agent_id="child", subagent_type="coder")

    assert child.skill_catalog is runtime.skill_catalog
    assert child.skills == runtime.skill_catalog.exhaustive_mapping()
