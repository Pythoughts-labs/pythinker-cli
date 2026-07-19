from __future__ import annotations

import json
from pathlib import Path

import pytest

from pythinker_code import config as config_module
from pythinker_code.config import Config, save_config


def test_save_config_preserves_previous_file_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    previous = 'default_model = "existing/model"\n'
    config_path.write_text(previous, encoding="utf-8")

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        save_config(Config(), config_path)

    assert config_path.read_text(encoding="utf-8") == previous
    assert list(tmp_path.glob(".config.toml.*.tmp")) == []


def test_save_config_preserves_symlink_and_uses_link_format(tmp_path: Path) -> None:
    target = tmp_path / "config-target"
    config_path = tmp_path / "config.json"
    config_path.symlink_to(target)

    save_config(Config(), config_path)

    assert config_path.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8"))["default_model"] == ""
