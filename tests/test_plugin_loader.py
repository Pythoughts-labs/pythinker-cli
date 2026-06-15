"""Tests for plugin discovery, loading, and artifact resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pythinker_code.plugin import artifacts, loader


def _make_plugin(
    root: Path,
    name: str,
    *,
    manifest_dir: str = ".claude-plugin",
    extra: dict | None = None,
) -> Path:
    payload = {"name": name, "version": "1.0.0", **(extra or {})}
    manifest_path = root / manifest_dir / "plugin.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return root


def test_discovers_plugin_nested_like_claude_cache(tmp_path: Path, monkeypatch) -> None:
    # Mirror ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/.claude-plugin/
    cache = tmp_path / "claude" / "cache"
    plugin_root = cache / "ponytail" / "ponytail" / "4.3.0"
    _make_plugin(plugin_root, "ponytail")

    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: tmp_path / "none")
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [cache])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])

    result = loader.discover_plugins()
    names = {p.name for p in result.plugins}
    assert "ponytail" in names
    found = next(p for p in result.plugins if p.name == "ponytail")
    assert found.origin == "claude"
    assert found.root == plugin_root


def test_malformed_plugin_is_collected_not_raised(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    bad = cache / "bad"
    (bad / ".claude-plugin").mkdir(parents=True)
    (bad / ".claude-plugin" / "plugin.json").write_text("{broken", encoding="utf-8")
    _make_plugin(cache / "good", "good")

    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])

    result = loader.discover_plugins()
    assert {p.name for p in result.plugins} == {"good"}
    assert len(result.errors) == 1
    assert "bad" in str(result.errors[0].root)


def test_pythinker_origin_wins_over_external_on_name_clash(tmp_path: Path, monkeypatch) -> None:
    native = tmp_path / "native"
    external = tmp_path / "claude"
    _make_plugin(native / "dup", "dup", extra={"description": "native"})
    _make_plugin(external / "dup", "dup", extra={"description": "external"})

    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: native)
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [external])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])

    result = loader.discover_plugins()
    dup = [p for p in result.plugins if p.name == "dup"]
    assert len(dup) == 1
    assert dup[0].origin == "pythinker"
    assert dup[0].manifest.description == "native"


def test_include_external_false_skips_claude_codex(tmp_path: Path, monkeypatch) -> None:
    _make_plugin(tmp_path / "claude" / "p", "claude-only")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [tmp_path / "claude"])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])

    result = loader.discover_plugins(include_external=False)
    assert result.plugins == []


def test_is_enabled_filter_marks_disabled(tmp_path: Path, monkeypatch) -> None:
    _make_plugin(tmp_path / "cache" / "on", "on")
    _make_plugin(tmp_path / "cache" / "off", "off")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])

    result = loader.discover_plugins(is_enabled={"on"})
    assert {p.name for p in result.enabled} == {"on"}
    assert len(result.plugins) == 2


def _loaded(root: Path, **manifest_extra) -> loader.LoadedPlugin:
    _make_plugin(root, "p", extra=manifest_extra)
    from pythinker_code.plugin.manifest import load_plugin_manifest

    return loader.LoadedPlugin(
        name="p", root=root, manifest=load_plugin_manifest(root), origin="pythinker"
    )


def test_skill_dirs_convention_and_override(tmp_path: Path) -> None:
    root = tmp_path / "p1"
    (root / "skills").mkdir(parents=True)
    plugin = _loaded(root)
    assert artifacts.skill_dirs(plugin) == [root / "skills"]

    root2 = tmp_path / "p2"
    (root2 / "custom").mkdir(parents=True)
    plugin2 = _loaded(root2, skills="custom")
    assert artifacts.skill_dirs(plugin2) == [(root2 / "custom").resolve()]


def test_artifact_path_traversal_is_blocked(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "p"
    plugin = _loaded(root, skills="../outside")
    # Escaping path is rejected -> no dirs contributed.
    assert artifacts.skill_dirs(plugin) == []


def test_hooks_file_convention(tmp_path: Path) -> None:
    root = tmp_path / "p"
    hooks = root / "hooks" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text("{}", encoding="utf-8")
    plugin = _loaded(root)
    assert artifacts.hooks_file(plugin) == hooks


def test_mcp_servers_merges_manifest_and_file(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}),
        encoding="utf-8",
    )
    plugin = _loaded(root, mcpServers={"b": {"command": "override"}, "c": {"command": "z"}})
    servers = artifacts.mcp_servers(plugin)
    assert set(servers) == {"a", "b", "c"}
    assert servers["b"] == {"command": "override"}  # manifest wins


def test_mcp_servers_malformed_file_contributes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / ".mcp.json").write_text("{broken", encoding="utf-8")
    plugin = _loaded(root)
    assert artifacts.mcp_servers(plugin) == {}


@pytest.mark.parametrize("missing_root", ["does/not/exist"])
def test_discovery_skips_absent_roots(tmp_path: Path, monkeypatch, missing_root: str) -> None:
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: tmp_path / missing_root)
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])
    result = loader.discover_plugins()
    assert result.plugins == [] and result.errors == []
