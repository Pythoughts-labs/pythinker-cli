from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

from pythinker_code.plugin import PluginError
from pythinker_code.plugin.manager import (
    collect_host_values,
    install_plugin,
    list_plugins,
    remove_plugin,
)


def _make_source_plugin(tmp_path: Path, name: str = "test-plugin") -> Path:
    """Create a minimal valid plugin source directory."""
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    (src / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "config_file": "config/config.json",
                "inject": {"app.api_key": "api_key"},
            }
        ),
        encoding="utf-8",
    )
    (src / "SKILL.md").write_text(
        "---\nname: test-plugin\ndescription: A test\n---\n# Test",
        encoding="utf-8",
    )
    config_dir = src / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"app": {"api_key": "PLACEHOLDER"}}),
        encoding="utf-8",
    )
    return src


def test_install_plugin(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    src = _make_source_plugin(tmp_path)

    install_plugin(
        source=src,
        plugins_dir=plugins_dir,
        host_values={"api_key": "sk-real"},
        host_name="pythinker-code",
        host_version="1.22.0",
    )

    installed = plugins_dir / "test-plugin"
    assert installed.is_dir()
    assert (installed / "SKILL.md").exists()

    # Host credentials are validated during install but injected as runtime env vars, not
    # persisted into plugin files.
    config = json.loads((installed / "config" / "config.json").read_text())
    assert config["app"]["api_key"] == "PLACEHOLDER"

    # Check runtime in plugin.json
    pj = json.loads((installed / "plugin.json").read_text())
    assert pj["runtime"]["host"] == "pythinker-code"
    assert pj["runtime"]["host_version"] == "1.22.0"


def test_install_plugin_missing_plugin_json(tmp_path: Path):
    src = tmp_path / "source" / "bad"
    src.mkdir(parents=True)

    with pytest.raises(PluginError, match="plugin.json"):
        install_plugin(
            source=src,
            plugins_dir=tmp_path / "plugins",
            host_values={},
            host_name="pythinker-code",
            host_version="1.0.0",
        )


def test_install_plugin_rollback_on_failure(tmp_path: Path):
    """If inject fails (missing host key), installed dir should not remain."""
    plugins_dir = tmp_path / "plugins"
    src = _make_source_plugin(tmp_path)

    with pytest.raises(PluginError):
        install_plugin(
            source=src,
            plugins_dir=plugins_dir,
            host_values={},  # missing api_key
            host_name="pythinker-code",
            host_version="1.0.0",
        )

    assert not (plugins_dir / "test-plugin").exists()


def test_reinstall_plugin(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    src = _make_source_plugin(tmp_path)

    install_plugin(
        source=src,
        plugins_dir=plugins_dir,
        host_values={"api_key": "sk-old"},
        host_name="pythinker-code",
        host_version="1.20.0",
    )
    install_plugin(
        source=src,
        plugins_dir=plugins_dir,
        host_values={"api_key": "sk-new"},
        host_name="pythinker-code",
        host_version="1.22.0",
    )

    config = json.loads((plugins_dir / "test-plugin" / "config" / "config.json").read_text())
    assert config["app"]["api_key"] == "PLACEHOLDER"

    pj = json.loads((plugins_dir / "test-plugin" / "plugin.json").read_text())
    assert pj["runtime"]["host_version"] == "1.22.0"


def test_list_plugins(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    src = _make_source_plugin(tmp_path, "alpha")

    install_plugin(
        source=src,
        plugins_dir=plugins_dir,
        host_values={"api_key": "k"},
        host_name="pythinker-code",
        host_version="1.0.0",
    )

    plugins = list_plugins(plugins_dir)
    assert len(plugins) == 1
    assert plugins[0].name == "alpha"


def test_list_plugins_empty(tmp_path: Path):
    assert list_plugins(tmp_path / "nonexistent") == []


def test_remove_plugin(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    src = _make_source_plugin(tmp_path)

    install_plugin(
        source=src,
        plugins_dir=plugins_dir,
        host_values={"api_key": "k"},
        host_name="pythinker-code",
        host_version="1.0.0",
    )
    assert (plugins_dir / "test-plugin").exists()

    remove_plugin("test-plugin", plugins_dir)
    assert not (plugins_dir / "test-plugin").exists()


def test_remove_nonexistent_plugin(tmp_path: Path):
    with pytest.raises(PluginError, match="not found"):
        remove_plugin("ghost", tmp_path / "plugins")


def test_install_rejects_path_traversal_name(tmp_path: Path):
    """Plugin name with '..' should be rejected."""
    src = tmp_path / "source" / "evil"
    src.mkdir(parents=True)
    (src / "plugin.json").write_text(
        json.dumps({"name": "../../escape", "version": "1.0.0"}),
        encoding="utf-8",
    )

    with pytest.raises(PluginError, match="Invalid plugin name"):
        install_plugin(
            source=src,
            plugins_dir=tmp_path / "plugins",
            host_values={},
            host_name="pythinker-code",
            host_version="1.0.0",
        )


def test_remove_rejects_plugin_root_name(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()

    with pytest.raises(PluginError, match="Invalid plugin name"):
        remove_plugin(".", plugins_dir)


@pytest.mark.asyncio
async def test_skill_discovery_includes_plugins_dir(tmp_path: Path, monkeypatch):
    """An installed plugin's ``skills/`` subdir is included in skill roots.

    Plugins live under ``<share>/plugins/cache/<marketplace>/<plugin>/<version>/``
    and contribute the skills nested inside them — not the plugins root itself.
    """
    from pythinker_host.path import HostPath

    from pythinker_code.skill import resolve_skills_roots

    # Install a plugin in the pythinker cache with a nested skill.
    plugin_root = tmp_path / "plugins" / "cache" / "market" / "my-plugin" / "1.0.0"
    manifest = plugin_root / ".pythinker-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"name": "my-plugin", "version": "1.0.0"}), encoding="utf-8")
    skills_dir = plugin_root / "skills"
    skill_md = skills_dir / "my-skill" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("---\nname: my-skill\ndescription: test\n---\n# Test", encoding="utf-8")

    # Point PYTHINKER_SHARE_DIR to tmp_path so get_plugins_dir() returns tmp_path/plugins
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))

    scoped = await resolve_skills_roots(HostPath(str(tmp_path)))
    root_strs = [str(s.root) for s in scoped]
    assert str(skills_dir.resolve()) in root_strs


# --- collect_host_values tests ---


def _make_config(*, api_key: str = "sk-test", oauth: object = None):
    """Build a minimal mock Config with a default model and provider."""
    provider = MagicMock()
    provider.api_key = SecretStr(api_key)
    provider.oauth = oauth
    provider.base_url = "https://api.example.com/v1"

    model = MagicMock()
    model.provider = "test-provider"

    config = MagicMock()
    config.default_model = "test-model"
    config.models = {"test-model": model}
    config.providers = {"test-provider": provider}
    return config


def test_collect_host_values_static_key():
    """Static API key (no OAuth) is returned correctly."""
    config = _make_config(api_key="sk-static-key")
    oauth = MagicMock()
    oauth.resolve_api_key.return_value = "sk-static-key"

    values = collect_host_values(config, oauth)
    assert values["api_key"] == "sk-static-key"
    assert values["base_url"] == "https://api.example.com/v1"


def test_collect_host_values_oauth_token():
    """OAuth token is returned when provider has OAuth configured."""
    oauth_ref = MagicMock()
    config = _make_config(api_key="", oauth=oauth_ref)
    oauth = MagicMock()
    oauth.resolve_api_key.return_value = "eyJ-oauth-token"

    values = collect_host_values(config, oauth)
    assert values["api_key"] == "eyJ-oauth-token"
    oauth.resolve_api_key.assert_called_once()


def test_collect_host_values_no_default_model():
    """Returns empty dict when no default_model is configured."""
    config = MagicMock()
    config.default_model = None
    oauth = MagicMock()

    values = collect_host_values(config, oauth)
    assert values == {}


def test_collect_host_values_empty_key():
    """Empty API key is not included in values."""
    config = _make_config(api_key="")
    oauth = MagicMock()
    oauth.resolve_api_key.return_value = ""

    values = collect_host_values(config, oauth)
    assert "api_key" not in values
