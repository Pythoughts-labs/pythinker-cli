from __future__ import annotations

from pathlib import Path

import pytest
from pythinker_host.path import HostPath

import pythinker_code.skill as skill_module
from pythinker_code.skill import (
    ScopedSkillsRoot,
    Skill,
    SkillCatalog,
    read_skill_text_with_local_specialization,
)
from pythinker_code.tools.skill import ReadSkill


def _skill(name: str, path: Path, scope: str = "user") -> Skill:
    return Skill(
        name=name,
        description=f"{name} description",
        type="standard",
        dir=HostPath.unsafe_from_local_path(path.parent),
        skill_md_file=HostPath.unsafe_from_local_path(path),
        scope=scope,  # type: ignore[arg-type]
    )


async def test_read_skill_returns_core_and_local_specialization(runtime, tmp_path: Path) -> None:
    core_dir = tmp_path / "review-pr"
    core_dir.mkdir()
    core_path = core_dir / "SKILL.md"
    core_path.write_text("Core review workflow", encoding="utf-8")
    local_dir = tmp_path / "review-pr-local"
    local_dir.mkdir()
    local_path = local_dir / "SKILL.md"
    local_path.write_text("Local review rules", encoding="utf-8")
    runtime.skills = {
        "review-pr": _skill("review-pr", core_path, scope="builtin"),
        "review-pr-local": _skill("review-pr-local", local_path, scope="project"),
    }

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="review-pr"))

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "skill: review-pr" in result.output
    assert "Core review workflow" in result.output
    assert "# Local specialization: review-pr-local" in result.output
    assert "Local review rules" in result.output


async def test_read_skill_reports_missing_skill(runtime) -> None:
    runtime.skills = {}

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="missing"))

    assert result.is_error
    assert result.brief == "Skill not found"
    assert "status: not_found" in result.message


async def test_read_skill_missing_name_returns_only_bounded_suggestions(
    runtime, tmp_path: Path
) -> None:
    runtime.skills = {
        f"deploy-{index}": _skill(f"deploy-{index}", tmp_path / f"deploy-{index}.md")
        for index in range(20)
    }
    runtime.skill_catalog = SkillCatalog(runtime.skills, ())

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="deploy"))

    assert result.is_error
    assert result.message.count("deploy-") <= 5
    assert "deploy-19" not in result.message


async def test_read_skill_distinguishes_unavailable_discovered_source(
    runtime, tmp_path: Path
) -> None:
    root = tmp_path / "skills"
    broken = root / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(
        "---\nname: broken\ntype: unsupported\n---\n",
        encoding="utf-8",
    )
    runtime.skill_catalog = await SkillCatalog.discover(
        [
            ScopedSkillsRoot(
                root=HostPath.unsafe_from_local_path(root),
                scope="project",
            )
        ]
    )
    runtime.skills = dict(runtime.skill_catalog.exhaustive_mapping())

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="broken"))

    assert result.is_error
    assert result.brief == "Skill unavailable"
    assert "status: unavailable" in result.message
    assert str(tmp_path) not in result.message


async def test_read_skill_deleted_after_discovery_reports_structured_unavailable(
    runtime, tmp_path: Path
) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "ephemeral"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("---\nname: ephemeral\ndescription: Temporary\n---\n", encoding="utf-8")
    runtime.skill_catalog = await SkillCatalog.discover(
        [ScopedSkillsRoot(root=HostPath.unsafe_from_local_path(root), scope="project")]
    )
    runtime.skills = runtime.skill_catalog.exhaustive_mapping()
    skill_path.unlink()

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="ephemeral"))

    assert result.is_error
    assert result.brief == "Skill unavailable"
    assert result.message.startswith("status: unavailable\n")
    assert "could not be read" in result.message
    assert str(tmp_path) not in result.message


async def test_read_skill_resolves_plugin_style_alias(runtime, tmp_path: Path) -> None:
    skill_dir = tmp_path / "designer-skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("Use MCP tools for design.", encoding="utf-8")
    runtime.skills = {
        "designer-skill": _skill("designer-skill", skill_path, scope="builtin"),
    }

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="designer-skill:designer-skill"))

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "skill: designer-skill" in result.output
    assert "Use MCP tools for design." in result.output


async def test_read_skill_mcp_bridge_when_filesystem_skill_missing(runtime) -> None:
    runtime.skills = {}
    runtime.mcp_tools = {
        "mcp__designer-skill__get_design_system": object(),
        "mcp__designer-skill__get_reference": object(),
        "mcp__designer-skill__anti_slop_checklist": object(),
    }

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="designer-skill"))

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "MCP bridge" in result.output
    assert "status: mcp_fallback" in result.output
    assert "mcp__designer-skill__get_design_system" in result.output
    assert "anti_slop_checklist" in result.output


async def test_read_skill_mcp_bridge_works_for_user_added_server(runtime) -> None:
    runtime.skills = {}
    runtime.mcp_tools = {
        "mcp__my-research__search": object(),
        "mcp__my-research__extract": object(),
    }

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="my-research"))

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "# MCP skill bridge: my-research" in result.output
    assert "mcp__my-research__search" in result.output
    assert "get_design_system" not in result.output


