"""Tests for installing plugins from marketplaces into the versioned cache."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pythinker_code.plugin import installed, loader, marketplace
from pythinker_code.plugin.install import install_plugin_from_marketplace
from pythinker_code.plugin.marketplace import MarketplaceError


@pytest.fixture(autouse=True)
def _share_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path / "share"))


def _make_directory_marketplace(root: Path, plugin: str) -> Path:
    """A directory marketplace with one plugin at ./plugins/<plugin>."""
    manifest = root / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": "mk",
                "plugins": [{"name": plugin, "version": "1.0.0", "source": f"./plugins/{plugin}"}],
            }
        ),
        encoding="utf-8",
    )
    plugin_dir = root / "plugins" / plugin
    pm = plugin_dir / ".claude-plugin" / "plugin.json"
    pm.parent.mkdir(parents=True)
    pm.write_text(json.dumps({"name": plugin, "version": "1.0.0"}), encoding="utf-8")
    (plugin_dir / "skills" / "s").mkdir(parents=True)
    (plugin_dir / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\n# s", encoding="utf-8"
    )
    return root


def test_install_from_directory_marketplace(tmp_path: Path) -> None:
    market = _make_directory_marketplace(tmp_path / "market", "demo")
    marketplace.add_marketplace("mk", marketplace.parse_marketplace_input(str(market)))

    record = install_plugin_from_marketplace("demo", "mk")

    dest = Path(record.install_path)
    assert dest.exists()
    assert (dest / ".claude-plugin" / "plugin.json").is_file()
    assert (dest / "skills" / "s" / "SKILL.md").is_file()
    # Recorded under name@marketplace.
    assert "demo@mk" in installed.load_installed_plugins()


def test_install_then_discoverable(tmp_path: Path) -> None:
    market = _make_directory_marketplace(tmp_path / "market", "demo")
    marketplace.add_marketplace("mk", marketplace.parse_marketplace_input(str(market)))
    install_plugin_from_marketplace("demo", "mk")

    # The installed plugin now lives in the pythinker cache and is discoverable.
    result = loader.discover_plugins(include_external=False)
    assert any(p.name == "demo" and p.origin == "pythinker" for p in result.plugins)


def test_install_unknown_marketplace_raises(tmp_path: Path) -> None:
    with pytest.raises(MarketplaceError, match="not configured"):
        install_plugin_from_marketplace("demo", "ghost")


def test_install_missing_plugin_raises(tmp_path: Path) -> None:
    market = _make_directory_marketplace(tmp_path / "market", "demo")
    marketplace.add_marketplace("mk", marketplace.parse_marketplace_input(str(market)))
    with pytest.raises(MarketplaceError, match="not found in marketplace"):
        install_plugin_from_marketplace("absent", "mk")


def test_install_rejects_unsafe_name(tmp_path: Path) -> None:
    with pytest.raises(MarketplaceError, match="Unsafe"):
        install_plugin_from_marketplace("../evil", "mk")


def test_install_traversal_source_blocked(tmp_path: Path) -> None:
    root = tmp_path / "market"
    manifest = root / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"name": "mk", "plugins": [{"name": "evil", "source": "../../etc"}]}),
        encoding="utf-8",
    )
    marketplace.add_marketplace("mk", marketplace.parse_marketplace_input(str(root)))
    with pytest.raises(MarketplaceError, match="escapes marketplace root"):
        install_plugin_from_marketplace("evil", "mk")


def test_install_reuses_external_via_symlink(tmp_path: Path, monkeypatch) -> None:
    # Plugin already present in an external (Claude) cache -> symlink, no copy.
    external_version = tmp_path / "claude_cache" / "mk" / "demo" / "9.9.9"
    pm = external_version / ".claude-plugin" / "plugin.json"
    pm.parent.mkdir(parents=True)
    pm.write_text(json.dumps({"name": "demo", "version": "9.9.9"}), encoding="utf-8")

    from pythinker_code.plugin import install as install_mod

    monkeypatch.setattr(
        install_mod, "external_installed_plugin_dirs", lambda m, p: [external_version]
    )
    # Marketplace must be configured, but fetch must be skipped by the reuse path.
    marketplace.add_marketplace("mk", marketplace.parse_marketplace_input("owner/repo"))

    record = install_plugin_from_marketplace("demo", "mk")
    dest = Path(record.install_path)
    assert dest.is_symlink()
    assert dest.resolve() == external_version.resolve()
    assert record.version == "9.9.9"
    # Discoverable through the symlink.
    result = loader.discover_plugins(include_external=False)
    assert any(p.name == "demo" for p in result.plugins)


@pytest.mark.parametrize(
    "bad_url",
    ["ext::sh -c touch${IFS}/tmp/x", "fd::7", "--upload-pack=evil", "/local/path"],
)
def test_git_clone_rejects_unsafe_url(tmp_path: Path, bad_url: str) -> None:
    from pythinker_code.plugin.install import _git_clone

    with pytest.raises(MarketplaceError, match="Unsafe or unsupported git URL"):
        _git_clone(bad_url, None, tmp_path / "dest")


def test_git_clone_rejects_flag_ref(tmp_path: Path) -> None:
    from pythinker_code.plugin.install import _git_clone

    with pytest.raises(MarketplaceError, match="Unsafe git ref"):
        _git_clone("https://example.com/x.git", "--upload-pack=evil", tmp_path / "dest")


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _git_available(), reason="git not available")
def test_install_git_plugin_source_offline(tmp_path: Path) -> None:
    # A bare-ish local git repo serving as a plugin source (clone works offline).
    repo = tmp_path / "plugrepo"
    pm = repo / ".claude-plugin" / "plugin.json"
    pm.parent.mkdir(parents=True)
    pm.write_text(json.dumps({"name": "gitp", "version": "2.0.0"}), encoding="utf-8")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)

    market = tmp_path / "market"
    manifest = market / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": "mk",
                "plugins": [
                    {
                        "name": "gitp",
                        "version": "2.0.0",
                        "source": {"source": "git", "url": f"file://{repo}"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    marketplace.add_marketplace("mk", marketplace.parse_marketplace_input(str(market)))

    record = install_plugin_from_marketplace("gitp", "mk")
    dest = Path(record.install_path)
    assert (dest / ".claude-plugin" / "plugin.json").is_file()
    assert not (dest / ".git").exists()  # .git excluded
