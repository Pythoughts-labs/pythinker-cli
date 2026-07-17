"""Tests for `pythinker info` auto-update reporting."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from pythinker_code.cli.info import InfoData, _auto_update_line, _collect_info


def _info(**overrides: object) -> InfoData:
    data: dict[str, object] = {
        "pythinker_code_version": "0.0.0",
        "organization": "x",
        "agent_spec_versions": ["1"],
        "wire_protocol_version": "1",
        "python_version": "3.14.0",
        "auto_update": None,
        "auto_update_config": None,
        "auto_update_override": None,
    }
    data.update(overrides)
    return cast(InfoData, data)


def test_auto_update_line_enabled() -> None:
    line = _auto_update_line(_info(auto_update=True, auto_update_config="download"))
    assert line == "auto-update: enabled (config auto_update=download)"


def test_auto_update_line_disabled_with_override() -> None:
    line = _auto_update_line(
        _info(
            auto_update=False,
            auto_update_config="download",
            auto_update_override="disabled for source checkouts",
        )
    )
    assert line == (
        "auto-update: disabled (config auto_update=download; disabled for source checkouts)"
    )


def test_auto_update_line_unknown_when_unresolved() -> None:
    assert _auto_update_line(_info()) == "auto-update: unknown"


def test_collect_info_includes_auto_update_keys() -> None:
    info = _collect_info()
    assert "auto_update" in info
    assert "auto_update_config" in info
    assert "auto_update_override" in info


def test_collect_info_does_not_create_share_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `info` is read-only: resolving auto-update status must not materialize the
    # share directory (regression — `get_config_file()` used to create it).
    share = tmp_path / ".pythinker"
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(share))

    _collect_info()

    assert not share.exists()
