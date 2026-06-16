"""Tests for MCP server name normalization (task 4.6)."""

from __future__ import annotations

import re

import pytest

from pythinker_code.exception import MCPConfigError
from pythinker_code.utils.mcp_names import (
    mcp_tool_runtime_key,
    normalize_mcp_server_name,
    normalize_mcp_servers_in_config,
)


def test_normalize_replaces_spaces_and_slashes() -> None:
    assert normalize_mcp_server_name("my server/v2") == "my_server_v2"


def test_normalize_is_idempotent_for_safe_names() -> None:
    assert normalize_mcp_server_name("context7") == "context7"


def test_normalize_bounds_overlong_names() -> None:
    long_name = "a" * 100
    normalized = normalize_mcp_server_name(long_name)
    assert len(normalized) <= 64
    # Truncated names keep a deterministic 8-char hex hash suffix so distinct
    # overlong names don't collide (real contract, not a tautology).
    assert re.search(r"_[0-9a-f]{8}$", normalized)
    assert normalized != normalize_mcp_server_name("b" * 100)


def test_empty_name_raises() -> None:
    with pytest.raises(MCPConfigError, match="empty"):
        normalize_mcp_server_name("   ")


def test_collision_surfaces_in_config() -> None:
    config = {
        "mcpServers": {
            "foo bar": {"command": "echo"},
            "foo_bar": {"command": "echo"},
        }
    }
    with pytest.raises(MCPConfigError, match="collision"):
        normalize_mcp_servers_in_config(config)


def test_mcp_tool_runtime_key_uses_normalized_server() -> None:
    assert mcp_tool_runtime_key("my server", "read_file") == "mcp__my_server__read_file"
