from __future__ import annotations

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
async def test_search_is_explicitly_deferred_until_ranking_policy_exists(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "alpha", name="alpha", description="Alpha workflow")
    catalog = await SkillCatalog.discover([_root(skills_root)])

    with pytest.raises(NotImplementedError, match="Task 3"):
        catalog.search("workflow", limit=10)


@pytest.mark.asyncio
async def test_prompt_view_reports_deferred_without_fabricated_view(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "alpha", name="alpha", description="Alpha workflow")
    _write_skill(skills_root, "zeta", name="zeta", description="Zeta workflow")
    catalog = await SkillCatalog.discover([_root(skills_root)])

    outcome = catalog.prompt_view("workflow", max_characters=1_000)

    assert outcome.status is SkillProjectionStatus.FAILED
    assert outcome.reason_code == "skill_projection_deferred"
    assert outcome.view is None
