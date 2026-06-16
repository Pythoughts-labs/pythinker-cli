"""Tests for plugin dependency parsing, demotion, and install closure."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pythinker_code.plugin import install, loader
from pythinker_code.plugin.dependency import parse_plugin_identifier, verify_and_demote
from pythinker_code.plugin.manifest import PluginManifest
from pythinker_code.plugin.marketplace import (
    MarketplaceError,
    add_marketplace,
    parse_marketplace_input,
)


def test_parse_plugin_identifier_first_at_only() -> None:
    assert parse_plugin_identifier("foo") == ("foo", None)
    assert parse_plugin_identifier("foo@mkt") == ("foo", "mkt")
    # Only the first '@' separates; the rest stays with the marketplace part.
    assert parse_plugin_identifier("foo@mkt@x") == ("foo", "mkt@x")


def test_manifest_strips_version_suffix_and_object_form() -> None:
    m = PluginManifest.model_validate(
        {
            "name": "p",
            "dependencies": [
                "bare",
                "name@mkt",
                "name@mkt@^1.2",  # version suffix stripped
                {"name": "obj", "marketplace": "mk2"},
                {"name": "obj2"},
            ],
        }
    )
    assert m.dependencies == ["bare", "name@mkt", "name@mkt", "obj@mk2", "obj2"]


def test_verify_and_demote_disables_unsatisfied() -> None:
    # b depends on a missing plugin -> demoted; a has no deps -> stays.
    demoted, issues = verify_and_demote([("a", []), ("b", ["ghost"])], {"a", "b"})
    assert demoted == {"b"}
    assert [(i.plugin, i.dependency, i.reason) for i in issues] == [("b", "ghost", "not-found")]


def test_verify_and_demote_cascades_and_marks_not_enabled() -> None:
    # c needs b, b needs a, but a is installed-yet-disabled. b demotes (a not
    # enabled), then c demotes (b no longer enabled). Marketplace qualifier on the
    # dep is matched by name.
    plugins = [("a", []), ("b", ["a@mk"]), ("c", ["b"])]
    demoted, issues = verify_and_demote(plugins, {"b", "c"})  # a known but disabled
    assert demoted == {"b", "c"}
    reasons = {i.plugin: i.reason for i in issues}
    assert reasons == {"b": "not-enabled", "c": "not-enabled"}


def _entry(root: Path, name: str, deps: list[str] | None = None) -> None:
    """Create a marketplace 'demo'-style plugin source dir with optional deps."""
    src = root / name
    manifest = src / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    body: dict[str, object] = {"name": name, "version": "1.0.0"}
    if deps:
        body["dependencies"] = deps
    manifest.write_text(json.dumps(body), encoding="utf-8")


def _marketplace(root: Path, names: list[str]) -> Path:
    mk = root / ".claude-plugin" / "marketplace.json"
    mk.parent.mkdir(parents=True)
    mk.write_text(
        json.dumps(
            {
                "name": "mk",
                "plugins": [{"name": n, "version": "1.0.0", "source": f"./{n}"} for n in names],
            }
        ),
        encoding="utf-8",
    )
    return root


def test_install_pulls_transitive_dependencies(tmp_path: Path) -> None:
    market = _marketplace(tmp_path / "market", ["app", "lib"])
    _entry(market, "app", deps=["lib"])
    _entry(market, "lib")
    add_marketplace("mk", parse_marketplace_input(str(market)))

    install.install_plugin_from_marketplace("app", "mk")

    found = {p.name for p in loader.discover_plugins().plugins}
    assert {"app", "lib"} <= found  # dependency installed alongside the root


def test_install_blocks_cross_marketplace_dependency(tmp_path: Path) -> None:
    market = _marketplace(tmp_path / "market", ["app"])
    _entry(market, "app", deps=["lib@other"])
    add_marketplace("mk", parse_marketplace_input(str(market)))

    with pytest.raises(MarketplaceError, match="Cross-marketplace"):
        install.install_plugin_from_marketplace("app", "mk")

    # The failed install must roll back: "app" was materialized before its
    # cross-marketplace dependency was rejected, so it must not be left recorded.
    from pythinker_code.plugin.installed import load_installed_plugins

    assert "app@mk" not in load_installed_plugins()
