"""Shared MCP server/tool key normalization (mcpext-6 / task 4.6)."""

from __future__ import annotations

import hashlib
import re
from typing import Any, cast

from pythinker_code.exception import MCPConfigError

_MCP_NAME_MAX = 64
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def normalize_mcp_server_name(name: str) -> str:
    """Return a deterministic, path-safe MCP server key."""
    trimmed = name.strip()
    if not trimmed:
        raise MCPConfigError("MCP server name must not be empty")
    cleaned = _UNSAFE_CHARS.sub("_", trimmed).strip("._-")
    if not cleaned:
        digest = hashlib.sha256(trimmed.encode("utf-8")).hexdigest()[:8]
        cleaned = f"server_{digest}"
    if len(cleaned) <= _MCP_NAME_MAX:
        return cleaned
    digest = hashlib.sha256(trimmed.encode("utf-8")).hexdigest()[:8]
    head = cleaned[: _MCP_NAME_MAX - 9]
    return f"{head}_{digest}"


def mcp_tool_runtime_key(server_name: str, tool_name: str) -> str:
    """Build the runtime registry key for an MCP tool."""
    server = normalize_mcp_server_name(server_name)
    safe_tool = _UNSAFE_CHARS.sub("_", tool_name.strip()).strip("._-") or "tool"
    return f"mcp__{server}__{safe_tool}"


def normalize_mcp_servers_in_config(config: dict[str, Any]) -> dict[str, Any]:
    """Re-key ``mcpServers`` entries to normalized names; fail on collisions.

    Mutates ``config`` in place (reassigns ``config["mcpServers"]``) and returns
    the same dict for chaining.
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return config
    typed_servers = cast(dict[str, Any], servers)
    normalized: dict[str, Any] = {}
    raw_by_norm: dict[str, str] = {}
    for raw_name, server_config in typed_servers.items():
        norm = normalize_mcp_server_name(raw_name)
        if norm in normalized:
            other = raw_by_norm[norm]
            raise MCPConfigError(
                f"MCP server name collision: '{raw_name}' and '{other}' both normalize to '{norm}'"
            )
        raw_by_norm[norm] = raw_name
        normalized[norm] = server_config
    config["mcpServers"] = normalized
    return config
