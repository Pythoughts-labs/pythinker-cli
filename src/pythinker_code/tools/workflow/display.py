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

    def start_agent(self, label: str, phase: str | None) -> AgentSnapshot:
        self.add_phase(phase)
        agent = AgentSnapshot(len(self.agents) + 1, label, phase)
        self.agents.append(agent)
        return agent

    def end_agent(self, label: str, *, error: str | None = None) -> None:
        for agent in reversed(self.agents):
            if agent.label == label and agent.status == "running":
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


def render_progress(snapshot: WorkflowSnapshot, max_agents: int = 6) -> str:
    state = ""
    if snapshot.error_count:
        state = f", {snapshot.error_count} errors"
    elif snapshot.running_count:
        state = f", {snapshot.running_count} running"
    lines = [
        f"◆ Workflow: {snapshot.name} ({snapshot.done_count}/{len(snapshot.agents)} done{state})"
    ]
    rendered: set[int] = set()
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
            rendered.add(agent.id)
            lines.append(f"    #{agent.id} {_STATUS_ICON.get(agent.status, '?')} {agent.label}")
    unphased = [a for a in snapshot.agents if a.id not in rendered]
    for agent in unphased[-max_agents:]:
        lines.append(f"    #{agent.id} {_STATUS_ICON.get(agent.status, '?')} {agent.label}")
    return "\n".join(lines)
