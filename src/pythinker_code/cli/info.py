from __future__ import annotations

import json
import platform
from typing import Annotated, TypedDict

import typer


class InfoData(TypedDict):
    pythinker_code_version: str
    organization: str
    agent_spec_versions: list[str]
    wire_protocol_version: str
    python_version: str
    auto_update: bool | None
    auto_update_config: str | None
    auto_update_override: str | None


def _auto_update_info() -> tuple[bool | None, str | None, str | None]:
    """Return ``(effective_enabled, config_mode, override_reason)``.

    Every element is ``None`` when the status cannot be resolved. The whole
    block is guarded so an unreadable config or any other failure never turns
    the always-available ``info`` diagnostic into a crash.
    """
    try:
        from pythinker_code.config import Config, get_config_file, load_config
        from pythinker_code.update_policy import (
            auto_update_enabled,
            auto_update_override_reason,
        )

        override = auto_update_override_reason()
        # `load_config()` seeds a default config file when none exists; `info`
        # must stay read-only, so fall back to in-memory defaults when the user
        # has no config file yet rather than creating one as a side effect.
        config_exists = get_config_file(create=False).expanduser().exists()
        config = load_config() if config_exists else Config()
        return auto_update_enabled(config), config.auto_update.value, override
    except (OSError, ValueError, ImportError) as exc:
        # Read-only diagnostic: never abort `info`, but log the degraded path
        # instead of silently masking a real config/policy failure. ConfigError
        # and pydantic validation errors are ValueError subclasses.
        from pythinker_code.utils.logging import logger

        logger.debug("Could not resolve auto-update status for `info`: {}", exc)
        return None, None, None


def _collect_info() -> InfoData:
    from pythinker_code.agentspec import SUPPORTED_AGENT_SPEC_VERSIONS
    from pythinker_code.constant import ORGANIZATION, get_version
    from pythinker_code.wire.protocol import WIRE_PROTOCOL_VERSION

    auto_update_effective, auto_update_config, auto_update_override = _auto_update_info()

    return {
        "pythinker_code_version": get_version(),
        "organization": ORGANIZATION,
        "agent_spec_versions": [str(version) for version in SUPPORTED_AGENT_SPEC_VERSIONS],
        "wire_protocol_version": WIRE_PROTOCOL_VERSION,
        "python_version": platform.python_version(),
        "auto_update": auto_update_effective,
        "auto_update_config": auto_update_config,
        "auto_update_override": auto_update_override,
    }


def _auto_update_line(info: InfoData) -> str:
    effective = info["auto_update"]
    if effective is None:
        return "auto-update: unknown"
    state = "enabled" if effective else "disabled"
    detail = f"config auto_update={info['auto_update_config'] or 'unknown'}"
    override = info["auto_update_override"]
    if override:
        detail += f"; {override}"
    return f"auto-update: {state} ({detail})"


def _emit_info(json_output: bool) -> None:
    info = _collect_info()
    if json_output:
        typer.echo(json.dumps(info, ensure_ascii=False))
        return

    agent_versions_text = ", ".join(str(version) for version in info["agent_spec_versions"])

    lines = [
        f"pythinker-code version: {info['pythinker_code_version']}",
        f"developed by: {info['organization']}",
        f"agent spec versions: {agent_versions_text}",
        f"wire protocol: {info['wire_protocol_version']}",
        f"python version: {info['python_version']}",
        _auto_update_line(info),
    ]
    for line in lines:
        typer.echo(line)


cli = typer.Typer(help="Show version and protocol information.")


@cli.callback(invoke_without_command=True)
def info(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Output information as JSON.",
        ),
    ] = False,
):
    """Show version and protocol information."""
    _emit_info(json_output)
