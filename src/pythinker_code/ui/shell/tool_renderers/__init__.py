"""Pythinker tool renderer registry.

Each registered tool can supply two callables:

* ``render_call(ctx)`` produces the *call preview* (label + args summary).
* ``render_result(ctx, result)`` produces the *result preview* (output / diff).

Renderers return Rich renderables and are pure functions of their inputs —
the host ``ToolExecutionComponent`` (Phase 4) assembles them into a card with
status-tinted background.

This module is consumed only when the TUI ``style`` flag is ``"card"``; under
``"pythinker"``, the existing ``_ToolCallBlock._compose()`` worklog rendering
path is used unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from rich.console import RenderableType

# ---------------------------------------------------------------------------
# Render context types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolRenderContext:
    """Per-render context passed to renderer callables.

    Attributes:
        args: Parsed JSON arguments dict (may be partial during streaming).
        tool_call_id: Stable id of this tool call.
        cwd: Working directory of the agent session.
        execution_started: True after the tool has actually been dispatched
            (vs still streaming arguments).
        args_complete: True when the streaming JSON parser closed the args.
        is_partial: True when *result* is still streaming.
        has_result: True after the final/partial result payload is attached.
        expanded: True when the user has expanded the card.
        is_error: True when the tool result was an error.
        elapsed_s: Stable elapsed wall time after the tool finishes, or a live
            elapsed value while it is still running.
        width: Approximate terminal width available to the renderer.
        state: Per-renderer scratch dict — renderers can stash transient
            state across redraws here without touching the host component.
    """

    args: dict[str, Any]
    tool_call_id: str
    cwd: str = ""
    execution_started: bool = False
    args_complete: bool = False
    is_partial: bool = False
    has_result: bool = False
    expanded: bool = False
    is_error: bool = False
    elapsed_s: float | None = None
    width: int = 100
    state: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(slots=True)
class ToolResultPayload:
    """Normalized tool result passed to ``render_result``.

    Mirrors the canonical shape (``content`` blocks + ``details``) so renderers can
    be ported directly. Pythinker callers convert their ``ToolReturnValue``
    into this payload at the registry boundary.
    """

    text: str = ""
    is_error: bool = False
    details: dict[str, Any] = field(default_factory=dict[str, Any])


# ---------------------------------------------------------------------------
# Renderer definition
# ---------------------------------------------------------------------------


_RenderShell = Literal["default", "self"]
_CallRenderer = Callable[[ToolRenderContext], RenderableType | None]
_ResultRenderer = Callable[[ToolRenderContext, ToolResultPayload], RenderableType | None]


@dataclass(slots=True, frozen=True)
class ToolRenderDefinition:
    """Renderer for a single tool name.

    Attributes:
        name: Tool name as it appears on tool calls (e.g. ``"Bash"``).
        label: Human-friendly label shown on the card header.
        render_shell: ``"default"`` lets the host component supply the
            background/box framing; ``"self"`` means the renderer is fully
            responsible for its own visual treatment.
        render_call: Callable producing the call preview.
        render_result: Callable producing the result preview.
    """

    name: str
    label: str
    render_shell: _RenderShell = "default"
    render_call: _CallRenderer | None = None
    render_result: _ResultRenderer | None = None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, ToolRenderDefinition] = {}


def register_tool_renderer(definition: ToolRenderDefinition) -> None:
    """Register *definition* under its tool name. Last write wins."""
    _REGISTRY[definition.name] = definition


def get_tool_renderer(tool_name: str) -> ToolRenderDefinition | None:
    """Return the registered renderer for *tool_name*, or None."""
    return _REGISTRY.get(tool_name)


def clear_tool_renderers() -> None:
    """Drop all registered renderers. Intended for tests only."""
    _REGISTRY.clear()


def registered_tool_names() -> list[str]:
    """Return a snapshot of registered tool names. Intended for tests / debug."""
    return list(_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Built-in renderer wiring
# ---------------------------------------------------------------------------


def register_builtin_renderers() -> None:
    """Register all built-in renderers shipped in this subpackage.

    Imports happen lazily inside the function so the registry module itself
    has no side effects at import time — the host wiring layer is the one
    that calls this once at startup when the Pythinker style is active.
    """
    from pythinker_code.ui.shell.tool_renderers import (
        agent,
        ask_user,
        background,
        bash,
        edit,
        find,
        generic,
        grep,
        lsp,
        mcp_resource,
        memory,
        plan,
        read,
        read_media,
        skill,
        smart_search,
        think,
        todo,
        tool_search,
        web,
        worktree,
        write,
    )

    register_tool_renderer(generic.GENERIC_RENDERER)
    register_tool_renderer(read.READ_RENDERER)
    register_tool_renderer(read_media.READ_MEDIA_RENDERER)
    register_tool_renderer(write.WRITE_RENDERER)
    register_tool_renderer(edit.EDIT_RENDERER)
    register_tool_renderer(grep.GREP_RENDERER)
    register_tool_renderer(smart_search.SMART_SEARCH_RENDERER)
    register_tool_renderer(find.FIND_RENDERER)
    register_tool_renderer(bash.SHELL_RENDERER)
    register_tool_renderer(skill.SKILL_RENDERER)
    register_tool_renderer(lsp.LSP_RENDERER)
    register_tool_renderer(mcp_resource.LIST_MCP_RESOURCES_RENDERER)
    register_tool_renderer(mcp_resource.READ_MCP_RESOURCE_RENDERER)
    register_tool_renderer(agent.AGENT_RENDERER)
    register_tool_renderer(agent.RUN_AGENTS_RENDERER)
    register_tool_renderer(ask_user.ASK_USER_RENDERER)
    register_tool_renderer(think.THINK_RENDERER)
    register_tool_renderer(todo.TODO_RENDERER)
    register_tool_renderer(memory.MEMORY_RENDERER)
    register_tool_renderer(memory.RECALL_RENDERER)
    register_tool_renderer(memory.SCRATCHPAD_RENDERER)
    register_tool_renderer(tool_search.TOOL_SEARCH_RENDERER)
    register_tool_renderer(web.FETCH_RENDERER)
    register_tool_renderer(web.SEARCH_RENDERER)
    register_tool_renderer(background.TASK_LIST_RENDERER)
    register_tool_renderer(background.TASK_OUTPUT_RENDERER)
    register_tool_renderer(background.TASK_INPUT_RENDERER)
    register_tool_renderer(background.TASK_HANDOFF_RENDERER)
    register_tool_renderer(background.TASK_STOP_RENDERER)
    register_tool_renderer(worktree.ENTER_WORKTREE_RENDERER)
    register_tool_renderer(worktree.EXIT_WORKTREE_RENDERER)
    register_tool_renderer(plan.ENTER_PLAN_RENDERER)
    register_tool_renderer(plan.EXIT_PLAN_RENDERER)


__all__ = [
    "ToolRenderContext",
    "ToolRenderDefinition",
    "ToolResultPayload",
    "clear_tool_renderers",
    "get_tool_renderer",
    "register_builtin_renderers",
    "register_tool_renderer",
    "registered_tool_names",
]
