"""Telemetry-safe tool name normalization (obs-eval-6).

Span and metric attributes must not carry raw MCP server paths, secrets, or
unbounded plugin identifiers. Runtime tool names stay unchanged for the model;
only telemetry exports use these sanitized labels.
"""

from __future__ import annotations

import hashlib
import re

_TELEMETRY_TOOL_NAME_MAX = 64
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def sanitize_telemetry_tool_name(name: str) -> str:
    """Return a bounded, path-safe tool label for spans and metrics."""
    trimmed = name.strip()
    if not trimmed:
        return "unknown_tool"
    if trimmed.startswith("mcp__"):
        parts = trimmed.split("__", 2)
        if len(parts) == 3:
            _prefix, server, tool = parts
            return _bounded_mcp_label(server, tool)
    cleaned = _UNSAFE_CHARS.sub("_", trimmed).strip("_")
    if not cleaned:
        cleaned = f"tool_{hashlib.sha256(trimmed.encode('utf-8')).hexdigest()[:8]}"
    return cleaned[:_TELEMETRY_TOOL_NAME_MAX]


def _bounded_mcp_label(server: str, tool: str) -> str:
    safe_server = _UNSAFE_CHARS.sub("_", server).strip("_") or "server"
    safe_tool = _UNSAFE_CHARS.sub("_", tool).strip("_") or "tool"
    label = f"mcp__{safe_server}__{safe_tool}"
    if len(label) <= _TELEMETRY_TOOL_NAME_MAX:
        return label
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:8]
    # Slice budget: "mcp__" (5) + server[:16] + "__" (2) + tool[:24] = 47, then
    # head[:55] + "_" + 8-char digest = 56 — always within _TELEMETRY_TOOL_NAME_MAX (64).
    head = f"mcp__{safe_server[:16]}__{safe_tool[:24]}"
    return f"{head[: _TELEMETRY_TOOL_NAME_MAX - 9]}_{digest}"
