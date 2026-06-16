"""Tests for plugin/marketplace manifest schemas and resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pythinker_code.plugin.manifest import (
    MANIFEST_DIRS,
    MarketplaceManifest,
    PluginManifest,
    PluginManifestError,
    find_plugin_manifest,
    load_marketplace_manifest,
    load_plugin_manifest,
)


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("manifest_dir", [*MANIFEST_DIRS, ""])
def test_find_plugin_manifest_across_ecosystems(tmp_path: Path, manifest_dir: str) -> None:
    root = tmp_path / "plug"
    target = root / manifest_dir / "plugin.json" if manifest_dir else root / "plugin.json"
    _write(target, {"name": "p", "version": "1.0.0"})
    assert find_plugin_manifest(root) == target


def test_manifest_dir_priority_prefers_pythinker(tmp_path: Path) -> None:
    root = tmp_path / "plug"
    _write(root / ".claude-plugin" / "plugin.json", {"name": "claude", "version": "1"})
    _write(root / ".pythinker-plugin" / "plugin.json", {"name": "pythinker", "version": "1"})
    # .pythinker-plugin wins over .claude-plugin (first in MANIFEST_DIRS).
    assert load_plugin_manifest(root).name == "pythinker"


def test_claude_minimal_manifest_loads_with_convention_fallback(tmp_path: Path) -> None:
    # A real Claude plugin.json carries no artifact paths — artifacts come from
    # convention dirs. The manifest must still load with empty overrides.
    root = tmp_path / "ponytail"
    _write(
        root / ".claude-plugin" / "plugin.json",
        {"name": "ponytail", "version": "4.3.0", "author": {"name": "x"}},
    )
    manifest = load_plugin_manifest(root)
    assert manifest.name == "ponytail"
    assert manifest.skills == [] and manifest.commands == []


def test_author_accepts_string_or_object(tmp_path: Path) -> None:
    root = tmp_path / "p"
    _write(root / "plugin.json", {"name": "p", "version": "1", "author": "Jane"})
    author = load_plugin_manifest(root).author
    assert author is not None
    assert author.name == "Jane"


def test_artifact_paths_normalize_string_and_list() -> None:
    m = PluginManifest.model_validate(
        {"name": "p", "skills": "skills", "commands": ["a", "b"], "outputStyles": None}
    )
    assert m.skills == ["skills"]
    assert m.commands == ["a", "b"]
    assert m.output_styles == []


def test_mcp_servers_and_dependencies_parse() -> None:
    m = PluginManifest.model_validate(
        {
            "name": "p",
            "mcpServers": {"db": {"command": "x"}},
            "dependencies": ["other@market"],
        }
    )
    assert m.mcp_servers == {"db": {"command": "x"}}
    assert m.dependencies == ["other@market"]


def test_missing_name_is_typed_error(tmp_path: Path) -> None:
    root = tmp_path / "p"
    _write(root / "plugin.json", {"version": "1"})
    with pytest.raises(PluginManifestError, match="Missing required field 'name'"):
        load_plugin_manifest(root)


def test_no_manifest_is_typed_error(tmp_path: Path) -> None:
    with pytest.raises(PluginManifestError, match="No plugin.json"):
        load_plugin_manifest(tmp_path / "empty")


def test_malformed_json_is_typed_error(tmp_path: Path) -> None:
    root = tmp_path / "p"
    (root).mkdir()
    (root / "plugin.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(PluginManifestError, match="Failed to read"):
        load_plugin_manifest(root)


def test_marketplace_manifest_parses_entries(tmp_path: Path) -> None:
    path = _write(
        tmp_path / ".claude-plugin" / "marketplace.json",
        {
            "name": "official",
            "owner": "OpenAI",
            "metadata": {"version": "1.0.2"},
            "plugins": [
                {"name": "codex", "version": "1.0.2", "source": "./plugins/codex"},
            ],
        },
    )
    market = load_marketplace_manifest(path)
    assert isinstance(market, MarketplaceManifest)
    assert market.name == "official"
    assert market.owner is not None and market.owner.name == "OpenAI"
    assert len(market.plugins) == 1
    assert market.plugins[0].source == "./plugins/codex"


def test_marketplace_missing_name_is_typed_error(tmp_path: Path) -> None:
    path = _write(tmp_path / "marketplace.json", {"plugins": []})
    with pytest.raises(PluginManifestError, match="Missing required field 'name'"):
        load_marketplace_manifest(path)
