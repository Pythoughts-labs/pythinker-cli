from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from pythinker_code.exception import AgentSpecError
from pythinker_code.subagents.catalogue import (
    UnknownFieldPolicy,
    normalize_agent_name,
    resolve_agent_catalogue,
)


def _write_yaml(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _write_root_with_subagents(path: Path, subagents: str, *, extra: str = "") -> None:
    _write_yaml(
        path,
        f"""version: 1
{extra}agent:
  name: root
  system_prompt_path: ./system.md
  tools: []
  subagents:
{subagents}
""",
    )
    (path.parent / "system.md").write_text("root", encoding="utf-8")


def _write_child(path: Path, *, extra: str = "") -> None:
    _write_yaml(
        path,
        f"""version: 1
agent:
  name: child
  system_prompt_path: ./child.md
  tools: []
{extra}""",
    )
    (path.parent / "child.md").write_text("child", encoding="utf-8")


@pytest.mark.asyncio
async def test_yaml_warn_aggregates_sorted_unknown_paths_without_values_or_absolute_paths(
    tmp_path: Path,
) -> None:
    child = tmp_path / "child.yaml"
    _write_child(child, extra="  zeta: SUPER_SECRET\n  alpha: hidden\n")
    root = tmp_path / "root.yaml"
    _write_root_with_subagents(
        root,
        "    Worker:\n      path: ./child.yaml\n      description: worker\n      typo: nested-secret\n",
        extra="unexpected_top: top-secret\n",
    )

    catalogue = await resolve_agent_catalogue(
        agent_file=root,
        markdown_roots=(),
        materialized_dir=tmp_path / "generated",
        unknown_field_policy=UnknownFieldPolicy.WARN,
    )

    assert len(catalogue.values()) == 1
    assert [diagnostic.field_path for diagnostic in catalogue.diagnostics] == [
        "agent.subagents.Worker.typo, unexpected_top",
        "agent.alpha, agent.zeta",
    ]
    rendered = repr(catalogue.diagnostics)
    assert "SUPER_SECRET" not in rendered
    assert "nested-secret" not in rendered
    assert str(tmp_path) not in rendered


@pytest.mark.asyncio
async def test_inherited_unknown_field_warns_once_for_defining_source(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    _write_child(base, extra="  stale_setting: secret\n")
    child = tmp_path / "child.yaml"
    _write_yaml(child, "version: 1\nagent:\n  extend: ./base.yaml\n  name: inherited\n")
    root = tmp_path / "root.yaml"
    _write_root_with_subagents(
        root,
        "    first:\n      path: ./child.yaml\n      description: first\n"
        "    second:\n      path: ./child.yaml\n      description: second\n",
    )

    catalogue = await resolve_agent_catalogue(
        agent_file=root,
        markdown_roots=(),
        materialized_dir=tmp_path / "generated",
    )

    stale = [d for d in catalogue.diagnostics if d.field_path == "agent.stale_setting"]
    assert len(stale) == 1


@pytest.mark.asyncio
async def test_yaml_forbid_fails_required_source_without_leaking_value_or_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root.yaml"
    _write_root_with_subagents(root, "", extra="unknown: DO_NOT_LEAK\n")

    with pytest.raises(AgentSpecError) as error:
        await resolve_agent_catalogue(
            agent_file=root,
            markdown_roots=(),
            materialized_dir=tmp_path / "generated",
            unknown_field_policy=UnknownFieldPolicy.FORBID,
        )

    assert "unknown" in str(error.value)
    assert "DO_NOT_LEAK" not in str(error.value)
    assert str(tmp_path) not in str(error.value)


@pytest.mark.asyncio
async def test_required_yaml_casefold_collision_is_fatal(tmp_path: Path) -> None:
    child = tmp_path / "child.yaml"
    _write_child(child)
    root = tmp_path / "root.yaml"
    _write_root_with_subagents(
        root,
        "    Worker:\n      path: ./child.yaml\n      description: first\n"
        "    worker:\n      path: ./child.yaml\n      description: second\n",
    )

    with pytest.raises(AgentSpecError, match="collision"):
        await resolve_agent_catalogue(
            agent_file=root,
            markdown_roots=(),
            materialized_dir=tmp_path / "generated",
        )


@pytest.mark.asyncio
async def test_catalogue_is_casefolded_sorted_and_deeply_immutable(tmp_path: Path) -> None:
    child = tmp_path / "child.yaml"
    _write_child(child)
    root = tmp_path / "root.yaml"
    _write_root_with_subagents(
        root,
        "    Zed:\n      path: ./child.yaml\n      description: zed\n"
        "    alpha:\n      path: ./child.yaml\n      description: alpha\n",
    )

    catalogue = await resolve_agent_catalogue(
        agent_file=root,
        markdown_roots=(),
        materialized_dir=tmp_path / "generated",
    )

    assert normalize_agent_name("Straße") == "strasse"
    assert [entry.name for entry in catalogue.values()] == ["alpha", "Zed"]
    assert catalogue.get("ALPHA") is catalogue.require("alpha")
    with pytest.raises(KeyError):
        catalogue.require("missing")
    with pytest.raises(TypeError):
        cast("dict[str, object]", catalogue.entries)["new"] = catalogue.require("alpha")
    with pytest.raises(AttributeError):
        catalogue.require("alpha").launch_spec.tools.append("unsafe")
