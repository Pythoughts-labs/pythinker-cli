"""Compact progress snapshot + renderer for the Workflow tool.

A port of the reference display module, trimmed to a single text block suitable
for a `ProgressNote` wire event.
"""

from __future__ import annotations

_STATUS_ICON = {"running": "●", "done": "✓", "error": "✗", "skipped": "-"}


class AgentSnapshot:
    __slots__ = ("id", "label", "phase", "status")

    def __init__(self, id: int, label: str, phase: str | None) -> None:
        self.id = id
        self.label = label
        self.phase = phase
        self.status = "running"


class WorkflowSnapshot:
    __slots__ = ("name", "description", "phases", "current_phase", "agents", "logs")

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self.phases: list[str] = []
        self.current_phase: str | None = None
        self.agents: list[AgentSnapshot] = []
        self.logs: list[str] = []

    def add_phase(self, title: str | None) -> None:
        if not title:
            return
        self.current_phase = title
        if title not in self.phases:
            self.phases.append(title)

    def start_agent(self, agent_id: int, label: str, phase: str | None) -> AgentSnapshot:
        self.add_phase(phase)
        agent = AgentSnapshot(agent_id, label, phase)
        self.agents.append(agent)
        return agent

    def end_agent(self, agent_id: int, *, error: str | None = None) -> None:
        # Matched by the engine's stable dispatch id, not by label: two
        # concurrent agents may share the same caller-supplied label, and a
        # label-based reverse search can mark the wrong entry done/error if
        # they finish out of dispatch order.
        for agent in self.agents:
            if agent.id == agent_id and agent.status == "running":
                agent.status = "error" if error else "done"
                return

    def mark_running_skipped(self) -> None:
        for agent in self.agents:
            if agent.status == "running":
                agent.status = "skipped"

    @property
    def running_count(self) -> int:
        return sum(1 for a in self.agents if a.status == "running")

    @property
    def done_count(self) -> int:
        return sum(1 for a in self.agents if a.status == "done")

    @property
    def error_count(self) -> int:
        return sum(1 for a in self.agents if a.status == "error")

    @property
    def skipped_count(self) -> int:
        return sum(1 for a in self.agents if a.status == "skipped")


def render_progress(snapshot: WorkflowSnapshot, max_agents: int = 6, max_logs: int = 3) -> str:
    state = ""
    if snapshot.error_count:
        state = f", {snapshot.error_count} errors"
    elif snapshot.running_count:
        state = f", {snapshot.running_count} running"
    lines = [
        f"◆ Workflow: {snapshot.name} ({snapshot.done_count}/{len(snapshot.agents)} done{state})"
    ]
    phase_order = list(snapshot.phases)
    if snapshot.current_phase and snapshot.current_phase not in phase_order:
        phase_order.append(snapshot.current_phase)
    for phase in phase_order:
        agents = [a for a in snapshot.agents if a.phase == phase]
        if not agents and snapshot.current_phase != phase:
            continue
        done = sum(1 for a in agents if a.status == "done")
        lines.append(f"  {phase} {done}/{len(agents)}")
        for agent in agents[-max_agents:]:
            lines.append(f"    #{agent.id} {_STATUS_ICON.get(agent.status, '?')} {agent.label}")
    # Membership in phase_order, not an id set of rendered rows: agents cut by
    # the per-phase max_agents tail must stay truncated, not reappear here.
    unphased = [a for a in snapshot.agents if a.phase not in phase_order]
    for agent in unphased[-max_agents:]:
        lines.append(f"    #{agent.id} {_STATUS_ICON.get(agent.status, '?')} {agent.label}")
    for message in snapshot.logs[-max_logs:]:
        lines.append(f"  log: {message}")
    return "\n".join(lines)
