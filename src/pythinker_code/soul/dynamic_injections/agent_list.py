from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from pythinker_core.message import Message

from pythinker_code.soul import wire_send
from pythinker_code.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider
from pythinker_code.subagents.models import AgentTypeDefinition
from pythinker_code.utils.logging import logger
from pythinker_code.wire.types import AgentListDelta

if TYPE_CHECKING:
    from pythinker_code.soul.pythinkersoul import PythinkerSoul


def format_agent_line(agent: AgentTypeDefinition) -> str:
    if agent.tool_policy.mode == "allowlist":
        tools = ", ".join(_unique_tool_names(agent.tool_policy.tools)) or "(none)"
    else:
        tools = "*"
    when = f" When to use: {' '.join(agent.when_to_use.split())}" if agent.when_to_use else ""
    return f"- `{agent.name}`: {agent.description} (Tools: {tools}).{when}"


def _unique_tool_names(tool_paths: tuple[str, ...]) -> list[str]:
    names: list[str] = []
    for path in tool_paths:
        name = path.split(":")[-1]
        if name not in names:
            names.append(name)
    return names


class AgentListInjectionProvider(DynamicInjectionProvider):
    """Inject the live built-in agent list for the root session only."""

    def __init__(self) -> None:
        self._last_fingerprint: tuple[str, ...] | None = None

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        if soul.is_subagent:
            return []
        del history
        agents = sorted(
            soul.runtime.labor_market.builtin_types.values(),
            key=lambda item: item.name,
        )
        lines = tuple(format_agent_line(agent) for agent in agents)
        if not lines:
            return []
        if lines == self._last_fingerprint:
            return []
        self._last_fingerprint = lines
        _emit_agent_list_delta(lines)
        return [
            DynamicInjection(
                type="agent_list",
                content=(
                    "Available agent types (regenerated when subagent specs change):\n"
                    + "\n".join(lines)
                ),
            )
        ]

    async def on_context_compacted(self) -> None:
        self._last_fingerprint = None

    def rearm(self, key: str) -> bool:
        if key != "agent_list":
            return False
        self._last_fingerprint = None
        return True


def _emit_agent_list_delta(lines: tuple[str, ...]) -> None:
    try:
        wire_send(AgentListDelta(items=lines, complete=True))
    except Exception as exc:  # noqa: BLE001 - prompt injection must not fail on UI telemetry
        logger.debug("Failed to emit AgentListDelta wire event: {error}", error=exc)
