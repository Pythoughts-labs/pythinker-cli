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
async def test_search_is_deterministic_when_root_input_is_reversed(tmp_path: Path) -> None:
    alpha_root = tmp_path / "alpha-root"
    zeta_root = tmp_path / "zeta-root"
    _write_skill(alpha_root, "alpha", name="alpha", description="Alpha workflow")
    _write_skill(zeta_root, "zeta", name="zeta", description="Zeta workflow")

    forward = await SkillCatalog.discover([_root(alpha_root), _root(zeta_root)])
    reversed_catalog = await SkillCatalog.discover([_root(zeta_root), _root(alpha_root)])

    forward_names = tuple(match.skill.name for match in forward.search("workflow", limit=10))
    reversed_names = tuple(
        match.skill.name for match in reversed_catalog.search("workflow", limit=10)
    )
    assert forward_names == reversed_names == ("alpha", "zeta")


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
    assert diagnostic.path == str(malformed_path)
    assert diagnostic.reason_code == "invalid_skill_metadata"
    assert "do-not-leak" not in diagnostic.safe_reason


@pytest.mark.asyncio
async def test_prompt_view_exposes_frozen_exhaustive_projection(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(skills_root, "alpha", name="alpha", description="Alpha workflow")
    _write_skill(skills_root, "zeta", name="zeta", description="Zeta workflow")
    catalog = await SkillCatalog.discover([_root(skills_root)])

    outcome = catalog.prompt_view("workflow", max_characters=1_000)

    assert outcome.status is SkillProjectionStatus.READY
    assert outcome.reason_code is None
    assert outcome.view is not None
    assert tuple(match.skill.name for match in outcome.view.matches) == ("alpha", "zeta")
    assert outcome.view.total_count == 2
    assert outcome.view.omitted_count == 0
    assert outcome.view.overflowed_priority_count == 0
    assert outcome.view.rendered_characters > 0
