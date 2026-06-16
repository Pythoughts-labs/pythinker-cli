"""Tests for plugin-based LSP server loading and recommendation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from pythinker_code.config import Config, LspConfig
from pythinker_code.lsp.plugin_servers import plugin_lsp_servers
from pythinker_code.lsp.recommend import (
    MAX_IGNORED_COUNT,
    get_matching_lsp_plugins,
    is_lsp_recommendations_disabled,
)
from pythinker_code.lsp.service import LspInitStatus, LspService
from pythinker_code.plugin import loader, marketplace
from pythinker_code.plugin.installed import InstalledRecord, record_install
from pythinker_code.plugin.marketplace import MarketplaceSource
from pythinker_code.plugin.policy import PluginPolicy
from pythinker_code.soul.agent import Runtime


def _install_plugin(
    cache: Path,
    plugin: str,
    *,
    manifest_extra: dict[str, Any] | None = None,
    lsp_json: dict[str, Any] | None = None,
) -> Path:
    root = cache / "mkt" / plugin / "1.0.0"
    manifest_dir = root / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"name": plugin, "version": "1.0.0"}
    if manifest_extra:
        manifest.update(manifest_extra)
    manifest_dir.joinpath("plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    if lsp_json is not None:
        root.joinpath(".lsp.json").write_text(json.dumps(lsp_json), encoding="utf-8")
    return root


def _server_config(*, command: str = "echo", ext: str = ".py") -> dict[str, Any]:
    return {
        "command": command,
        "extensionToLanguage": {ext: "python"},
    }


@pytest.fixture
def _no_external(monkeypatch):
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])


@pytest.fixture(autouse=True)
def _share_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path / "share"))


def test_inline_manifest_lsp_servers(tmp_path: Path, monkeypatch, _no_external) -> None:
    cache = tmp_path / "cache"
    _install_plugin(
        cache,
        "py-lsp",
        manifest_extra={
            "lspServers": {
                "pyright": _server_config(command="pyright-langserver", ext=".py"),
            }
        },
    )
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)

    servers = plugin_lsp_servers()
    assert list(servers) == ["plugin:py-lsp:pyright"]
    cfg = servers["plugin:py-lsp:pyright"]
    assert cfg.command == "pyright-langserver"
    assert cfg.extension_to_language == {".py": "python"}


def test_lsp_json_file_loading(tmp_path: Path, monkeypatch, _no_external) -> None:
    cache = tmp_path / "cache"
    _install_plugin(
        cache,
        "ts-lsp",
        lsp_json={"tsserver": _server_config(command="typescript-language-server", ext=".ts")},
    )
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)

    servers = plugin_lsp_servers()
    assert "plugin:ts-lsp:tsserver" in servers
    assert servers["plugin:ts-lsp:tsserver"].extension_to_language == {".ts": "python"}


def test_manifest_string_path_to_lsp_json(tmp_path: Path, monkeypatch, _no_external) -> None:
    cache = tmp_path / "cache"
    root = _install_plugin(cache, "go-lsp", manifest_extra={"lspServers": "servers.json"})
    servers_file = root / "servers.json"
    servers_file.write_text(
        json.dumps({"gopls": _server_config(command="gopls", ext=".go")}),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)

    servers = plugin_lsp_servers()
    assert "plugin:go-lsp:gopls" in servers


def test_env_resolution(tmp_path: Path, monkeypatch, _no_external) -> None:
    cache = tmp_path / "cache"
    root = _install_plugin(
        cache,
        "env-lsp",
        manifest_extra={
            "lspServers": {
                "srv": {
                    "command": "${PYTHINKER_PLUGIN_ROOT}/bin/lsp",
                    "args": ["${TEST_LSP_ARG:-fallback}"],
                    "extensionToLanguage": {".rs": "rust"},
                    "env": {"TOKEN": "${user_config.api_key}", "HOME_PATH": "${HOME}"},
                }
            },
            "userConfig": {"api_key": {"type": "string"}},
        },
    )
    (root / "bin").mkdir()
    (root / "bin" / "lsp").write_text("", encoding="utf-8")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)
    monkeypatch.setenv("HOME", "/tmp/home")
    monkeypatch.delenv("TEST_LSP_ARG", raising=False)

    policy = PluginPolicy(options={"env-lsp": {"api_key": "secret-token"}})
    servers = plugin_lsp_servers(policy)
    cfg = servers["plugin:env-lsp:srv"]
    assert cfg.command == f"{root}/bin/lsp"
    assert cfg.args == ["fallback"]
    assert cfg.env["PYTHINKER_PLUGIN_ROOT"] == str(root)
    assert cfg.env["TOKEN"] == "secret-token"
    assert cfg.env["HOME_PATH"] == "/tmp/home"
    assert "PYTHINKER_PLUGIN_DATA" in cfg.env


def test_scope_prefix(tmp_path: Path, monkeypatch, _no_external) -> None:
    cache = tmp_path / "cache"
    _install_plugin(
        cache,
        "one",
        manifest_extra={"lspServers": {"shared": _server_config(command="first", ext=".js")}},
    )
    _install_plugin(
        cache,
        "two",
        manifest_extra={"lspServers": {"shared": _server_config(command="second", ext=".js")}},
    )
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)

    servers = plugin_lsp_servers()
    assert servers["plugin:one:shared"].command == "first"
    assert servers["plugin:two:shared"].command == "second"


def test_external_exec_gate(tmp_path: Path, monkeypatch) -> None:
    claude = tmp_path / "claude"
    root = claude / "exec-lsp" / "exec-lsp" / "1.0.0"
    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "name": "exec-lsp",
                "version": "1.0.0",
                "lspServers": {"srv": _server_config(command="srv-bin", ext=".py")},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: tmp_path / "empty")
    monkeypatch.setattr(loader, "claude_plugin_roots", lambda: [claude])
    monkeypatch.setattr(loader, "codex_plugin_roots", lambda: [])

    assert plugin_lsp_servers() == {}
    servers = plugin_lsp_servers(PluginPolicy(external_exec=True))
    assert "plugin:exec-lsp:srv" in servers


def test_bad_plugin_isolated(tmp_path: Path, monkeypatch, _no_external) -> None:
    cache = tmp_path / "cache"
    _install_plugin(
        cache,
        "good",
        manifest_extra={"lspServers": {"ok": _server_config(command="good-bin", ext=".py")}},
    )
    bad_root = cache / "mkt" / "bad" / "1.0.0"
    bad_root.mkdir(parents=True)
    bad_root.joinpath(".lsp.json").write_text("{not json", encoding="utf-8")
    manifest = bad_root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({"name": "bad", "version": "1.0.0"}), encoding="utf-8")
    monkeypatch.setattr(loader, "plugin_cache_dir", lambda: cache)

    servers = plugin_lsp_servers()
    assert "plugin:good:ok" in servers


def _register_marketplace(tmp_path: Path, name: str, manifest_path: Path) -> None:
    marketplace.add_marketplace(
        name,
        MarketplaceSource(source="file", path=str(manifest_path)),
    )


def _write_marketplace(tmp_path: Path, name: str, plugins: list[dict[str, Any]]) -> Path:
    path = tmp_path / f"{name}.json"
    path.write_text(
        json.dumps({"name": name, "plugins": plugins}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_recommendation_filter_matrix(tmp_path: Path, monkeypatch) -> None:
    official = _write_marketplace(
        tmp_path,
        "pythinker-plugins-official",
        [
            {
                "name": "official-py",
                "description": "Official Python LSP",
                "lspServers": {"py": _server_config(command="official-py-bin", ext=".py")},
            }
        ],
    )
    third_party = _write_marketplace(
        tmp_path,
        "community",
        [
            {
                "name": "community-ts",
                "lspServers": {"ts": _server_config(command="community-ts-bin", ext=".ts")},
            },
            {
                "name": "installed-py",
                "lspServers": {"py2": _server_config(command="installed-py-bin", ext=".py")},
            },
            {
                "name": "never-py",
                "lspServers": {"py3": _server_config(command="never-py-bin", ext=".py")},
            },
            {
                "name": "no-binary",
                "lspServers": {"py4": _server_config(command="missing-binary-xyz", ext=".py")},
            },
        ],
    )
    _register_marketplace(tmp_path, "pythinker-plugins-official", official)
    _register_marketplace(tmp_path, "community", third_party)
    record_install("installed-py", "community", InstalledRecord(installPath="/tmp/x"))

    config = Config(
        lsp=LspConfig(recommendation_never=["never-py@community"]),
    )

    def binary_on_path(command: str) -> bool:
        return command != "missing-binary-xyz"

    with patch(
        "pythinker_code.lsp.recommend.is_binary_installed",
        side_effect=binary_on_path,
    ):
        recs = get_matching_lsp_plugins("main.py", config)

    assert [r.plugin_id for r in recs] == ["official-py@pythinker-plugins-official"]
    assert recs[0].is_official is True

    with patch(
        "pythinker_code.lsp.recommend.is_binary_installed",
        side_effect=binary_on_path,
    ):
        ts_recs = get_matching_lsp_plugins("app.ts", config)
    assert [r.plugin_id for r in ts_recs] == ["community-ts@community"]

    disabled = Config(lsp=LspConfig(recommendation_disabled=True))
    assert get_matching_lsp_plugins("main.py", disabled) == []

    ignored = Config(lsp=LspConfig(recommendation_ignored_count=MAX_IGNORED_COUNT))
    assert is_lsp_recommendations_disabled(ignored.lsp)
    assert get_matching_lsp_plugins("main.py", ignored) == []


@pytest.mark.asyncio
async def test_reinit_generation_guard(monkeypatch) -> None:
    init_started = asyncio.Event()
    release_init = asyncio.Event()
    successful_generations: list[int] = []

    class FakeRuntime:
        work_dir = "/tmp/project"
        config = Config(lsp=LspConfig())

    original_run_init = LspService._run_init

    async def tracking_run_init(self, generation: int) -> None:
        await original_run_init(self, generation)
        if self.status() == LspInitStatus.SUCCESS and self._generation == generation:
            successful_generations.append(generation)

    async def slow_initialize(self) -> None:  # noqa: ANN001
        init_started.set()
        await release_init.wait()

    monkeypatch.setattr(LspService, "_run_init", tracking_run_init)
    monkeypatch.setattr(
        "pythinker_code.lsp.service.LspServerManager.initialize",
        slow_initialize,
    )

    service = LspService.create(cast(Runtime, FakeRuntime()), servers={})
    await init_started.wait()
    await service.reinitialize()
    release_init.set()
    await service.wait_for_init()

    assert service.status() == LspInitStatus.SUCCESS
    assert successful_generations == [2]
    await service.shutdown()
