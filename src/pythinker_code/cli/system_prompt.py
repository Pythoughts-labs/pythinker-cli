"""`pythinker system-prompt` — print an agent's assembled system prompt (read-only)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from pythinker_code.agentspec import get_agents_dir

cli = typer.Typer(help="Print the assembled system prompt for an agent.")


def _resolve_agent_file(name: str) -> Path:
    """Resolve a built-in agent name to its spec file.

    Checks primary agent dirs (``<name>/agent.yaml``, e.g. ``default``, ``okabe``)
    then built-in role specs (``default/<name>.yaml``, e.g. ``coder``, ``ask``).
    """
    agents_dir = get_agents_dir()
    for candidate in (agents_dir / name / "agent.yaml", agents_dir / "default" / f"{name}.yaml"):
        if candidate.exists():
            return candidate
    raise typer.BadParameter(
        f"Unknown agent '{name}'. Pass --agent-file for a custom spec, "
        "or a built-in name like 'default' or 'coder'."
    )


@cli.callback(invoke_without_command=True)
def system_prompt(
    agent: Annotated[
        str,
        typer.Option("--agent", "-a", help="Built-in agent name (e.g. default, coder, ask)."),
    ] = "default",
    agent_file: Annotated[
        Path | None,
        typer.Option("--agent-file", help="Path to an agent spec file (overrides --agent)."),
    ] = None,
    work_dir: Annotated[
        Path | None,
        typer.Option(
            "--work-dir",
            "-C",
            help="Directory whose context to render. Default: current directory.",
        ),
    ] = None,
) -> None:
    """Print the fully-assembled system prompt for an agent.

    Read-only: renders the prompt the agent would receive (work dir, OS, shell,
    AGENTS.md, and stable skill-catalogue metadata) without creating a session,
    authenticating, loading MCP, or inventing task-specific skill candidates.
    """
    from pythinker_host.path import HostPath

    from pythinker_code.config import load_config
    from pythinker_code.soul.agent import render_agent_system_prompt

    resolved = agent_file if agent_file is not None else _resolve_agent_file(agent)
    if not resolved.exists():
        raise typer.BadParameter(f"Agent spec not found: {resolved}")

    # Resolve the merged user/project/local scoped config so the dump reflects
    # runtime behaviour even before a user config file exists. persist=False keeps
    # the command read-only: no share-dir/lock creation, seeding, or auto-gitignore.
    config = load_config(persist=False)
    wd = (
        HostPath.unsafe_from_local_path(work_dir.resolve())
        if work_dir is not None
        else HostPath.cwd()
    )

    prompt = asyncio.run(render_agent_system_prompt(resolved, wd, config))
    typer.echo(prompt)
