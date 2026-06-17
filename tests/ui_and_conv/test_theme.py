"""Tests for terminal theme system (ui/theme.py) and /theme slash command."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from pythinker_core.tooling.empty import EmptyToolset

from pythinker_code.cli import Reload
from pythinker_code.config import Config, get_default_config
from pythinker_code.exception import ConfigError
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.ui.shell import Shell
from pythinker_code.ui.shell import slash as shell_slash
from pythinker_code.ui.theme import (
    get_active_theme,
    get_diff_colors,
    get_mcp_prompt_colors,
    get_prompt_style,
    get_task_browser_style,
    get_toolbar_colors,
    set_active_theme,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_theme():
    """Ensure every test starts and ends with dark theme."""
    set_active_theme("dark")
    yield
    set_active_theme("dark")


async def _run_theme(app: Shell, args: str) -> None:
    await cast(Awaitable[None], shell_slash.theme(app, args))


def _make_shell_app(runtime: Runtime, tmp_path: Path) -> SimpleNamespace:
    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    return SimpleNamespace(soul=soul)


# ---------------------------------------------------------------------------
# Theme module: set/get and color resolution
# ---------------------------------------------------------------------------


def test_set_and_get_active_theme():
    assert get_active_theme() == "dark"
    set_active_theme("light")
    assert get_active_theme() == "light"
    set_active_theme("dark")
    assert get_active_theme() == "dark"


@pytest.mark.parametrize(
    ("theme", "expected_add_bg_fragment"),
    [("dark", "#213A2B"), ("light", "#dafbe1")],
)
def test_diff_colors_by_theme(theme: str, expected_add_bg_fragment: str):
    set_active_theme(theme)  # type: ignore[arg-type]
    colors = get_diff_colors()
    assert expected_add_bg_fragment.lower() in str(colors.add_bg).lower()


def test_all_getters_respond_to_theme_switch():
    """Every color getter returns a different result after switching."""
    dark_diff = get_diff_colors()
    dark_toolbar = get_toolbar_colors()
    dark_mcp = get_mcp_prompt_colors()

    set_active_theme("light")

    assert get_diff_colors() != dark_diff
    assert get_toolbar_colors() != dark_toolbar
    assert get_mcp_prompt_colors() != dark_mcp


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_ptk_styles_valid_for_both_themes(theme: str):
    """get_prompt_style and get_task_browser_style return valid PTKStyle objects."""
    set_active_theme(theme)  # type: ignore[arg-type]
    prompt_style = get_prompt_style()
    browser_style = get_task_browser_style()
    assert prompt_style is not None
    assert browser_style is not None


# ---------------------------------------------------------------------------
# /theme slash command
# ---------------------------------------------------------------------------


def test_theme_command_registered_in_both_registries():
    from pythinker_code.ui.shell.slash import registry, shell_mode_registry

    agent_cmds = {c.name for c in registry.list_commands()}
    shell_cmds = {c.name for c in shell_mode_registry.list_commands()}
    assert "theme" in agent_cmds
    assert "theme" in shell_cmds


@pytest.mark.asyncio
async def test_theme_no_args_opens_selector_and_cancels(
    runtime: Runtime, tmp_path: Path, monkeypatch
):
    from pythinker_code.ui.shell.selectors import theme as theme_selector

    app = _make_shell_app(runtime, tmp_path)
    print_mock = Mock()
    monkeypatch.setattr(shell_slash.console, "print", print_mock)

    called: dict[str, object] = {}

    async def fake_run_theme_selector(**kwargs):
        called.update(kwargs)
        return None

    monkeypatch.setattr(theme_selector, "run_theme_selector", fake_run_theme_selector)

    await _run_theme(cast(Shell, app), "")

    assert called["current_theme"] == "dark"
    assert called["available_themes"] == ["dark", "light", "auto"]
    print_mock.assert_not_called()


@pytest.mark.asyncio
async def test_theme_invalid_arg(runtime: Runtime, tmp_path: Path, monkeypatch):
    app = _make_shell_app(runtime, tmp_path)
    print_mock = Mock()
    monkeypatch.setattr(shell_slash.console, "print", print_mock)

    await _run_theme(cast(Shell, app), "neon")

    assert "Unknown theme" in str(print_mock.call_args.args[0])


@pytest.mark.asyncio
async def test_theme_same_as_current(runtime: Runtime, tmp_path: Path, monkeypatch):
    app = _make_shell_app(runtime, tmp_path)
    print_mock = Mock()
    monkeypatch.setattr(shell_slash.console, "print", print_mock)

    await _run_theme(cast(Shell, app), "dark")

    assert "Already using" in str(print_mock.call_args.args[0])


@pytest.mark.asyncio
async def test_theme_switch_persists_and_reloads(runtime: Runtime, tmp_path: Path, monkeypatch):
    config_path = (tmp_path / "config.toml").resolve()
    runtime.config.source_file = config_path
    app = _make_shell_app(runtime, tmp_path)

    config_for_save = get_default_config()
    load_mock = Mock(return_value=config_for_save)
    save_mock = Mock()
    monkeypatch.setattr(shell_slash, "load_config", load_mock)
    monkeypatch.setattr(shell_slash, "save_config", save_mock)
    monkeypatch.setattr(shell_slash.console, "print", Mock())

    with pytest.raises(Reload) as exc_info:
        await _run_theme(cast(Shell, app), "light")

    load_mock.assert_called_once_with(config_path)
    save_mock.assert_called_once()
    assert config_for_save.theme == "light"
    assert exc_info.value.session_id == runtime.session.id


@pytest.mark.asyncio
async def test_theme_switch_light_to_dark(runtime: Runtime, tmp_path: Path, monkeypatch):
    """Reverse direction: light → dark also works."""
    set_active_theme("light")
    config_path = (tmp_path / "config.toml").resolve()
    runtime.config.source_file = config_path
    # The command compares against the persisted setting (so `/theme dark`
    # can pin a theme even when `auto` happened to resolve to dark).
    runtime.config.theme = "light"
    app = _make_shell_app(runtime, tmp_path)

    config_for_save = get_default_config()
    config_for_save.theme = "light"
    load_mock = Mock(return_value=config_for_save)
    save_mock = Mock()
    monkeypatch.setattr(shell_slash, "load_config", load_mock)
    monkeypatch.setattr(shell_slash, "save_config", save_mock)
    monkeypatch.setattr(shell_slash.console, "print", Mock())

    with pytest.raises(Reload):
        await _run_theme(cast(Shell, app), "dark")

    assert config_for_save.theme == "dark"


@pytest.mark.asyncio
async def test_theme_arg_case_and_whitespace(runtime: Runtime, tmp_path: Path, monkeypatch):
    """Args are stripped and lowercased: ' LIGHT ' should work."""
    config_path = (tmp_path / "config.toml").resolve()
    runtime.config.source_file = config_path
    app = _make_shell_app(runtime, tmp_path)

    config_for_save = get_default_config()
    monkeypatch.setattr(shell_slash, "load_config", Mock(return_value=config_for_save))
    monkeypatch.setattr(shell_slash, "save_config", Mock())
    monkeypatch.setattr(shell_slash.console, "print", Mock())

    with pytest.raises(Reload):
        await _run_theme(cast(Shell, app), "  LIGHT  ")

    assert config_for_save.theme == "light"


@pytest.mark.asyncio
async def test_theme_rejects_inline_config(runtime: Runtime, tmp_path: Path, monkeypatch):
    runtime.config.source_file = None
    app = _make_shell_app(runtime, tmp_path)

    load_mock = Mock()
    save_mock = Mock()
    print_mock = Mock()
    monkeypatch.setattr(shell_slash, "load_config", load_mock)
    monkeypatch.setattr(shell_slash, "save_config", save_mock)
    monkeypatch.setattr(shell_slash.console, "print", print_mock)

    await _run_theme(cast(Shell, app), "light")

    load_mock.assert_not_called()
    save_mock.assert_not_called()
    assert "config file" in str(print_mock.call_args.args[0]).lower()


@pytest.mark.asyncio
async def test_theme_save_failure_no_reload_no_state_change(
    runtime: Runtime, tmp_path: Path, monkeypatch
):
    """When save fails: no Reload raised, global theme and config unchanged."""
    config_path = (tmp_path / "config.toml").resolve()
    runtime.config.source_file = config_path
    runtime.config.theme = "dark"
    app = _make_shell_app(runtime, tmp_path)

    monkeypatch.setattr(shell_slash, "load_config", Mock(side_effect=ConfigError("disk full")))
    print_mock = Mock()
    monkeypatch.setattr(shell_slash.console, "print", print_mock)

    # Should NOT raise Reload
    await _run_theme(cast(Shell, app), "light")

    assert "Failed to save" in str(print_mock.call_args.args[0])
    assert get_active_theme() == "dark"
    assert runtime.config.theme == "dark"


# ---------------------------------------------------------------------------
# Config: theme field
# ---------------------------------------------------------------------------


def test_config_theme_defaults_and_validation():
    assert Config().theme == "dark"
    assert Config(theme="light").theme == "light"

    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Config(theme="neon")  # type: ignore[arg-type]


def test_config_theme_round_trip():
    data = Config(theme="light").model_dump()
    assert data["theme"] == "light"
    assert Config.model_validate(data).theme == "light"


# ---------------------------------------------------------------------------
# Shell startup initializes theme from config
# ---------------------------------------------------------------------------


async def test_shell_startup_initializes_theme_from_config(
    runtime: Runtime, tmp_path: Path, monkeypatch
):
    """Shell.run() should call set_active_theme with config.theme."""
    runtime.config.theme = "light"

    from pythinker_code.ui import theme as theme_mod
    from pythinker_code.ui.shell import Shell as RealShell
    from pythinker_code.ui.shell.visualize import _live_view as live_view_mod

    agent = Agent(
        name="Test Agent",
        system_prompt="Test system prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))
    shell = RealShell(soul)

    set_theme_mock = Mock(side_effect=set_active_theme)
    monkeypatch.setattr(theme_mod, "set_active_theme", set_theme_mock)

    @asynccontextmanager
    async def _no_keyboard_listener(_handler) -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr(live_view_mod, "_keyboard_listener", _no_keyboard_listener)

    # Shell.run(command=...) initializes theme then runs the command.
    # The command will fail (no LLM), but theme init happens first.
    await shell.run(command="hello")

    set_theme_mock.assert_called_with("light")


# ---------------------------------------------------------------------------
# Diff rendering respects theme
# ---------------------------------------------------------------------------


def test_prompt_glyph_is_light_text_dark():
    set_active_theme("dark")
    from pythinker_code.ui.theme import _PROMPT_STYLE_DARK, get_tui_tokens

    assert get_tui_tokens("dark").activity_label in _PROMPT_STYLE_DARK["compact-input.prompt"]


def test_render_diff_panel_both_themes():
    from rich.console import Console

    from pythinker_code.utils.rich.diff_render import (
        DiffLine,
        DiffLineKind,
        render_diff_panel,
    )

    hunk = [
        DiffLine(kind=DiffLineKind.ADD, old_num=0, new_num=1, code="added line"),
        DiffLine(kind=DiffLineKind.DELETE, old_num=1, new_num=0, code="deleted line"),
    ]

    for theme_name in ("dark", "light"):
        set_active_theme(theme_name)  # type: ignore[arg-type]
        panel = render_diff_panel("test.py", [hunk], added=1, removed=1)
        console = Console(width=80, force_terminal=True, color_system=None)
        with console.capture() as cap:
            console.print(panel, end="")
        output = cap.get()
        assert "added line" in output
        assert "deleted line" in output
