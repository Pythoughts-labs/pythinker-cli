from __future__ import annotations

import asyncio
from typing import Annotated

import typer

cli = typer.Typer(help="Check for and install Pythinker CLI updates.")


@cli.callback(invoke_without_command=True)
def update(
    ctx: typer.Context,
    check_only: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Only check whether an update is available; don't install.",
        ),
    ] = False,
) -> None:
    """Check for and install Pythinker CLI updates."""
    if ctx.invoked_subcommand is not None:
        return

    from pythinker_code.ui.shell.update import UpdateIntent, UpdateResult
    from pythinker_code.ui.shell.update_orchestrator import run_update_job

    # The standalone CLI is its own foreground process: exiting to hand off to
    # the platform installer is expected, unlike in-shell updates which stage.
    intent = UpdateIntent.CHECK if check_only else UpdateIntent.INSTALL_AND_EXIT
    result = asyncio.run(run_update_job(print_output=True, intent=intent, source="cli"))
    if result in (
        UpdateResult.FAILED,
        UpdateResult.VERIFICATION_FAILED,
        UpdateResult.UNSUPPORTED,
    ):
        raise typer.Exit(1)


@cli.command("status")
def status_command() -> None:
    """Show the last recorded update job status."""
    from pythinker_code.ui.shell.update_orchestrator import read_update_status

    status = read_update_status()
    if status is None:
        typer.echo("No update job recorded.")
        return
    typer.echo(f"State: {status.state.value}")
    if status.result:
        typer.echo(f"Result: {status.result}")
    if status.current_version:
        typer.echo(f"Current version: {status.current_version}")
    if status.target_version:
        typer.echo(f"Target version: {status.target_version}")
    if status.message:
        typer.echo(f"Message: {status.message}")
    typer.echo(f"Log: {status.log_path}")


@cli.command("log")
def log_command(
    lines: Annotated[
        int,
        typer.Option("--lines", "-n", min=1, help="Number of log lines to show."),
    ] = 80,
) -> None:
    """Show the tail of the update log."""
    from pythinker_code.ui.shell.update_orchestrator import read_update_log_tail

    tail = read_update_log_tail(lines)
    if not tail:
        typer.echo("No update log recorded.")
        return
    typer.echo("\n".join(tail))
