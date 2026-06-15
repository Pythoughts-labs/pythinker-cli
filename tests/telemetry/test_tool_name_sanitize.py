"""obs-eval-6: MCP/plugin tool names are sanitized before telemetry export."""

from __future__ import annotations

from pythinker_code.telemetry.names import sanitize_telemetry_tool_name


def test_mcp_tool_name_is_bounded_and_safe() -> None:
    raw = "mcp__my server!__tool/with/slashes"
    sanitized = sanitize_telemetry_tool_name(raw)
    assert sanitized.startswith("mcp__")
    assert "/" not in sanitized
    assert " " not in sanitized
    assert len(sanitized) <= 64


def test_builtin_tool_name_passes_through_when_safe() -> None:
    assert sanitize_telemetry_tool_name("ReadFile") == "ReadFile"


def test_unsafe_builtin_name_is_normalized() -> None:
    assert sanitize_telemetry_tool_name("weird tool!") == "weird_tool"


def test_empty_name_becomes_unknown() -> None:
    assert sanitize_telemetry_tool_name("   ") == "unknown_tool"
