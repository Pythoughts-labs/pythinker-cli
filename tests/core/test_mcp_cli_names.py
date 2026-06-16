"""MCP CLI name resolution round-trip (task 4.6 / mcpext-6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pythinker_code.cli.mcp import _resolve_mcp_server_key, cli


@pytest.fixture
def mcp_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "my_server_v2": {"command": "echo", "args": ["hello"]},
                }
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_remove_accepts_display_name_with_spaces_and_slashes(
    mcp_config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["remove", "my server/v2"])
    assert result.exit_code == 0, result.output
    saved = json.loads(mcp_config_file.read_text(encoding="utf-8"))
    assert saved["mcpServers"] == {}


def test_resolve_mcp_server_key_maps_raw_name_to_stored_key() -> None:
    servers = {"my_server_v2": {"command": "echo"}}
    assert _resolve_mcp_server_key("my server/v2", servers) == "my_server_v2"


def test_resolve_mcp_server_key_accepts_already_normalized_name() -> None:
    servers = {"context7": {"url": "https://example.com/mcp", "transport": "http"}}
    assert _resolve_mcp_server_key("context7", servers) == "context7"