async def test_missing_skill_caps_connected_mcp_server_hint(runtime) -> None:
    runtime.skills = {}
    runtime.skill_catalog = SkillCatalog({}, ())
    runtime.mcp_tools = {f"mcp__server-{index:03d}__search": object() for index in range(50)}

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="missing"))

    assert result.is_error
    assert len(result.message) < 500
    assert (
        "Connected MCP servers: server-000, server-001, server-002, server-003, server-004"
        in result.message
    )
    assert "45 omitted" in result.message
    assert "server-049" not in result.message


async def test_read_skill_appends_resource_manifest(runtime, tmp_path: Path) -> None:
    # skills-1: a subdirectory skill referencing scripts/ and references/ must
    # surface those bundled files at runtime so the model knows they exist and
    # where they resolve, instead of improvising a directory listing.
    skill_dir = tmp_path / "pdf-tools"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "references").mkdir()
    (skill_dir / "SKILL.md").write_text("Rotate PDFs with scripts/rotate_pdf.py", encoding="utf-8")
    (skill_dir / "scripts" / "rotate_pdf.py").write_text("print('x')", encoding="utf-8")
    (skill_dir / "references" / "aws.md").write_text("docs", encoding="utf-8")
    runtime.skills = {"pdf-tools": _skill("pdf-tools", skill_dir / "SKILL.md", scope="builtin")}

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="pdf-tools"))

    assert not result.is_error
    assert isinstance(result.output, str)
    out = result.output
    assert f"Base directory: {skill_dir}" in out
    manifest = out.split("Bundled resources:", 1)[1]
    assert "scripts/rotate_pdf.py" in manifest
    assert "references/aws.md" in manifest
    # SKILL.md itself is the body, not a bundled resource.
    assert "SKILL.md" not in manifest


async def test_read_skill_no_manifest_when_only_skill_md(runtime, tmp_path: Path) -> None:
    skill_dir = tmp_path / "plain"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("Just a body", encoding="utf-8")
    runtime.skills = {"plain": _skill("plain", skill_dir / "SKILL.md", scope="builtin")}

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="plain"))

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "Bundled resources:" not in result.output


async def test_read_skill_flat_skill_has_no_manifest(runtime, tmp_path: Path) -> None:
    # Flat ".md" skills share the skills root with every other flat skill;
    # enumerating it would leak unrelated files, so no manifest is emitted.
    root = tmp_path / "flatroot"
    root.mkdir()
    flat = root / "quick.md"
    flat.write_text("A flat skill", encoding="utf-8")
    (root / "other-skill.md").write_text("unrelated", encoding="utf-8")
    skill = Skill(
        name="quick",
        description="quick",
        type="standard",
        dir=HostPath.unsafe_from_local_path(root),
        skill_md_file=HostPath.unsafe_from_local_path(flat),
        scope="user",  # type: ignore[arg-type]
    )
    runtime.skills = {"quick": skill}

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="quick"))

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "Bundled resources:" not in result.output
    assert "other-skill.md" not in result.output


async def test_read_skill_manifest_caps_and_summarizes(runtime, tmp_path: Path) -> None:
    skill_dir = tmp_path / "many"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("body", encoding="utf-8")
    for i in range(12):
        (skill_dir / f"r{i:02d}.txt").write_text("x", encoding="utf-8")
    runtime.skills = {"many": _skill("many", skill_dir / "SKILL.md", scope="builtin")}

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="many"))

    assert not result.is_error
    assert isinstance(result.output, str)
    manifest = result.output.split("Bundled resources:", 1)[1]
    # Sorted, so the first 10 (r00..r09) are shown and the last 2 summarized.
    assert manifest.count("\n- r") == 10
    assert "and 2 more file(s)" in manifest


async def test_read_skill_manifest_ceiling_truncates(
    runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(skill_module, "_SKILL_RESOURCE_SCAN_CEILING", 2)
    skill_dir = tmp_path / "huge"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("body", encoding="utf-8")
    for i in range(5):
        (skill_dir / f"f{i}.txt").write_text("x", encoding="utf-8")
    runtime.skills = {"huge": _skill("huge", skill_dir / "SKILL.md", scope="builtin")}

    result = await ReadSkill(runtime)(ReadSkill.params(skill_name="huge"))

    assert not result.is_error
    assert isinstance(result.output, str)
    # Hitting the ceiling reports truncation without claiming an exact count.
    assert "scan stopped at 2 entries" in result.output


async def test_skill_body_manifest_follows_local_specialization(runtime, tmp_path: Path) -> None:
    # Closes the asymmetry: the manifest lives in the shared body-injection
    # function, so every path (ReadSkill, slash runner, compaction restore) gets
    # it — and it appears AFTER the local specialization section.
    core_dir = tmp_path / "deploy"
    (core_dir / "scripts").mkdir(parents=True)
    (core_dir / "SKILL.md").write_text("core body", encoding="utf-8")
    (core_dir / "scripts" / "ship.sh").write_text("echo ship", encoding="utf-8")
    local_dir = tmp_path / "deploy-local"
    local_dir.mkdir()
    (local_dir / "SKILL.md").write_text("local rules", encoding="utf-8")
    skills = {
        "deploy": _skill("deploy", core_dir / "SKILL.md", scope="builtin"),
        "deploy-local": _skill("deploy-local", local_dir / "SKILL.md", scope="project"),
    }

    text = await read_skill_text_with_local_specialization(skills["deploy"], skills)

    assert text is not None
    assert "# Local specialization: deploy-local" in text
    assert "scripts/ship.sh" in text
    # Order: core body -> local specialization -> resource manifest.
    assert text.index("# Local specialization") < text.index("Base directory:")
