"""Tests for marketplace registry, source parsing, and installed registry."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pythinker_code.plugin import installed, marketplace
from pythinker_code.plugin.marketplace import MarketplaceError, parse_marketplace_input


@pytest.fixture(autouse=True)
def _share_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path / "share"))


# --- parse_marketplace_input -------------------------------------------------


def test_parse_github_shorthand() -> None:
    src = parse_marketplace_input("anthropics/claude-plugins-official")
    assert src.source == "github"
    assert src.repo == "anthropics/claude-plugins-official"
    assert src.ref is None


def test_parse_github_shorthand_with_ref() -> None:
    src = parse_marketplace_input("owner/repo#v2")
    assert src.source == "github" and src.repo == "owner/repo" and src.ref == "v2"


def test_parse_github_url_becomes_git() -> None:
    src = parse_marketplace_input("https://github.com/owner/repo")
    assert src.source == "git" and src.url == "https://github.com/owner/repo.git"


def test_parse_git_url_with_dot_git() -> None:
    src = parse_marketplace_input("https://example.com/x/y.git#main")
    assert src.source == "git" and src.ref == "main"


def test_parse_ssh_url() -> None:
    src = parse_marketplace_input("git@github.com:owner/repo.git")
    assert src.source == "git" and src.url == "git@github.com:owner/repo.git"


def test_parse_plain_url() -> None:
    src = parse_marketplace_input("https://example.com/market.json")
    assert src.source == "url"


def test_parse_local_directory(tmp_path: Path) -> None:
    d = tmp_path / "mk"
    d.mkdir()
    src = parse_marketplace_input(str(d))
    assert src.source == "directory" and src.path == str(d.resolve())


def test_parse_local_json_file(tmp_path: Path) -> None:
    f = tmp_path / "m.json"
    f.write_text("{}", encoding="utf-8")
    src = parse_marketplace_input(str(f))
    assert src.source == "file"


def test_parse_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(MarketplaceError, match="does not exist"):
        parse_marketplace_input(str(tmp_path / "nope"))


def test_parse_non_json_file_raises(tmp_path: Path) -> None:
    f = tmp_path / "m.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(MarketplaceError, match="must be .json"):
        parse_marketplace_input(str(f))


def test_parse_unrecognized_raises() -> None:
    with pytest.raises(MarketplaceError):
        parse_marketplace_input("just-a-word")


# --- known_marketplaces registry --------------------------------------------


def test_add_list_remove_marketplace() -> None:
    src = parse_marketplace_input("anthropics/claude-plugins-official")
    marketplace.add_marketplace("official", src)

    loaded = marketplace.load_known_marketplaces()
    assert "official" in loaded
    assert loaded["official"].source.repo == "anthropics/claude-plugins-official"
    assert loaded["official"].install_location is not None

    assert marketplace.remove_marketplace("official") is True
    assert "official" not in marketplace.load_known_marketplaces()
    assert marketplace.remove_marketplace("official") is False


def test_known_marketplaces_skips_corrupt_entry() -> None:
    path = marketplace.known_marketplaces_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"good": {"source": {"source": "github", "repo": "a/b"}}, "bad": 123}),
        encoding="utf-8",
    )
    loaded = marketplace.load_known_marketplaces()
    assert set(loaded) == {"good"}


def test_resolve_local_directory_marketplace(tmp_path: Path) -> None:
    market_dir = tmp_path / "mk"
    manifest = market_dir / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"name": "mk", "plugins": [{"name": "p", "source": "./p"}]}),
        encoding="utf-8",
    )
    src = parse_marketplace_input(str(market_dir))
    manifest_obj = marketplace.resolve_local_marketplace(src)
    assert manifest_obj.name == "mk"
    assert manifest_obj.plugins[0].name == "p"


# --- installed registry ------------------------------------------------------


def test_record_and_remove_install() -> None:
    rec = installed.InstalledRecord(installPath="/x/y", version="1.0.0")
    installed.record_install("p", "official", rec)

    loaded = installed.load_installed_plugins()
    assert "p@official" in loaded
    assert loaded["p@official"][0].install_path == "/x/y"

    assert installed.remove_install("p", "official") is True
    assert installed.load_installed_plugins() == {}


def test_record_install_replaces_same_scope() -> None:
    installed.record_install("p", "m", installed.InstalledRecord(installPath="/a", scope="user"))
    installed.record_install("p", "m", installed.InstalledRecord(installPath="/b", scope="user"))
    records = installed.load_installed_plugins()["p@m"]
    assert len(records) == 1 and records[0].install_path == "/b"


def test_remove_install_by_scope() -> None:
    installed.record_install("p", "m", installed.InstalledRecord(installPath="/a", scope="user"))
    installed.record_install("p", "m", installed.InstalledRecord(installPath="/b", scope="project"))
    assert installed.remove_install("p", "m", scope="user") is True
    remaining = installed.load_installed_plugins()["p@m"]
    assert len(remaining) == 1 and remaining[0].scope == "project"
