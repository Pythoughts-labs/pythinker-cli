from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

from prompt_toolkit.shortcuts.choice_input import ChoiceInput
from rich.markup import escape

from pythinker_code.auth.platforms import get_platform_name_for_provider, refresh_managed_models
from pythinker_code.cli import Reload, SwitchToDashboard, SwitchToWeb
from pythinker_code.config import AutoUpdateMode, StatusLineConfig, load_config, save_config
from pythinker_code.exception import ConfigError
from pythinker_code.session import Session
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.ui.shell.console import console
from pythinker_code.ui.shell.mcp_status import render_mcp_console
from pythinker_code.ui.shell.task_browser import TaskBrowserApp
from pythinker_code.utils.changelog import CHANGELOG
from pythinker_code.utils.logging import logger
from pythinker_code.utils.slashcmd import SlashCommand, SlashCommandRegistry

if TYPE_CHECKING:
    from pythinker_code.ui.shell import Shell
    from pythinker_code.wire.types import MCPStatusSnapshot

type ShellSlashCmdFunc = Callable[[Shell, str], None | Awaitable[None]]
"""
A function that runs as a Shell-level slash command.

Raises:
    Reload: When the configuration should be reloaded.
"""


registry = SlashCommandRegistry[ShellSlashCmdFunc]()
shell_mode_registry = SlashCommandRegistry[ShellSlashCmdFunc]()


def _rich_escape(value: object) -> str:
    return escape(str(value))


def ensure_pythinker_soul(app: Shell) -> PythinkerSoul | None:
    if not isinstance(app.soul, PythinkerSoul):
        from pythinker_code.ui.theme import get_tui_tokens as _get_tui_tokens_local

        _t = _get_tui_tokens_local()
        console.print(f"[{_t.error}]PythinkerSoul required[/]")
        return None
    return app.soul


@registry.command(aliases=["quit"])
@shell_mode_registry.command(aliases=["quit"])
def exit(app: Shell, args: str):
    """Exit the application"""
    # should be handled by `Shell`
    raise NotImplementedError


SKILL_COMMAND_PREFIX = "skill:"

# Ordered first-token hints for slash commands with fixed subcommands (ghost text + menu).
_THEME_ARGS: tuple[str, ...] = ("current", "doctor", "tokens", "code", "dark", "light", "auto")
_PROMPT_HISTORY_ARGS: tuple[str, ...] = ("status", "clear")


def slash_command_arg_suggestions() -> dict[str, tuple[str, ...]]:
    """First-token argument hints keyed by slash command name or alias (lowercase)."""
    return {
        "theme": _THEME_ARGS,
        "color": _THEME_ARGS,
        "prompt-history": _PROMPT_HISTORY_ARGS,
    }


_KEYBOARD_SHORTCUTS = [
    ("Ctrl-X", "Toggle agent/shell mode"),
    ("Shift-Tab", "Toggle plan mode (read-only research)"),
    ("Ctrl-O", "Edit in external editor ($VISUAL/$EDITOR)"),
    ("Ctrl-J / Alt-Enter", "Insert newline"),
    ("Ctrl-V", "Paste (supports images)"),
    ("Ctrl-L", "Repaint the screen (recover a blank/corrupted display)"),
    ("Ctrl-D", "Exit"),
    ("Ctrl-C", "Interrupt"),
]


@registry.command(aliases=["h", "?"], available_during_task=True)
@shell_mode_registry.command(aliases=["h", "?"])
def help(app: Shell, args: str):
    """Show help information"""
    from rich.console import Group, RenderableType
    from rich.text import Text

    from pythinker_code.ui.theme import get_tui_tokens, tui_rich_style
    from pythinker_code.utils.rich.columns import BulletColumns

    _tok = get_tui_tokens()

    def section(title: str, items: list[tuple[str, str]], color: str) -> BulletColumns:
        lines: list[RenderableType] = [Text.from_markup(f"[bold]{_rich_escape(title)}:[/bold]")]
        for name, desc in items:
            lines.append(
                BulletColumns(
                    Text.from_markup(
                        f"[{color}]{_rich_escape(name)}[/]: [{_tok.muted}]{_rich_escape(desc)}[/]"
                    ),
                    bullet_style=color,
                )
            )
        return BulletColumns(Group(*lines))

    renderables: list[RenderableType] = []
    renderables.append(
        BulletColumns(
            Group(
                Text.from_markup(
                    f"[{_tok.muted}]Help! I need somebody. Help! Not just anybody.[/]"
                ),
                Text.from_markup(f"[{_tok.muted}]Help! You know I need someone. Help![/]"),
                Text.from_markup(f"[{_tok.muted}]\u2015 The Beatles, [italic]Help![/italic][/]"),
            ),
            bullet_style=tui_rich_style("muted"),
        )
    )
    renderables.append(
        BulletColumns(
            Text(
                "Sure, Pythinker is ready to help! "
                "Just send me messages and I will help you get things done!"
            ),
        )
    )

    commands: list[SlashCommand[Any]] = []
    skills: list[SlashCommand[Any]] = []
    for cmd in app.available_slash_commands.values():
        if cmd.name.startswith(SKILL_COMMAND_PREFIX):
            skills.append(cmd)
        else:
            commands.append(cmd)

    renderables.append(section("Keyboard shortcuts", _KEYBOARD_SHORTCUTS, _tok.warning))
    renderables.append(
        section(
            "Slash commands",
            [(c.slash_name(), c.description) for c in sorted(commands, key=lambda c: c.name)],
            "blue",  # needs-human: blue has no exact brand token equivalent
        )
    )
    if skills:
        renderables.append(
            section(
                "Skills",
                [(c.slash_name(), c.description) for c in sorted(skills, key=lambda c: c.name)],
                _tok.info,
            )
        )

    with console.pager(styles=True):
        console.print(Group(*renderables))


@registry.command(available_during_task=True)
@shell_mode_registry.command
def version(app: Shell, args: str):
    """Show version information"""
    from pythinker_code.constant import VERSION

    console.print(f"pythinker, version {VERSION}")


@registry.command(name="prompt-history", available_during_task=True)
@shell_mode_registry.command(name="prompt-history")
def prompt_history(app: Shell, args: str) -> None:
    """Inspect or clear prompt history. Usage: /prompt-history status|clear"""
    from pythinker_code.ui.shell.prompt import CustomPromptSession
    from pythinker_code.ui.shell.prompting.history import PromptHistoryError
    from pythinker_code.ui.theme import get_tui_tokens

    tokens = get_tui_tokens()
    action = args.strip().lower()
    if action not in _PROMPT_HISTORY_ARGS:
        console.print(f"[{tokens.error}]Usage: /prompt-history status|clear[/]")
        return

    prompt_session = getattr(app, "_prompt_session", None)
    if not isinstance(prompt_session, CustomPromptSession):
        console.print(
            f"[{tokens.error}]Prompt history is unavailable because no shell session is active.[/]"
        )
        return
    store = prompt_session.prompt_history_store
    if action == "status":
        status = store.status()
        state = "enabled" if status.enabled else "disabled"
        total_size = status.size_bytes + status.rotated_size_bytes
        console.print(
            f"Prompt history: [{tokens.info}]{state}[/] · {status.entries} entries · "
            f"{total_size} bytes\n"
            f"[{tokens.muted}]current: {_rich_escape(status.path)}\n"
            f"rotated: {_rich_escape(status.rotated_path)}[/]"
        )
        return

    try:
        status = store.clear()
    except PromptHistoryError as exc:
        console.print(
            f"[{tokens.error}]Failed to clear prompt history: {_rich_escape(exc)}. "
            "Close other Pythinker sessions using this workspace and try again.[/]"
        )
        return
    console.print(f"[{tokens.success}]Prompt history cleared: {status.entries} entries remain.[/]")


@registry.command(available_during_task=True)
@shell_mode_registry.command
def agents(app: Shell, args: str):
    """List available subagent types"""
    from rich import box
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    from pythinker_code.soul.agent import agent_type_definitions

    builtin_types = agent_type_definitions(soul.runtime)
    type_defs = sorted(builtin_types.values(), key=lambda item: item.name)
    from pythinker_code.ui.theme import get_tui_tokens, tui_rich_style

    _tok = get_tui_tokens()

    if not type_defs:
        console.print(f"[{_tok.warning}]No subagents are registered for this agent.[/]")
        return

    table = Table.grid(expand=True)
    table.add_column(ratio=1, no_wrap=True)
    table.add_column(ratio=4)
    table.add_column(ratio=1, no_wrap=True)
    table.add_column(ratio=2, no_wrap=True)
    from rich.style import Style as _RichStyle

    table.add_row(
        Text("agent", style=tui_rich_style("info") + _RichStyle(bold=True)),
        Text("when to use", style="bold"),
        Text("model", style="bold"),
        Text("tools", style="bold"),
    )
    for type_def in type_defs:
        tool_policy = getattr(type_def, "tool_policy", None)
        tool_label = (
            f"allow {len(tool_policy.tools)}"
            if tool_policy is not None and tool_policy.mode == "allowlist"
            else "inherit"
        )
        table.add_row(
            Text(type_def.name, style=tui_rich_style("info")),
            Text(
                type_def.when_to_use or type_def.description or "—", style=tui_rich_style("muted")
            ),
            Text(type_def.default_model or "inherit", style=tui_rich_style("dim")),
            Text(tool_label, style=tui_rich_style("dim")),
        )

    footer = Text("Use the Agent tool with subagent_type=<agent>, or ask Pythinker to delegate.")
    footer.stylize(tui_rich_style("dim"))
    console.print(
        Panel(
            table,
            title=f"[bold {_tok.info}]Agents[/]",
            subtitle=footer,
            border_style=tui_rich_style("border"),
            box=box.ROUNDED,
        )
    )


@registry.command
async def model(app: Shell, args: str):
    """Switch LLM model or thinking mode"""
    from pythinker_code.llm import ModelCapability, derive_model_capabilities
    from pythinker_code.thinking import model_uses_native_thinking
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok

    _t = _get_tok()

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return
    config = soul.runtime.config

    await refresh_managed_models(config)

    if not config.models:
        console.print(f'[{_t.warning}]No models configured, send "/login" to login.[/]')
        return

    if not config.is_from_default_location:
        console.print(
            f"[{_t.warning}]Model switching requires the default config file; "
            f"restart without --config/--config-file.[/]"
        )
        return

    # Find current model/thinking from runtime (may be overridden by --model/--thinking)
    curr_model_cfg = soul.runtime.llm.model_config if soul.runtime.llm else None
    curr_model_name: str | None = None
    if curr_model_cfg is not None:
        for name, model_cfg in config.models.items():
            if model_cfg == curr_model_cfg:
                curr_model_name = name
                break
    curr_capabilities: set[ModelCapability] = (
        derive_model_capabilities(curr_model_cfg) if curr_model_cfg else set()
    )
    curr_effort = soul.thinking_effort or ("high" if soul.thinking else "off")
    if model_uses_native_thinking(curr_capabilities):
        curr_effort = "off"

    # Step 1: Pick a model — single grouped picker with type-to-filter.
    from pythinker_code.ui.shell.model_picker import ModelPickerApp, build_provider_groups

    groups = build_provider_groups(
        config_models=config.models,
        label_for=_provider_label,
    )
    selected_model_name = await ModelPickerApp(
        groups=groups,
        current_model_name=curr_model_name,
    ).run()
    if not selected_model_name:
        return

    selected_model_cfg = config.models[selected_model_name]
    selected_provider = config.providers.get(selected_model_cfg.provider)
    if selected_provider is None:
        console.print(
            f"[{_t.error}]Provider not found: {_rich_escape(selected_model_cfg.provider)}[/]"
        )
        return

    # Step 2: Determine thinking effort
    capabilities = derive_model_capabilities(selected_model_cfg)
    from pythinker_code.llm import (
        available_model_thinking_levels,
        resolve_provider_compatibility,
    )
    from pythinker_code.thinking import clamp_thinking_effort
    from pythinker_code.ui.shell.selectors.thinking import ThinkingLevel, run_thinking_selector

    native_thinking = model_uses_native_thinking(capabilities)
    compatibility = resolve_provider_compatibility(
        selected_model_cfg.provider,
        selected_provider,
        selected_model_cfg,
    )
    available_efforts = available_model_thinking_levels(
        selected_model_cfg,
        capabilities,
        compatibility,
    )
    if native_thinking or available_efforts == ("off",):
        new_effort = "off"
    else:
        current_level = clamp_thinking_effort(curr_effort, available_efforts)
        _level = await run_thinking_selector(
            current_level=cast(ThinkingLevel, current_level),
            available_levels=[cast(ThinkingLevel, level) for level in available_efforts],
        )
        if _level is None:
            return
        new_effort = _level
    new_thinking = new_effort != "off"
    thinking_label = "native reasoning" if native_thinking else f"thinking {new_effort}"

    # Check if anything changed
    model_changed = curr_model_name != selected_model_name
    thinking_changed = curr_effort != new_effort
    selected_display = selected_model_cfg.display_name or selected_model_cfg.model

    if not model_changed and not thinking_changed:
        console.print(
            f"[{_t.warning}]Already using {_rich_escape(selected_display)} "
            f"with {thinking_label}.[/]"
        )
        return

    # Save and reload
    prev_model = config.default_model
    prev_thinking = config.default_thinking
    prev_effort = config.default_thinking_effort
    config.default_model = selected_model_name
    config.default_thinking = new_thinking
    config.default_thinking_effort = new_effort
    try:
        config_for_save = load_config()
        config_for_save.default_model = selected_model_name
        config_for_save.default_thinking = new_thinking
        config_for_save.default_thinking_effort = new_effort
        save_config(config_for_save)
    except (ConfigError, OSError) as exc:
        config.default_model = prev_model
        config.default_thinking = prev_thinking
        config.default_thinking_effort = prev_effort
        console.print(f"[{_t.error}]Failed to save config: {_rich_escape(exc)}[/]")
        return

    from pythinker_code.telemetry import track

    if model_changed:
        track("model_switch", model=selected_model_name)
    if thinking_changed:
        track("thinking_toggle", enabled=new_thinking, level=new_effort)
    console.print(
        f"[{_t.success}]Switched to {selected_display} with {thinking_label}. Reloading...[/]"
    )

    # Pre-load LM Studio models so the user doesn't hit a 10-60s wait on
    # the first message. Fire-and-forget on error — Reload still proceeds.
    if model_changed and selected_model_cfg.provider == "managed:lm-studio":
        await _preload_lm_studio_model(selected_provider, selected_model_cfg.model)

    if model_changed:
        current_session = soul.runtime.session
        session = await Session.create(current_session.work_dir)
        session.state.additional_dirs = list(current_session.state.additional_dirs)
        if session.state.additional_dirs:
            await asyncio.to_thread(session.save_state)
        carried = False
        if config.model_switch_carryover:
            carried = await _carry_context_to_session(soul, session)
        if carried:
            console.print(f"[{_t.success}]Carrying a conversation summary to the new model...[/]")
        else:
            console.print(f"[{_t.success}]Starting fresh session for the new model...[/]")
        raise Reload(session_id=session.id)

    raise Reload(session_id=soul.runtime.session.id)


async def _carry_context_to_session(soul: PythinkerSoul, new_session: Any) -> bool:
    """Seed the new session with a summary written by the outgoing model.

    Summarizing before the switch means only plain text crosses the model
    boundary — no provider-specific thinking blocks or tool-call schemas the
    incoming provider might reject. Best-effort: any failure leaves the new
    session untouched so the switch degrades to the start-fresh behavior.
    """
    from pythinker_code.soul.compaction import SimpleCompaction
    from pythinker_code.soul.context import Context
    from pythinker_code.soul.message import system
    from pythinker_code.utils.logging import logger

    history = list(soul.context.history)
    llm = soul.runtime.llm
    if not history or llm is None:
        return False
    compaction = SimpleCompaction(base_prompt=soul.runtime.config.compact_prompt)
    try:
        summary = await compaction.summarize_all(history, llm)
    except Exception:
        logger.warning("Model-switch carry-over summarization failed", exc_info=True)
        return False
    if not summary:
        return False
    from pythinker_core.message import Message

    seed = Message(
        role="system",
        content=[
            system(
                "Summary of the conversation so far, carried over from the previous "
                f"model session:\n{summary}\nContinue from this state. Details were "
                "summarized away; re-read files before editing them."
            )
        ],
    )
    await Context(file_backend=new_session.context_file).append_message(seed)
    return True


_PROVIDER_LABEL_OVERRIDES = {
    "managed:minimax-anthropic": "MiniMax",
    "managed:opencode-go-openai": "OpenCode Go (OpenAI)",
    "managed:opencode-go-anthropic": "OpenCode Go (Anthropic)",
    "managed:deepseek": "DeepSeek",
    "managed:anthropic": "Anthropic",
    "managed:openrouter": "OpenRouter",
}


def _provider_label(provider_key: str) -> str:
    if name := get_platform_name_for_provider(provider_key):
        return name
    if name := _PROVIDER_LABEL_OVERRIDES.get(provider_key):
        return name
    # Fall back: strip "managed:" prefix and humanize.
    raw = provider_key.removeprefix("managed:")
    return raw.replace("-", " ").title() if raw else provider_key


async def _preload_lm_studio_model(provider: Any, model_id: str) -> None:
    """Best-effort: ask LM Studio to load the model now."""
    from rich.status import Status

    from pythinker_code.auth.lm_studio import request_lm_studio_load
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_lms

    _t_lms = _get_tok_lms()

    base_url = provider.base_url
    api_key = provider.api_key.get_secret_value()
    status_msg = f"Pre-loading {model_id} in LM Studio (this may take a moment)..."
    status = Status(status_msg, console=console)
    status.start()
    try:
        result = await request_lm_studio_load(base_url=base_url, model_id=model_id, api_key=api_key)
        status.stop()
        console.print(
            f"[dim]LM Studio loaded {model_id} in "
            f"{result.load_time_seconds:.1f}s (status={result.status}).[/dim]"
        )
    except Exception as exc:
        status.stop()
        console.print(
            f"[{_t_lms.warning}]LM Studio pre-load failed for {model_id}: {exc}[/]\n"
            "[dim]The chat will still try the model on first message.[/dim]"
        )


@registry.command
@shell_mode_registry.command
async def editor(app: Shell, args: str):
    """Set default external editor for Ctrl-O"""
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_editor
    from pythinker_code.utils.editor import get_editor_command

    _t_ed = _get_tok_editor()

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return
    config = soul.runtime.config
    config_file = config.source_file
    if config_file is None:
        console.print(
            f"[{_t_ed.warning}]Editor switching is unavailable with inline --config; "
            f"use --config-file to persist this setting.[/]"
        )
        return

    current_editor = config.default_editor

    # If args provided directly, use as editor command
    if args.strip():
        new_editor = args.strip()
    else:
        options: list[tuple[str, str]] = [
            ("code --wait", "VS Code (code --wait)"),
            ("vim", "Vim"),
            ("nano", "Nano"),
            ("", "Auto-detect (use $VISUAL/$EDITOR)"),
        ]
        # Mark current selection
        options = [
            (val, label + (" ← current" if val == current_editor else "")) for val, label in options
        ]

        try:
            choice = cast(
                str | None,
                await ChoiceInput(
                    message="Select an editor (↑↓ navigate, Enter select, Ctrl+C cancel):",
                    options=options,
                    default=(
                        current_editor
                        if current_editor in {v for v, _ in options}
                        else "code --wait"
                    ),
                ).prompt_async(),
            )
        except (EOFError, KeyboardInterrupt):
            return

        if choice is None:
            return
        new_editor = choice

    # Validate the editor binary is available
    if new_editor:
        import shlex
        import shutil

        try:
            parts = shlex.split(new_editor)
        except ValueError:
            console.print(f"[{_t_ed.error}]Invalid editor command: {_rich_escape(new_editor)}[/]")
            return

        binary = parts[0]
        if not shutil.which(binary):
            console.print(
                f"[{_t_ed.warning}]Warning: '{_rich_escape(binary)}' not found in PATH. "
                f"Saving anyway — make sure it's installed before using Ctrl-O.[/]"
            )

    if new_editor == current_editor:
        editor_label = _rich_escape(new_editor or "auto-detect")
        console.print(f"[{_t_ed.warning}]Editor is already set to: {editor_label}[/]")
        return

    # Save to disk
    try:
        config_for_save = load_config(config_file)
        config_for_save.default_editor = new_editor
        save_config(config_for_save, config_file)
    except (ConfigError, OSError) as exc:
        console.print(f"[{_t_ed.error}]Failed to save config: {_rich_escape(exc)}[/]")
        return

    # Sync in-memory config so Ctrl-O picks it up immediately
    config.default_editor = new_editor

    if new_editor:
        console.print(f"[{_t_ed.success}]Editor set to: {_rich_escape(new_editor)}[/]")
    else:
        resolved = get_editor_command()
        label = " ".join(resolved) if resolved else "none"
        console.print(
            f"[{_t_ed.success}]Editor set to auto-detect (resolved: {_rich_escape(label)})[/]"
        )


@registry.command(aliases=["release-notes"], available_during_task=True)
@shell_mode_registry.command(aliases=["release-notes"])
def changelog(app: Shell, args: str):
    """Show release notes"""
    from pythinker_code.ui.shell.components.markdown import PythinkerMarkdown

    sections: list[str] = []
    for ver, entry in CHANGELOG.items():
        lines = [f"## {ver}"]
        if entry.description:
            lines.extend(["", entry.description])
        items = [item for item in entry.entries if not item.lower().startswith("lib:")]
        if items:
            lines.append("")
            lines.extend(f"- {item}" for item in items)
        sections.append("\n".join(lines))

    with console.pager(styles=True):
        console.print(PythinkerMarkdown("\n\n".join(sections)))


def _feedback_destination(soul: PythinkerSoul) -> tuple[str, dict[str, str]] | None:
    """Resolve the endpoint and headers for explicit user feedback submissions."""
    import os

    from pythinker_code.auth import PYTHINKER_CODE_PLATFORM_ID
    from pythinker_code.auth.platforms import get_platform_by_id, managed_provider_key

    headers: dict[str, str] = {}
    feedback_config = soul.runtime.config.feedback

    endpoint_url = os.getenv("PYTHINKER_FEEDBACK_URL", "").strip()
    if not endpoint_url:
        endpoint_url = feedback_config.endpoint_url.strip()

    if endpoint_url:
        if feedback_config.custom_headers:
            headers.update(feedback_config.custom_headers)
        api_key = os.getenv("PYTHINKER_FEEDBACK_API_KEY", "").strip()
        if not api_key and feedback_config.api_key is not None:
            api_key = feedback_config.api_key.get_secret_value()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return endpoint_url, headers

    pythinker_platform = get_platform_by_id(PYTHINKER_CODE_PLATFORM_ID)
    if pythinker_platform is None:
        return None

    provider = soul.runtime.config.providers.get(managed_provider_key(PYTHINKER_CODE_PLATFORM_ID))
    if provider is None:
        return None
    if provider.custom_headers:
        headers.update(provider.custom_headers)
    provider_api_key = getattr(provider, "api_key", None)
    if provider_api_key is None:
        return None
    api_key = provider_api_key.get_secret_value()
    if provider.oauth is not None:
        api_key = soul.runtime.oauth.resolve_api_key(provider_api_key, provider.oauth)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if not any(key.lower() == "authorization" and value for key, value in headers.items()):
        return None

    return f"{pythinker_platform.base_url.rstrip('/')}/feedback", headers


@registry.command
@shell_mode_registry.command
async def feedback(app: Shell, args: str):
    """Submit feedback with redacted session context. Usage: /feedback [bug|feature|ux|wrong] [message]"""  # noqa: E501
    import aiohttp

    from pythinker_code.feedback import (
        build_feedback_issue_url,
        build_feedback_payload,
        feedback_summary,
        parse_feedback_args,
        submit_feedback_payload,
    )
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_fb
    from pythinker_code.utils.term import open_url_in_browser

    _t_fb = _get_tok_fb()
    app_soul = getattr(app, "soul", None)
    soul = app_soul if isinstance(app_soul, PythinkerSoul) else None

    def _fallback_to_issue(payload: dict[str, Any] | None = None) -> None:
        issue_url = (
            build_feedback_issue_url(payload, soul.runtime.config.feedback.github_repo)
            if payload is not None and soul is not None
            else "https://github.com/Pythoughts-labs/pythinker-code/issues/new/choose"
        )
        if open_url_in_browser(issue_url):
            console.print(f"[{_t_fb.success}]Opening GitHub feedback in your browser...[/]")
        else:
            console.print(f"Please open: [underline]{issue_url}[/underline]")

    parsed = parse_feedback_args(args)
    if isinstance(parsed, str):
        console.print(f"[{_t_fb.error}]{_rich_escape(parsed)}[/]")
        if soul is None:
            _fallback_to_issue()
        return

    if soul is None:
        _fallback_to_issue()
        return

    if not parsed.message:
        from prompt_toolkit import PromptSession

        prompt_session: PromptSession[str] = PromptSession()
        try:
            message = await prompt_session.prompt_async(
                f"Describe your {parsed.kind} feedback (Ctrl-C to cancel): "
            )
        except (EOFError, KeyboardInterrupt):
            console.print(f"[{_t_fb.muted}]Feedback cancelled.[/]")
            return
        parsed = type(parsed)(
            kind=parsed.kind,
            message=message.strip(),
            include_diff=parsed.include_diff,
            include_transcript=parsed.include_transcript,
            include_tool_details=parsed.include_tool_details,
            yes=parsed.yes,
        )

    if not parsed.message:
        console.print(f"[{_t_fb.warning}]Nothing to submit. Cancelled.[/]")
        return

    with console.status(f"[{_t_fb.info}]Collecting feedback context...[/]"):
        payload = await build_feedback_payload(soul, parsed)
    console.print(feedback_summary(payload))

    if not parsed.yes:
        from prompt_toolkit import PromptSession

        prompt_session = PromptSession[str]()
        try:
            answer = await prompt_session.prompt_async("Send this feedback? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            console.print(f"[{_t_fb.muted}]Feedback cancelled.[/]")
            return
        if answer.strip().lower() not in {"y", "yes"}:
            console.print(f"[{_t_fb.muted}]Feedback cancelled.[/]")
            return

    destination = _feedback_destination(soul)
    if destination is None:
        _fallback_to_issue(payload)
        return
    feedback_url, headers = destination

    with console.status(f"[{_t_fb.info}]Submitting feedback...[/]"):
        try:
            submission = await submit_feedback_payload(feedback_url, headers, payload)
        except TimeoutError:
            console.print(f"[{_t_fb.error}]Feedback submission timed out.[/]")
            _fallback_to_issue(payload)
            return
        except aiohttp.ClientError as exc:
            status = getattr(exc, "status", None)
            msg = (
                f"Feedback submission failed (HTTP {status})."
                if status
                else "Network error, failed to submit feedback."
            )
            console.print(f"[{_t_fb.error}]{msg}[/]")
            _fallback_to_issue(payload)
            return

    from pythinker_code.telemetry import track

    track(
        "feedback_submitted",
        feedback_type=parsed.kind,
        include_diff=parsed.include_diff,
        include_transcript=parsed.include_transcript,
        include_tool_details=parsed.include_tool_details,
    )
    if submission.html_url:
        console.print(f"[{_t_fb.success}]Thanks — feedback submitted: {submission.html_url}[/]")
    elif submission.number is not None:
        console.print(
            f"[{_t_fb.success}]Thanks — feedback report #{submission.number} submitted.[/]"
        )
    else:
        console.print(
            f"[{_t_fb.success}]Thanks — feedback submitted. "
            f"Session ID: {soul.runtime.session.id}[/]"
        )
        issue_url = build_feedback_issue_url(payload, soul.runtime.config.feedback.github_repo)
        console.print(
            "No report link was returned. To add follow-up details, open: "
            f"[underline]{issue_url}[/underline]"
        )


@registry.command(aliases=["report-error", "report"])
@shell_mode_registry.command(aliases=["report-error", "report"])
async def report_error(app: Shell, args: str):
    """Submit a report about an error you hit, with a snapshot of recent failures."""
    import platform

    import aiohttp

    from pythinker_code.constant import VERSION
    from pythinker_code.telemetry.errors import clear_recent_errors, recent_errors
    from pythinker_code.ui.shell.oauth import current_model_key
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_re
    from pythinker_code.utils.aiohttp import new_client_session
    from pythinker_code.utils.term import open_url_in_browser

    _t_re = _get_tok_re()

    ISSUE_URL = "https://github.com/Pythoughts-labs/pythinker-code/issues"

    def _fallback_to_issues() -> None:
        if not open_url_in_browser(ISSUE_URL):
            console.print(f"Please file the report at [underline]{ISSUE_URL}[/underline].")

    soul = ensure_pythinker_soul(app)
    if soul is None:
        _fallback_to_issues()
        return

    destination = _feedback_destination(soul)
    if destination is None:
        _fallback_to_issues()
        return
    feedback_url, headers = destination

    errors = recent_errors()
    if errors:
        console.print("[bold]Recent errors this session (most recent last):[/bold]")
        for i, err in enumerate(errors, start=1):
            tool_part = f" (tool={_rich_escape(err.tool)})" if err.tool else ""
            console.print(
                f"  [dim]{i}.[/dim] [{_t_re.info}]{_rich_escape(err.site)}[/]"
                f"{tool_part}: {_rich_escape(err.exc_class)}"
            )
    else:
        console.print(
            f"[{_t_re.muted}]No errors recorded this session. "
            f"You can still attach a comment describing what went wrong.[/]"
        )

    from prompt_toolkit import PromptSession

    prompt_session: PromptSession[str] = PromptSession()
    try:
        comment = await prompt_session.prompt_async("Describe what went wrong (Ctrl-C to cancel): ")
    except (EOFError, KeyboardInterrupt):
        console.print(f"[{_t_re.muted}]Report cancelled.[/]")
        return

    comment = comment.strip()
    if not comment and not errors:
        console.print(f"[{_t_re.warning}]Nothing to report. Cancelled.[/]")
        return

    payload = {
        "session_id": soul.runtime.session.id,
        "type": "error",
        "content": comment,
        "version": VERSION,
        "os": f"{platform.system()} {platform.release()}",
        "model": current_model_key(soul),
        "recent_errors": [
            {
                "timestamp": err.timestamp,
                "site": err.site,
                "exc_class": err.exc_class,
                "message": err.message,
                "tool": err.tool,
            }
            for err in errors
        ],
    }

    with console.status(f"[{_t_re.info}]Submitting error report...[/]"):
        try:
            async with (
                new_client_session() as session,
                session.post(
                    feedback_url,
                    json=payload,
                    headers=headers,
                    raise_for_status=True,
                ),
            ):
                pass
            from pythinker_code.telemetry import track

            track("error_report_submitted", error_count=len(errors), has_comment=bool(comment))
            clear_recent_errors()
            console.print(
                f"[{_t_re.success}]Error report submitted. Session ID: {soul.runtime.session.id}[/]"
            )
        except TimeoutError:
            console.print(f"[{_t_re.error}]Submission timed out.[/]")
            _fallback_to_issues()
        except aiohttp.ClientError as e:
            status = getattr(e, "status", None)
            msg = (
                f"Failed to submit error report (HTTP {status})."
                if status
                else "Network error, failed to submit error report."
            )
            console.print(f"[{_t_re.error}]{msg}[/]")
            _fallback_to_issues()


@registry.command(aliases=["reset"])
async def clear(app: Shell, args: str):
    """Clear the context"""
    if ensure_pythinker_soul(app) is None:
        return
    from pythinker_code.telemetry import track

    track("clear")
    await app.run_soul_command("/clear")
    raise Reload(clear_screen=True)


@registry.command
async def new(app: Shell, args: str):
    """Start a new session"""
    soul = ensure_pythinker_soul(app)
    if soul is None:
        return
    current_session = soul.runtime.session
    work_dir = current_session.work_dir
    # Clean up the current session if it has no content, so that chaining
    # /new commands (or switching away before the first message) does not
    # leave orphan empty session directories on disk.
    if current_session.is_empty():
        await current_session.delete()
    session = await Session.create(work_dir)
    from pythinker_code.telemetry import track

    track("session_new")
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_new

    console.print(f"[{_get_tok_new().success}]New session created. Switching...[/]")
    raise Reload(session_id=session.id)


@registry.command(name="title", aliases=["rename"])
async def title(app: Shell, args: str):
    """Set or show the session title"""
    soul = ensure_pythinker_soul(app)
    if soul is None:
        return
    session = soul.runtime.session
    if not args.strip():
        console.print(f"Session title: [bold]{_rich_escape(session.title)}[/bold]")
        return

    from pythinker_code.session_state import load_session_state, save_session_state

    new_title = args.strip()[:200]
    # Read-modify-write: load fresh state to avoid overwriting concurrent web changes
    fresh = load_session_state(session.dir)
    fresh.custom_title = new_title
    fresh.title_generated = True
    save_session_state(fresh, session.dir)
    session.state.custom_title = new_title
    session.state.title_generated = True
    session.title = new_title
    from pythinker_code.scratchpad import append_scratch_event_sync, rename_session_scratch
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_title

    rename_session_scratch(session.work_dir, session_id=session.id, session_title=new_title)
    await asyncio.to_thread(
        append_scratch_event_sync,
        session.work_dir,
        session_id=session.id,
        session_title=new_title,
        title="session title set",
        details=[f"title: {new_title}"],
        labels=[f"scope:{new_title}"],
    )
    console.print(f"[{_get_tok_title().success}]Session title set to: {_rich_escape(new_title)}[/]")


@registry.command(name="sessions", aliases=["resume", "session"])
async def list_sessions(app: Shell, args: str):
    """List sessions and resume optionally"""
    import shlex

    from pythinker_code.ui.shell.session_picker import SessionPickerApp

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    current_session = soul.runtime.session
    result = await SessionPickerApp(
        work_dir=current_session.work_dir,
        current_session=current_session,
    ).run()

    if result is None:
        return

    selection, selected_work_dir = result

    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_sess

    _t_sess = _get_tok_sess()
    if selection == current_session.id:
        console.print(f"[{_t_sess.warning}]You are already in this session.[/]")
        return

    if selected_work_dir != current_session.work_dir:
        cmd = f"pythinker --work-dir {shlex.quote(str(selected_work_dir))} --session {selection}"
        console.print(
            f"[{_t_sess.warning}]Session is in a different directory. Run:[/]\n"
            f"  {_rich_escape(cmd)}"
        )
        return

    from pythinker_code.telemetry import track

    track("session_resume")
    console.print(f"[{_t_sess.success}]Switching to session {_rich_escape(selection)}...[/]")
    raise Reload(session_id=selection)


@registry.command(name="task", aliases=["tasks"])
@shell_mode_registry.command(name="task", aliases=["tasks"])
async def task(app: Shell, args: str):
    """Browse and manage background tasks"""
    soul = ensure_pythinker_soul(app)
    if soul is None:
        return
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_task

    _t_task = _get_tok_task()
    if args.strip():
        console.print(f'[{_t_task.warning}]Usage: "/task" opens the interactive task browser.[/]')
        return
    if soul.runtime.role != "root":
        console.print(
            f"[{_t_task.warning}]Background tasks are only available from the root agent.[/]"
        )
        return

    await TaskBrowserApp(soul).run()


async def _theme_code_picker(app: Shell, soul: PythinkerSoul, arg: str) -> None:
    """Shell syntax theme picker (live preview + persist)."""
    from pythinker_code.share import get_share_dir
    from pythinker_code.ui.shell.selectors.code_theme import run_code_theme_selector
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_theme
    from pythinker_code.utils.rich.syntax import (
        code_themes_match_for_picker,
        get_active_code_theme,
        list_picker_code_themes,
        set_active_code_theme,
    )

    _t = _get_tok_theme()
    configured = soul.runtime.config.tui.code_theme
    available = list_picker_code_themes(get_share_dir())

    if arg:
        # Match case-insensitively but resolve to the canonical picker name so
        # custom mixed-case theme stems are reachable regardless of typed case.
        chosen = next((name for name in available if name.casefold() == arg.casefold()), None)
        if chosen is None:
            console.print(
                f"[{_t.error}]Unknown code theme: {_rich_escape(arg)}. "
                f"Use `/theme code` to pick from {len(available)} themes.[/]"
            )
            return
    else:
        saved = get_active_code_theme()

        def _preview(name: str) -> None:
            set_active_code_theme(name)

        chosen = await run_code_theme_selector(
            current_theme=configured,
            available_themes=available,
            on_preview=_preview,
            theme_matches_current=code_themes_match_for_picker,
        )
        if chosen is None:
            set_active_code_theme(saved)
            return
        if chosen == configured or code_themes_match_for_picker(chosen, configured):
            set_active_code_theme(saved)
            return

    config_file = soul.runtime.config.source_file
    if config_file is None:
        set_active_code_theme(chosen)
        console.print(
            f"[{_t.warning}]Cannot persist code theme: no config file. "
            f"Using {_rich_escape(chosen)} for this session only. "
            f"Restart without --config to save settings.[/]"
        )
        return

    try:
        config_for_save = load_config(config_file)
        config_for_save.tui.code_theme = chosen
        save_config(config_for_save, config_file)
    except (ConfigError, OSError) as exc:
        console.print(f"[{_t.error}]Failed to save config: {_rich_escape(exc)}[/]")
        set_active_code_theme(configured)
        return

    set_active_code_theme(chosen)
    console.print(f"[{_t.success}]Switched code theme to {_rich_escape(chosen)}. Reloading...[/]")
    raise Reload(session_id=soul.runtime.session.id)


@registry.command(aliases=["color"])
@shell_mode_registry.command(aliases=["color"])
async def theme(app: Shell, args: str) -> None:
    """Switch or inspect terminal color theme."""
    from pythinker_code.ui.theme import (
        TUI_TOKEN_NAMES,
        get_active_theme,
        theme_doctor_report,
    )
    from pythinker_code.ui.theme import (
        get_tui_tokens as _get_tok_theme,
    )

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    _t_theme = _get_tok_theme()
    configured = soul.runtime.config.theme
    raw_arg = args.strip()
    sub_raw, _, rest = raw_arg.partition(" ")
    # Lowercase only the subcommand for routing; preserve the theme-name case so
    # mixed-case custom code themes stay reachable via `/theme code <Name>`.
    sub = sub_raw.lower()
    arg = raw_arg.lower()

    if sub in ("current", "doctor", "tokens", "code"):
        if sub == "current":
            from pythinker_code.utils.rich.syntax import get_active_code_theme

            console.print(
                f"[{_t_theme.info}]Active UI theme:[/] {get_active_theme()}\n"
                f"[{_t_theme.muted}]Configured UI theme:[/] {configured}\n"
                f"[{_t_theme.info}]Active code theme:[/] {get_active_code_theme()}\n"
                f"[{_t_theme.muted}]Configured code theme:[/] "
                f"{soul.runtime.config.tui.code_theme}"
            )
            return
        if sub == "doctor":
            report = theme_doctor_report(
                configured=configured,
                config_path=str(soul.runtime.config.source_file)
                if soul.runtime.config.source_file
                else None,
            )
            console.print(report)
            return
        if sub == "tokens":
            tokens = _get_tok_theme()
            lines = [
                f"{name}: {getattr(tokens, name) or '(terminal default)'}"
                for name in sorted(TUI_TOKEN_NAMES)
            ]
            console.print("\n".join(lines))
            return
        if sub == "code":
            await _theme_code_picker(app, soul, rest.strip())
            return

    if not arg:
        from pythinker_code.ui.shell.selectors.theme import run_theme_selector

        chosen = await run_theme_selector(
            current_theme=configured,
            available_themes=["dark", "light", "auto"],
        )
        if chosen is None or chosen == configured:
            return
        arg = chosen
    elif rest:
        arg = sub

    if arg not in ("dark", "light", "auto"):
        console.print(
            f"[{_t_theme.error}]Unknown theme: {_rich_escape(arg)}. "
            f"Use 'dark', 'light', 'auto', 'code', 'current', 'doctor', or 'tokens'.[/]"
        )
        return

    if arg == configured:
        console.print(f"[{_t_theme.warning}]Already using {_rich_escape(arg)} theme.[/]")
        return

    config_file = soul.runtime.config.source_file
    if config_file is None:
        console.print(
            f"[{_t_theme.warning}]Theme switching requires a config file; "
            f"restart without --config to persist this setting.[/]"
        )
        return

    try:
        config_for_save = load_config(config_file)
        config_for_save.theme = arg  # type: ignore[assignment]
        save_config(config_for_save, config_file)
    except (ConfigError, OSError) as exc:
        console.print(f"[{_t_theme.error}]Failed to save config: {_rich_escape(exc)}[/]")
        return

    from pythinker_code.telemetry import track

    track("theme_switch", theme=arg)
    if arg == "auto":
        from pythinker_code.ui.terminal_background import reset_probe_cache

        reset_probe_cache()
    console.print(f"[{_t_theme.success}]Switched to {_rich_escape(arg)} theme. Reloading...[/]")
    raise Reload(session_id=soul.runtime.session.id)


@registry.command
@shell_mode_registry.command
async def thinking(app: Shell, args: str) -> None:
    """Switch thinking level — interactive picker"""
    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    from pythinker_code.thinking import (
        clamp_thinking_effort,
        model_uses_native_thinking,
    )
    from pythinker_code.ui.shell.selectors.thinking import ThinkingLevel, run_thinking_selector
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_think

    _t_think = _get_tok_think()

    if soul.runtime.llm is None:
        console.print(f"[{_t_think.error}]LLM is not set.[/]")
        return
    capabilities = soul.runtime.llm.capabilities
    # Model-aware levels (scopes e.g. gpt-5.4/5.5 away from unsupported 'minimal').
    available_efforts = soul.available_thinking_efforts()
    if available_efforts == ("off",):
        if model_uses_native_thinking(capabilities):
            console.print(
                f"[{_t_think.warning}]Current model uses native reasoning; "
                "there is no effort setting to change.[/]"
            )
        else:
            console.print(f"[{_t_think.warning}]Current model does not support thinking.[/]")
        return

    curr_effort = soul.thinking_effort or ("high" if soul.thinking else "off")
    curr_level = clamp_thinking_effort(curr_effort, available_efforts)
    level = await run_thinking_selector(
        current_level=cast(ThinkingLevel, curr_level),
        available_levels=[cast(ThinkingLevel, effort) for effort in available_efforts],
    )
    if level is None:
        return

    new_thinking = level != "off"
    if level == curr_effort:
        console.print(f"[{_t_think.warning}]Thinking setting unchanged.[/]")
        return

    config_file = soul.runtime.config.source_file
    if config_file is None:
        console.print(
            f"[{_t_think.warning}]Thinking requires a config file; "
            f"restart without --config to persist this setting.[/]"
        )
        return

    try:
        config_for_save = load_config(config_file)
        config_for_save.default_thinking = new_thinking
        config_for_save.default_thinking_effort = level
        save_config(config_for_save, config_file)
    except (ConfigError, OSError) as exc:
        console.print(f"[{_t_think.error}]Failed to save config: {_rich_escape(exc)}[/]")
        return

    from pythinker_code.telemetry import track

    track("thinking_toggle", enabled=new_thinking, level=level)
    console.print(f"[{_t_think.success}]Thinking level set to {level}. Reloading...[/]")
    raise Reload(session_id=soul.runtime.session.id)


@registry.command(aliases=["keybindings"])
@shell_mode_registry.command(aliases=["keybindings"])
def keys(app: Shell, args: str):
    """List keyboard shortcuts (semantic keymap)"""
    from rich.console import Group, RenderableType
    from rich.table import Table
    from rich.text import Text

    from pythinker_code.ui.shell.keymap import keybinding_help
    from pythinker_code.ui.theme import get_tui_tokens, tui_rich_style

    _tok = get_tui_tokens()
    bindings = keybinding_help()
    if not bindings:
        console.print(f"[{_tok.warning}]No keybindings registered.[/]")
        return

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Shortcut", style="bold", no_wrap=True)
    table.add_column("Action", style=tui_rich_style("info"))
    table.add_column("Where", style="dim", no_wrap=True)

    for binding in bindings:
        table.add_row("/".join(binding.keys), binding.description, binding.context)

    blocks: list[RenderableType] = [
        Text.from_markup("[bold]Keyboard shortcuts[/bold]"),
        table,
        Text.from_markup(
            f"[{_tok.muted}]Tip: press ? on an empty prompt for the compact overlay.[/]"
        ),
    ]
    console.print(Group(*blocks))


@registry.command
@shell_mode_registry.command
def tui(app: Shell, args: str):
    """Show or set the TUI style: card or pythinker"""
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_tui
    from pythinker_code.ui.tui_config import get_active_tui_style, set_active_tui_style

    _t_tui = _get_tok_tui()

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    current = get_active_tui_style()
    arg = args.strip().lower()

    # Accept "/tui card", "/tui style card", "/tui set card" — all natural shapes.
    parts = arg.split()
    if parts and parts[0] in ("style", "set"):
        parts = parts[1:]
    target = parts[0] if parts else ""

    if not target:
        console.print(f"Current TUI style: [bold]{_rich_escape(current)}[/bold]")
        console.print(f"[{_t_tui.muted}]Usage: /tui card | /tui pythinker[/]")
        return

    if target not in ("card", "pythinker"):
        target_label = _rich_escape(target)
        console.print(
            f"[{_t_tui.error}]Unknown style: {target_label}. Use 'card' or 'pythinker'.[/]"
        )
        return

    if target == current:
        console.print(f"[{_t_tui.warning}]Already using {_rich_escape(target)} style.[/]")
        return

    config_file = soul.runtime.config.source_file
    if config_file is None:
        # Apply in-memory only — useful for one-off testing without a persisted config.
        set_active_tui_style(target)
        console.print(
            f"[{_t_tui.success}]Switched to {target} style for this session "
            "(no config file to persist).[/]"
        )
        return

    try:
        config_for_save = load_config(config_file)
        config_for_save.tui.style = target  # type: ignore[assignment]
        save_config(config_for_save, config_file)
    except (ConfigError, OSError) as exc:
        console.print(f"[{_t_tui.error}]Failed to save config: {_rich_escape(exc)}[/]")
        return

    set_active_tui_style(target)
    if target == "card":
        # Make sure renderers are present immediately for current session.
        from pythinker_code.ui.shell.tool_renderers import register_builtin_renderers

        register_builtin_renderers()

    from pythinker_code.telemetry import track

    track("tui_style_switch", style=target)
    console.print(f"[{_t_tui.success}]Switched to {_rich_escape(target)} style. Reloading...[/]")
    raise Reload(session_id=soul.runtime.session.id)


@registry.command(aliases=["config"])
@shell_mode_registry.command(aliases=["config"])
async def settings(app: Shell, args: str):
    """Open the interactive settings panel; `show` for read-only, `recaps on|off` to toggle"""
    from rich.console import Group, RenderableType
    from rich.table import Table
    from rich.text import Text

    from pythinker_code.ui.theme import get_active_theme, tui_rich_style
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_set
    from pythinker_code.ui.tui_config import get_active_tui_style

    _t_set = _get_tok_set()

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    config = soul.runtime.config
    mode = args.strip().lower()

    def print_settings_table() -> None:
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style=tui_rich_style("info"), no_wrap=True)
        table.add_column()

        table.add_row("Theme", get_active_theme())
        table.add_row("TUI style", get_active_tui_style())
        table.add_row("Default model", config.default_model or "(none)")
        table.add_row("Telemetry", "on" if config.telemetry else "off")
        from pythinker_code.llm import derive_model_capabilities
        from pythinker_code.thinking import (
            effective_config_thinking_effort,
            model_uses_native_thinking,
        )

        default_model_cfg = (
            config.models.get(config.default_model) if config.default_model else None
        )
        default_thinking_label = (
            "native reasoning"
            if default_model_cfg
            and model_uses_native_thinking(derive_model_capabilities(default_model_cfg))
            else effective_config_thinking_effort(
                config.default_thinking, config.default_thinking_effort
            )
        )
        table.add_row("Default thinking", default_thinking_label)
        table.add_row("Show thinking stream", "on" if config.show_thinking_stream else "off")
        table.add_row("Turn recaps", "on" if config.tui.turn_recaps else "off")
        table.add_row("Default yolo", "on" if config.default_yolo else "off")
        table.add_row("Default plan mode", "on" if config.default_plan_mode else "off")
        if config.source_file is not None:
            table.add_row("Config file", str(config.source_file))
        else:
            table.add_row("Config file", "(none — runtime overrides only)")

        blocks: list[RenderableType] = [Text.from_markup("[bold]Settings[/bold]"), table]
        console.print(Group(*blocks))
        console.print(
            f"[{_t_set.muted}]Tip: /settings (or /config) opens the interactive panel; "
            f"/theme, /tui, /model, /keys for related controls.[/]"
        )

    if mode in {"show", "list", "view"}:
        print_settings_table()
        return
    mode_parts = mode.split()
    if mode_parts and mode_parts[0] in {"recap", "recaps"}:
        value = " ".join(mode_parts[1:]).strip()
        if value not in {"on", "off"}:
            console.print(f"[{_t_set.warning}]Usage: /settings recaps on|off[/]")
            return
        enabled = value == "on"
        if config.tui.turn_recaps == enabled:
            console.print(f"[{_t_set.warning}]Turn recaps already {value}.[/]")
            return
        config_file = config.source_file
        if config_file is None:
            console.print(
                f"[{_t_set.warning}]Toggling recaps requires a config file; "
                f"restart without --config (or use --config-file) to persist settings.[/]"
            )
            return
        try:
            config_for_save = load_config(config_file)
            config_for_save.tui.turn_recaps = enabled
            save_config(config_for_save, config_file)
        except (ConfigError, OSError) as exc:
            console.print(f"[{_t_set.error}]Failed to save config: {_rich_escape(exc)}[/]")
            return
        from pythinker_code.telemetry import track

        track("settings_update", changed="tui.turn_recaps", count=1)
        console.print(f"[{_t_set.success}]Turn recaps {value}. Reloading...[/]")
        raise Reload(session_id=soul.runtime.session.id)
    if mode:
        console.print(f"[{_t_set.warning}]Usage: /settings [show|recap(s) on|off][/]")
        return

    config_file = config.source_file
    if config_file is None:
        print_settings_table()
        console.print(
            f"[{_t_set.warning}]Interactive settings require a config file; "
            f"restart without --config text to persist settings.[/]"
        )
        return

    from pythinker_code.ui.shell.selectors.settings import (
        apply_settings_changes,
        run_settings_selector,
    )

    try:
        config_for_save = load_config(config_file)
    except (ConfigError, OSError) as exc:
        console.print(f"[{_t_set.error}]Failed to load config: {_rich_escape(exc)}[/]")
        return

    changes = await run_settings_selector(config_for_save)
    if changes is None:
        return

    changed_ids = apply_settings_changes(config_for_save, changes)
    if not changed_ids:
        console.print(f"[{_t_set.warning}]Settings unchanged.[/]")
        return

    try:
        save_config(config_for_save, config_file)
    except (ConfigError, OSError) as exc:
        console.print(f"[{_t_set.error}]Failed to save config: {_rich_escape(exc)}[/]")
        return

    from pythinker_code.telemetry import track

    track("settings_update", changed=",".join(changed_ids), count=len(changed_ids))
    console.print(f"[{_t_set.success}]Updated {len(changed_ids)} setting(s). Reloading...[/]")
    raise Reload(session_id=soul.runtime.session.id)


@registry.command(available_during_task=True)
@shell_mode_registry.command
async def statusline(app: Shell, args: str) -> None:
    """Customize the status line (footer): segments, on/off, external command"""
    from rich.table import Table

    from pythinker_code.config import STATUSLINE_SEGMENT_IDS
    from pythinker_code.ui.theme import get_tui_tokens

    _t = get_tui_tokens()
    soul = ensure_pythinker_soul(app)
    if soul is None:
        return
    config = soul.runtime.config
    current = config.tui.statusline

    # Pre-escaped: the [show|on|off|...] brackets would otherwise be parsed
    # (and swallowed) as Rich markup when interpolated into styled prints.
    usage_text = _rich_escape(
        "Usage: /statusline [show|on|off|segments <id,...>|style fancy|plain"
        "|bar-width <4-20>|budget <usd|none>|command <argv...>|command none]"
        f" — segment ids: {', '.join(STATUSLINE_SEGMENT_IDS)}"
    )

    def print_table() -> None:
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_row("Enabled", "on" if current.enabled else "off")
        table.add_row("Segments", ", ".join(current.segments) or "(none)")
        table.add_row("Style", current.style)
        table.add_row("Bar width", str(current.bar_width))
        table.add_row(
            "Cost budget",
            f"${current.cost_budget:g}" if current.cost_budget is not None else "(none)",
        )
        table.add_row("Command", _rich_escape(current.command) if current.command else "(none)")
        table.add_row("Command timeout", f"{current.command_timeout_ms} ms")
        console.print(table)
        console.print(f"[{_t.muted}]{usage_text}[/]")

    def persist(mutate: Callable[[StatusLineConfig], None], message: str) -> NoReturn | None:
        config_file = config.source_file
        if config_file is None:
            console.print(
                f"[{_t.warning}]Changing the status line requires a config file; "
                f"restart without --config text to persist settings.[/]"
            )
            return None
        try:
            config_for_save = load_config(config_file)
            mutate(config_for_save.tui.statusline)
            save_config(config_for_save, config_file)
        except (ConfigError, OSError) as exc:
            console.print(f"[{_t.error}]Failed to save config: {_rich_escape(exc)}[/]")
            return None
        from pythinker_code.telemetry import track

        track("settings_update", changed="tui.statusline", count=1)
        console.print(f"[{_t.success}]{message} Reloading...[/]")
        raise Reload(session_id=soul.runtime.session.id)

    async def run_menu() -> None:
        from pythinker_code.ui.shell.components.settings_list import (
            SettingItem,
            SettingsListConfig,
            run_settings_list,
        )

        items = [
            SettingItem(
                id="enabled",
                label="Enabled",
                current_value="on" if current.enabled else "off",
                description="Show the customizable status line below the prompt.",
                values=("on", "off"),
            )
        ]
        for seg in STATUSLINE_SEGMENT_IDS:
            items.append(
                SettingItem(
                    id=f"segment:{seg}",
                    label=f"Segment: {seg}",
                    current_value="on" if seg in current.segments else "off",
                    description=f"Show the {seg} segment.",
                    values=("on", "off"),
                )
            )
        timeout_values = sorted({current.command_timeout_ms, 500, 1000, 2000, 5000})
        items.append(
            SettingItem(
                id="command_timeout_ms",
                label="Command timeout (ms)",
                current_value=str(current.command_timeout_ms),
                description="Timeout for the external status command.",
                values=[str(v) for v in timeout_values],
            )
        )
        items.append(
            SettingItem(
                id="style",
                label="Style",
                current_value=current.style,
                description="Footer visual style: 'fancy' (colors + bar) or 'plain' (monochrome).",
                values=("fancy", "plain"),
            )
        )
        bar_width_values = sorted({current.bar_width, 6, 8, 10, 12, 16})
        items.append(
            SettingItem(
                id="bar_width",
                label="Bar width",
                current_value=str(current.bar_width),
                description="Width in cells of the context progress bar (4-20).",
                values=[str(v) for v in bar_width_values],
            )
        )
        items.append(
            SettingItem(
                id="command",
                label="Command",
                current_value=current.command or "(none)",
                description="External command; change via /statusline command <argv...>.",
            )
        )

        result = await run_settings_list(SettingsListConfig(title="Status line", items=items))
        if result is None:
            return
        changes = result.changes
        if not changes:
            console.print(f"[{_t.warning}]Status line unchanged.[/]")
            return

        def _apply(sl: Any) -> None:
            if "enabled" in changes:
                sl.enabled = changes["enabled"] == "on"
            if "command_timeout_ms" in changes:
                sl.command_timeout_ms = int(changes["command_timeout_ms"])
            if "style" in changes:
                sl.style = changes["style"]
            if "bar_width" in changes:
                sl.bar_width = int(changes["bar_width"])
            segment_changes = {
                key.removeprefix("segment:"): value == "on"
                for key, value in changes.items()
                if key.startswith("segment:")
            }
            if segment_changes:
                wanted = set(sl.segments)
                for seg, on in segment_changes.items():
                    (wanted.add if on else wanted.discard)(seg)
                sl.segments = [s for s in STATUSLINE_SEGMENT_IDS if s in wanted]

        persist(_apply, "Status line updated.")

    mode = args.strip()
    if mode == "":
        # Bare /statusline opens the interactive menu (Esc to dismiss). Fall
        # back to the static table while a turn is streaming — a second
        # prompt_toolkit application cannot run on top of the live view.
        if getattr(app, "_active_view", None) is None:
            await run_menu()
        else:
            print_table()
        return
    if mode in {"show", "list", "view"}:
        print_table()
        return
    if mode in {"on", "off"}:
        enabled = mode == "on"

        def _set_enabled(sl: Any) -> None:
            sl.enabled = enabled

        persist(_set_enabled, f"Status line customization {mode}.")
        return
    # Exact-verb match: "/statusline commands" must not parse as
    # `command` with argument "s" and silently persist a junk command.
    verb, _, verb_args = mode.partition(" ")
    if verb == "segments":
        if not verb_args.strip():
            from rich.table import Table as _Table

            from pythinker_code.ui.shell.statusline import SEGMENT_REGISTRY

            seg_table = _Table(show_header=True, box=None, pad_edge=False)
            seg_table.add_column("id")
            seg_table.add_column("zone")
            seg_table.add_column("state")
            for seg_id, spec in SEGMENT_REGISTRY.items():
                seg_table.add_row(seg_id, spec.zone, "on" if seg_id in current.segments else "off")
            console.print(seg_table)
            console.print(f"[{_t.muted}]{usage_text}[/]")
            return
        raw = verb_args.strip()
        wanted = [s.strip() for s in raw.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in STATUSLINE_SEGMENT_IDS]
        if not wanted or unknown:
            detail = f" Unknown: {', '.join(unknown)}." if unknown else ""
            console.print(f"[{_t.warning}]{usage_text}{_rich_escape(detail)}[/]")
            return

        def _set_segments(sl: Any) -> None:
            sl.segments = wanted

        persist(_set_segments, f"Status line segments set to {', '.join(wanted)}.")
        return
    if verb == "command":
        raw = verb_args.strip()
        if not raw:
            console.print(f"[{_t.warning}]{usage_text}[/]")
            return
        if raw == "none":

            def _clear_command(sl: Any) -> None:
                sl.command = None

            persist(_clear_command, "Status line command cleared.")
            return

        def _set_command(sl: Any) -> None:
            sl.command = raw
            if "command" not in sl.segments:
                sl.segments = [*sl.segments, "command"]

        persist(_set_command, f"Status line command set to {_rich_escape(repr(raw))}.")
        return
    if verb == "style":
        choice = verb_args.strip()
        if choice not in {"fancy", "plain"}:
            console.print(f"[{_t.warning}]{usage_text}[/]")
            return

        def _set_style(sl: Any) -> None:
            sl.style = choice

        persist(_set_style, f"Status line style set to {choice}.")
        return
    if verb == "bar-width":
        try:
            width = int(verb_args.strip())
        except ValueError:
            width = -1
        if not 4 <= width <= 20:
            console.print(f"[{_t.warning}]bar-width must be between 4 and 20.[/]")
            return

        def _set_width(sl: Any) -> None:
            sl.bar_width = width

        persist(_set_width, f"Context bar width set to {width}.")
        return
    if verb == "budget":
        raw_budget = verb_args.strip()
        if raw_budget in {"none", "off", "clear"}:

            def _clear_budget(sl: Any) -> None:
                sl.cost_budget = None

            persist(_clear_budget, "Cost budget cleared.")
            return
        import math

        try:
            budget = float(raw_budget.lstrip("$"))
        except ValueError:
            budget = -1.0
        # float() accepts "nan"/"inf", and nan < 0 is False — guard explicitly.
        if not math.isfinite(budget) or budget < 0:
            console.print(
                f"[{_t.warning}]budget must be a non-negative dollar amount or 'none'.[/]"
            )
            return

        def _set_budget(sl: Any) -> None:
            sl.cost_budget = budget

        persist(_set_budget, f"Cost budget set to ${budget:g}.")
        return
    console.print(f"[{_t.warning}]{usage_text}[/]")


@registry.command(aliases=["rewind-files"])
@shell_mode_registry.command(aliases=["rewind-files"])
def restore(app: Shell, args: str) -> None:
    """List or restore file mutation checkpoints"""
    from rich.table import Table

    from pythinker_code.file_restore import (
        list_file_restore_points,
        restore_file_restore_point,
    )
    from pythinker_code.ui.theme import get_tui_tokens, tui_rich_style

    _tok = get_tui_tokens()
    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    session = soul.runtime.session
    arg = args.strip()
    if not arg:
        points = list_file_restore_points(session, limit=20)
        if not points:
            console.print(f"[{_tok.warning}]No file restore points in this session.[/]")
            return
        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
        table.add_column("ID", style=tui_rich_style("info"), no_wrap=True)
        table.add_column("Tool", no_wrap=True)
        table.add_column("Path")
        table.add_column("State", no_wrap=True)
        for point in points:
            state = "modified" if point.existed else "created"
            table.add_row(point.id, point.tool_name, str(point.path), state)
        console.print(table)
        console.print(f"[{_tok.muted}]Run /restore <id> or /restore latest.[/]")
        return

    if arg == "latest":
        points = list_file_restore_points(session, limit=1)
        if not points:
            console.print(f"[{_tok.warning}]No file restore points in this session.[/]")
            return
        restore_id = points[0].id
    else:
        # Cheap format guard (ids are `<millis>-<8hex>`): rejects traversal
        # attempts like `../../x` without loading every snapshot. Unknown but
        # well-formed ids fall through to the FileNotFoundError handler below.
        if not re.fullmatch(r"[A-Za-z0-9-]+", arg):
            console.print(f"[{_tok.error}]Restore point not found: {_rich_escape(arg)}[/]")
            return
        restore_id = arg

    try:
        point = restore_file_restore_point(session, restore_id)
    except FileNotFoundError:
        console.print(f"[{_tok.error}]Restore point not found: {_rich_escape(restore_id)}[/]")
        return
    except (OSError, ValueError) as exc:
        # ValueError is raised by the file_restore containment check when a restore
        # point's stored path escapes the workspace — surface it cleanly, don't crash.
        console.print(
            f"[{_tok.error}]Failed to restore {_rich_escape(restore_id)}: {_rich_escape(exc)}[/]"
        )
        return

    action = "restored" if point.existed else "removed"
    console.print(
        f"[{_tok.success}]File {action} from restore point {_rich_escape(point.id)}: "
        f"{_rich_escape(point.path)}[/]"
    )


@registry.command
@shell_mode_registry.command
def trust(app: Shell, args: str) -> None:
    """Show or update workspace trust safe mode"""
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_trust

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    _t_trust = _get_tok_trust()
    state = soul.runtime.session.state.trust
    mode = args.strip().lower()
    if mode in {"on", "yes", "trust"}:
        state.trusted = True
        state.safe_mode = False
        soul.runtime.approval.set_safe_mode(False)
        soul.runtime.session.save_state()
        console.print(
            f"[{_t_trust.success}]Workspace trusted. Safe mode disabled for this session.[/]"
        )
        if _persist_project_trust(soul, trusted=True):
            console.print(
                f"[{_t_trust.info}]Project config trust recorded — project hooks load "
                "on /reload or next start.[/]"
            )
        return
    if mode in {"off", "no", "untrust", "safe"}:
        state.trusted = False
        state.safe_mode = True
        soul.runtime.approval.set_safe_mode(True)
        soul.runtime.approval.set_yolo(False)
        soul.runtime.approval.set_auto(False)
        soul.runtime.session.state.approval.auto_approve_actions.clear()
        soul.runtime.session.save_state()
        _persist_project_trust(soul, trusted=False)
        console.print(
            f"[{_t_trust.warning}]Workspace untrusted. Safe mode enabled; "
            "auto-approval is disabled.[/]"
        )
        return
    if mode:
        console.print(f"[{_t_trust.warning}]Usage: /trust [on|off][/]")
        return

    status = "trusted" if state.trusted else "untrusted"
    safe = "on" if state.safe_mode else "off"
    console.print(f"Workspace trust: [bold]{status}[/bold]  safe mode: [bold]{safe}[/bold]")


def _persist_project_trust(soul: PythinkerSoul, *, trusted: bool) -> bool:
    """Record the durable per-project trust decision for the session's repo.

    Returns True when a project root was found and recorded. Sessions outside
    a git project have no project config scope to gate, so nothing persists.
    """
    from pythinker_code.config import find_project_root
    from pythinker_code.project_trust import set_project_trusted

    root = find_project_root(Path(str(soul.runtime.work_dir)))
    if root is None:
        return False
    set_project_trusted(root, trusted)
    return True


@registry.command
@shell_mode_registry.command
async def worklog(app: Shell, args: str) -> None:
    """Show a compact session activity timeline"""
    from rich.table import Table

    from pythinker_code.file_restore import list_file_restore_points

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    counts: dict[str, int] = {}
    recent: list[str] = []
    try:
        async for record in soul.runtime.session.wire_file.iter_records():
            event = type(record.to_wire_message()).__name__
            counts[event] = counts.get(event, 0) + 1
            recent.append(event)
            recent = recent[-12:]
    except FileNotFoundError:
        pass

    from pythinker_code.ui.theme import get_tui_tokens, tui_rich_style

    _tok = get_tui_tokens()

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Signal", style=tui_rich_style("info"))
    table.add_column("Count", justify="right")
    for key in sorted(counts):
        table.add_row(key, str(counts[key]))
    if counts:
        console.print(table)
        console.print(f"[{_tok.muted}]Recent: {_rich_escape(' -> '.join(recent))}[/]")
    else:
        console.print(f"[{_tok.warning}]No worklog events recorded yet.[/]")

    restore_points = list_file_restore_points(soul.runtime.session, limit=5)
    if restore_points:
        console.print("[bold]Recent restore points:[/bold]")
        for point in restore_points:
            console.print(
                f"  [{_tok.info}]{_rich_escape(point.id)}[/] "
                f"{_rich_escape(point.tool_name)} {_rich_escape(point.path)}"
            )


@registry.command(available_during_task=True)
@shell_mode_registry.command
def context(app: Shell, args: str) -> None:
    """Show context, checkpoint, and compaction status"""
    from rich.table import Table

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    from pythinker_code.ui.theme import get_tui_tokens, tui_rich_style

    _tok = get_tui_tokens()

    status = soul.status
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style=tui_rich_style("info"), no_wrap=True)
    table.add_column()
    table.add_row("Tokens", str(status.context_tokens or 0))
    table.add_row("Window", str(status.max_context_tokens or 0))
    context_usage_label = f"{status.context_usage:.1f}%"
    table.add_row("Usage", context_usage_label)
    table.add_row("Checkpoints", str(soul.context.n_checkpoints))
    table.add_row("Plan mode", "on" if soul.plan_mode else "off")
    table.add_row("Context file", str(soul.runtime.session.context_file))
    console.print(table)
    console.print(f"[{_tok.muted}]Use /compact [focus] to summarize old context.[/]")


@registry.command(available_during_task=True)
@shell_mode_registry.command
def tools(app: Shell, args: str) -> None:
    """List available tools and permission posture"""
    from rich.table import Table

    from pythinker_code.soul.permission import permission_profile_for_runtime
    from pythinker_code.ui.theme import tui_rich_style

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    profile = permission_profile_for_runtime(soul.runtime)
    console.print(
        f"Permission profile: [bold]{profile.name}[/bold] "
        f"({profile.description}; file mutations={'yes' if profile.allow_file_mutation else 'no'}, "
        f"shell mutations={'yes' if profile.allow_shell_mutation else 'no'})"
    )

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Tool", style=tui_rich_style("info"), no_wrap=True)
    table.add_column("Description")
    for tool in sorted(soul.agent.toolset.tools, key=lambda item: item.name):
        desc = " ".join((tool.description or "").split())
        if len(desc) > 100:
            desc = desc[:97] + "..."
        table.add_row(tool.name, desc)
    console.print(table)
    if args.strip().lower() == "audit":
        from pythinker_code.ui.theme import get_tui_tokens as _get_tok_tools

        _t_tools = _get_tok_tools()
        console.print(
            f"[{_t_tools.muted}]Audit: external MCP/wire/plugin tools are blocked in read-only, "
            f"plan, review, and verify profiles unless their side effects are known.[/]"
        )


@registry.command(aliases=["a11y"])
@shell_mode_registry.command(aliases=["a11y"])
def accessibility(app: Shell, args: str) -> None:
    """Show or update accessibility/plain-output preferences"""
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_a11y

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    _t_a11y = _get_tok_a11y()

    state = soul.runtime.session.state.accessibility
    mode = args.strip().lower()
    match mode:
        case "plain" | "on":
            state.plain_output = True
        case "rich" | "off":
            state.plain_output = False
        case "no-animation" | "no-animations":
            state.animations = False
        case "animation" | "animations":
            state.animations = True
        case "ascii":
            state.symbols = "ascii"
        case "unicode":
            state.symbols = "unicode"
        case "":
            pass
        case _:
            console.print(
                f"[{_t_a11y.warning}]Usage: /accessibility "
                "[plain|rich|no-animation|animation|ascii|unicode][/]"
            )
            return
    if mode:
        soul.runtime.session.save_state()
    console.print(
        "Accessibility: "
        f"plain_output=[bold]{'on' if state.plain_output else 'off'}[/bold] "
        f"animations=[bold]{'on' if state.animations else 'off'}[/bold] "
        f"symbols=[bold]{state.symbols}[/bold]"
    )


@registry.command
def web(app: Shell, args: str):
    """Open Pythinker Web UI in browser"""
    from pythinker_code.telemetry import track

    track("web_opened")
    soul = ensure_pythinker_soul(app)
    session_id = soul.runtime.session.id if soul else None
    raise SwitchToWeb(session_id=session_id)


@registry.command(aliases=["dashboard"])
def reports(app: Shell, _args: str) -> NoReturn:
    """Open Pythinker session reports (Agent Tracing Visualizer) in browser"""
    from pythinker_code.telemetry import track

    track("reports_opened")
    soul = ensure_pythinker_soul(app)
    session_id = soul.runtime.session.id if soul else None
    raise SwitchToDashboard(session_id=session_id)


@registry.command(name="memory", aliases=["mem"])
async def show_memory(app: Shell, args: str):
    """Show project memory, or manage the approval-gated memory inbox."""
    soul = ensure_pythinker_soul(app)
    if soul is None:
        return
    from pythinker_code.project_memory import ProjectMemoryStore, Target

    store = ProjectMemoryStore(soul.runtime.work_dir)
    parts = args.split()
    if parts and parts[0] == "inbox":
        memory_config = getattr(soul.runtime.config, "memory", None)
        if not getattr(memory_config, "consolidation", False):
            console.print(
                "Memory inbox consolidation is disabled. "
                "Set `memory.consolidation = true` in your config to enable it."
            )
            return
        from pythinker_code.memory.consolidation import (
            approve_inbox_candidate,
            generate_inbox_candidates,
            list_inbox_candidates,
            reject_inbox_candidate,
        )

        action = parts[1] if len(parts) > 1 else "list"
        if action == "scan":
            created = await generate_inbox_candidates(store, soul.runtime.work_dir)
            console.print(f"Staged {len(created)} memory inbox candidate(s).")
            return
        if action == "approve" and len(parts) > 2:
            console.print(await approve_inbox_candidate(store, parts[2]))
            if soul.runtime.rearm_injection is not None:
                soul.runtime.rearm_injection("project_memory")
            return
        if action == "reject" and len(parts) > 2:
            console.print(await reject_inbox_candidate(store, parts[2]))
            return
        candidates = await list_inbox_candidates(store)
        if not candidates:
            console.print("Memory inbox is empty. Run `/memory inbox scan` to stage candidates.")
            return
        for candidate in candidates:
            console.print(f"- {candidate.id}: {candidate.title} ({candidate.source_path})")
        return

    block = await store.snapshot()
    if not block.strip():
        console.print("No project memory recorded yet. The agent records it with the Memory tool.")
        return
    console.print(block)

    # Capacity line + education: surface how full each store is so the user
    # understands the "memory full" rejection and how to act on it.
    near_full = False
    cap_parts: list[str] = []
    targets: tuple[tuple[Target, str], ...] = (("memory", "Project"), ("user", "User"))
    for target, label in targets:
        used, limit, free = await store.capacity(target)
        cap_parts.append(f"{label} {used}/{limit} ({free} free)")
        if limit and used / limit >= 0.85:
            near_full = True
    console.print(f"\n[dim]Capacity — {' · '.join(cap_parts)}[/dim]")
    if near_full:
        console.print(
            "[yellow]Memory is nearly full.[/yellow] When full, new facts are rejected "
            "(nothing is lost). To make room: ask the agent to merge or drop stale entries, "
            "or edit MEMORY.md / USER.md directly. Memory holds only durable facts, so "
            "occasional pruning is expected."
        )


@registry.command(name="update", aliases=["upgrade"])
async def update_command(app: Shell, args: str):
    """Check for updates, or `auto [on|off]` to toggle silent startup auto-updates."""
    from pythinker_code.ui.shell.update import UpdateIntent, UpdateResult, run_update_prompt
    from pythinker_code.ui.shell.update_orchestrator import run_update_job

    parts = args.strip().split()
    if parts and parts[0].lower() in {"auto", "auto-update", "autoupdate"}:
        await _auto_update_toggle(app, parts[1:])
        return

    # Bare `/update` opens a top-level menu so the auto-update toggle is
    # discoverable without knowing the `auto` subcommand. Explicit args skip
    # straight to the check/update flow.
    if not parts:
        action = await _prompt_update_action(app)
        if action == "auto":
            await _auto_update_toggle(app, [])
            return
        if action != "check":
            return

    async def _runner(*, print_output: bool, intent: UpdateIntent) -> UpdateResult:
        return await run_update_job(print_output=print_output, intent=intent, source="slash")

    result = await run_update_prompt(update_runner=_runner)
    if result is UpdateResult.UPDATED:
        # A stage created via /update must honor the apply_on_exit policy just
        # like the silent startup stage does.
        app._maybe_arm_windows_apply_on_exit()  # pyright: ignore[reportPrivateUsage]
        console.print("Updated — restart Pythinker to use the new version.")


async def _prompt_update_action(app: Shell) -> str | None:
    """Top-level `/update` menu.

    Returns ``"check"`` to run the update check/install flow, ``"auto"`` to open
    the auto-update toggle, or ``None`` when the user cancels/aborts. The cursor
    defaults to "check" so a bare ``/update`` + Enter still goes straight to the
    update check.
    """
    from prompt_toolkit.shortcuts.choice_input import ChoiceInput

    from pythinker_code.update_policy import resolve_auto_update_mode

    auto_label = "Auto-update on startup"
    if isinstance(app.soul, PythinkerSoul):
        mode = resolve_auto_update_mode(app.soul.runtime.config)
        auto_label = f"{auto_label}: {mode.value}"

    try:
        selection = await ChoiceInput(
            message="Update",
            options=[
                ("check", "Check for updates now"),
                ("auto", auto_label),
                ("cancel", "Cancel"),
            ],
            default="check",
        ).prompt_async()
    except (EOFError, KeyboardInterrupt):
        return None
    return selection if selection in {"check", "auto"} else None


async def _auto_update_toggle(app: Shell, args: list[str]) -> None:
    """Show or set the startup auto-update policy.

    `/update auto <mode>` sets it directly (modes: off, notify, download,
    apply_on_exit). Legacy `on` maps to download; `off` selects the fully-off
    policy — use `notify` for the old "no install, keep notices" behavior.
    `/update auto`
    with no value opens an interactive picker (or, when an external override
    has made the setting read-only, reports the effective state instead of
    popping a no-op picker).
    """
    from pythinker_code.config import coerce_auto_update_mode
    from pythinker_code.telemetry import track
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok
    from pythinker_code.update_policy import auto_update_override_reason, resolve_auto_update_mode

    _t = _get_tok()
    soul = ensure_pythinker_soul(app)
    if soul is None:
        return
    config = soul.runtime.config
    override = auto_update_override_reason()

    def _print_override() -> None:
        if override is not None:
            console.print(f"[{_t.muted}]Note: {override}; this overrides the setting.[/]")

    if args:
        raw = args[0].lower()
        usage = f"[{_t.warning}]Usage: /update auto [on|off|notify|download|apply_on_exit][/]"
        if len(args) > 1:
            console.print(usage)
            return
        if raw == "on":
            # `on` keeps its historical meaning: background auto-update, which
            # now downloads and stages instead of installing mid-session.
            selected_mode = AutoUpdateMode.DOWNLOAD
        elif raw == "off":
            # `off` now means the fully-off policy; the old "no silent install
            # but keep notices" behavior is the explicit `notify` mode.
            selected_mode = AutoUpdateMode.OFF
        else:
            coerced = coerce_auto_update_mode(raw)
            try:
                selected_mode = (
                    coerced if isinstance(coerced, AutoUpdateMode) else AutoUpdateMode(coerced)
                )
            except ValueError:
                console.print(usage)
                return
    elif override is not None:
        # An override makes the stored setting read-only: changing it would not
        # change behavior, so report the effective state instead of a no-op picker.
        effective = resolve_auto_update_mode(config).value
        stored = config.auto_update.value
        console.print(f"[{_t.info}]Auto-update: {effective}[/] (config auto_update={stored})")
        _print_override()
        return
    else:
        picked = await _prompt_auto_update_selection(current=config.auto_update)
        if picked is None:
            return
        selected_mode = picked

    if config.auto_update == selected_mode:
        console.print(f"[{_t.warning}]Auto-update already {selected_mode.value}.[/]")
        _print_override()
        return

    config_file = config.source_file
    if config_file is None:
        console.print(
            f"[{_t.warning}]Toggling auto-update requires a config file; "
            f"restart without --config (or use --config-file) to persist settings.[/]"
        )
        return
    try:
        config_for_save = load_config(config_file)
        config_for_save.auto_update = selected_mode
        save_config(config_for_save, config_file)
    except (ConfigError, OSError) as exc:
        console.print(f"[{_t.error}]Failed to save config: {_rich_escape(exc)}[/]")
        return
    # auto_update is only consulted at startup, so nothing live depends on it:
    # mirror the saved value into the running config instead of forcing a reload
    # (a reload would re-trigger the startup auto-update task we just toggled).
    config.auto_update = selected_mode

    track("settings_update", changed="auto_update", count=1)
    console.print(
        f"[{_t.success}]Auto-update {selected_mode.value}. Takes effect at next startup.[/]"
    )
    _print_override()


async def _prompt_auto_update_selection(*, current: AutoUpdateMode) -> AutoUpdateMode | None:
    """Interactive mode picker for startup auto-update.

    Returns the chosen mode, or ``None`` when the user cancels (selects Cancel,
    or aborts with Esc/Ctrl-C). The cursor defaults to the current setting so
    leaving it unchanged is the zero-effort choice.
    """
    from prompt_toolkit.shortcuts.choice_input import ChoiceInput

    options = [
        (AutoUpdateMode.DOWNLOAD.value, "Download in background, apply on restart"),
        (AutoUpdateMode.NOTIFY.value, "Notify only"),
        (AutoUpdateMode.APPLY_ON_EXIT.value, "Download and apply when Pythinker exits"),
        (AutoUpdateMode.OFF.value, "Off"),
        ("cancel", "Cancel"),
    ]
    default = current.value
    try:
        selection = await ChoiceInput(
            message="Auto-update on startup",
            options=options,
            default=default,
        ).prompt_async()
    except (EOFError, KeyboardInterrupt):
        return None
    if selection in {mode.value for mode in AutoUpdateMode}:
        return AutoUpdateMode(selection)
    return None


@registry.command
async def mcp(app: Shell, args: str):
    """Show MCP servers and tools, or manage one server (reconnect/disconnect/refresh)."""
    from rich.live import Live

    from pythinker_code.exception import MCPRuntimeError
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_mcp

    parts = args.strip().split()
    if parts:
        verb = parts[0].lower()
        if verb in {"reconnect", "disconnect", "refresh", "retry"}:
            soul = ensure_pythinker_soul(app)
            if soul is None:
                return
            server_name = parts[1] if len(parts) > 1 else None
            # Reject extra operands instead of silently using only the first.
            if verb in {"reconnect", "disconnect", "retry"} and len(parts) != 2:
                console.print(f"[{_get_tok_mcp().warning}]Usage: /mcp {verb} <server>[/]")
                return
            if verb == "refresh" and len(parts) > 2:
                console.print(f"[{_get_tok_mcp().warning}]Usage: /mcp refresh [server][/]")
                return
            if verb == "retry":
                verb = "reconnect"
            try:
                if verb == "disconnect":
                    if not server_name:
                        console.print(
                            f"[{_get_tok_mcp().warning}]Usage: /mcp disconnect <server>[/]"
                        )
                        return
                    await soul.disconnect_mcp_server(server_name)
                    console.print(
                        f"[{_get_tok_mcp().success}]Disconnected MCP server "
                        f"{_rich_escape(server_name)}.[/]"
                    )
                    return
                if verb == "reconnect":
                    if not server_name:
                        console.print(
                            f"[{_get_tok_mcp().warning}]Usage: /mcp reconnect <server>[/]"
                        )
                        return
                    await soul.reconnect_mcp_server(server_name)
                    console.print(
                        f"[{_get_tok_mcp().success}]Reconnected MCP server "
                        f"{_rich_escape(server_name)}.[/]"
                    )
                    return
                # refresh [server]
                targets = await soul.refresh_mcp_inventory(server_name)
                if not targets:
                    console.print(f"[{_get_tok_mcp().warning}]No connected MCP servers.[/]")
                    return
                console.print(
                    f"[{_get_tok_mcp().success}]Refreshed MCP inventory for "
                    f"{len(targets)} server(s).[/]"
                )
            except MCPRuntimeError as exc:
                console.print(f"[{_get_tok_mcp().warning}]{exc}[/]")
            return

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return
    await soul.start_background_mcp_loading()
    snapshot = soul.status.mcp_status
    if snapshot is None:
        from pythinker_code.ui.theme import get_tui_tokens as _get_tok_mcp

        console.print(f"[{_get_tok_mcp().warning}]No MCP servers configured.[/]")
        return

    if not snapshot.loading:
        console.print(render_mcp_console(snapshot))
        return

    final_snapshot: MCPStatusSnapshot | None = snapshot
    with Live(
        render_mcp_console(snapshot),
        console=console,
        refresh_per_second=8,
        transient=True,
    ) as live:
        while True:
            snapshot = soul.status.mcp_status
            if snapshot is None:
                final_snapshot = None
                break
            final_snapshot = snapshot
            live.update(render_mcp_console(snapshot), refresh=True)
            if not snapshot.loading:
                break
            await asyncio.sleep(0.125)
        try:
            await soul.wait_for_background_mcp_loading()
        except Exception as e:
            logger.debug("MCP loading completed with error while rendering /mcp: {error}", error=e)
        snapshot = soul.status.mcp_status
        if snapshot is not None:
            final_snapshot = snapshot
            live.update(render_mcp_console(snapshot), refresh=True)

    if final_snapshot is not None:
        console.print(render_mcp_console(final_snapshot))


@registry.command
@shell_mode_registry.command
def hooks(app: Shell, args: str):
    """List configured hooks"""
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_hooks

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    _t_hooks = _get_tok_hooks()
    engine = soul.hook_engine
    if not engine.summary:
        console.print(
            f"[{_t_hooks.warning}]No hooks configured. "
            f"Add [[hooks]] sections to your config.toml to set up hooks.[/]"
        )
        return

    console.print()
    console.print("[bold]Configured Hooks:[/bold]")
    console.print()

    for event, entries in engine.details().items():
        console.print(f"  [{_t_hooks.info}]{_rich_escape(event)}[/]: {len(entries)} hook(s)")
        for entry in entries:
            source_tag = (
                f" [dim]({_rich_escape(entry['source'])})[/dim]"
                if entry["source"] == "wire"
                else ""
            )
            console.print(
                f"    [dim]{_rich_escape(entry['matcher'])}[/dim] "
                f"{_rich_escape(entry['command'])}{source_tag}"
            )

    console.print()


@registry.command
async def undo(app: Shell, args: str):
    """Undo: fork the session at a previous turn and retry"""
    from pythinker_code.session_fork import enumerate_turns, fork_session
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_undo
    from pythinker_code.utils.string import shorten

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    _t_undo = _get_tok_undo()
    session = soul.runtime.session
    wire_path = session.dir / "wire.jsonl"
    turns = enumerate_turns(wire_path)

    if not turns:
        console.print(f"[{_t_undo.warning}]No turns found in this session.[/]")
        return

    # Build choices: each turn's first line, truncated
    choices: list[tuple[str, str]] = []
    for turn in turns:
        first_line = turn.user_text.split("\n", 1)[0]
        label = shorten(first_line, width=80, placeholder="...")
        choices.append((str(turn.index), f"[{turn.index}] {label}"))

    try:
        selected = await ChoiceInput(
            message="Select a turn to undo (↑↓ navigate, Enter select, Ctrl+C cancel):",
            options=choices,
            default=choices[-1][0],
        ).prompt_async()
    except (EOFError, KeyboardInterrupt):
        return

    turn_index = int(selected)

    # The selected turn is the one we want to redo — fork includes turns *before* it
    selected_turn = turns[turn_index]
    user_text = selected_turn.user_text

    if turn_index == 0:
        # Fork with no history — just the user text
        new_session = await Session.create(session.work_dir)
        new_session_id = new_session.id
        # Set title to match the convention used by fork_session
        from pythinker_code.session_state import load_session_state, save_session_state

        new_state = load_session_state(new_session.dir)
        new_state.custom_title = f"Undo: {session.title}"
        new_state.title_generated = True
        save_session_state(new_state, new_session.dir)
    else:
        # Fork includes turns 0..turn_index-1
        fork_turn_index = turn_index - 1
        new_session_id = await fork_session(
            source_session_dir=session.dir,
            work_dir=session.work_dir,
            turn_index=fork_turn_index,
            title_prefix="Undo",
            source_title=session.title,
        )

    from pythinker_code.telemetry import track

    track("undo")
    console.print(f"[{_t_undo.success}]Forked at turn {turn_index}. Switching to new session...[/]")
    raise Reload(session_id=new_session_id, prefill_text=user_text)


@registry.command
async def fork(app: Shell, args: str):
    """Fork the current session (copy all history to a new session)"""
    from pythinker_code.session_fork import fork_session

    soul = ensure_pythinker_soul(app)
    if soul is None:
        return

    session = soul.runtime.session
    new_session_id = await fork_session(
        source_session_dir=session.dir,
        work_dir=session.work_dir,
        turn_index=None,
        title_prefix="Fork",
        source_title=session.title,
    )

    from pythinker_code.telemetry import track

    track("session_fork")
    from pythinker_code.ui.theme import get_tui_tokens as _get_tok_fork

    console.print(f"[{_get_tok_fork().success}]Session forked. Switching to new session...[/]")
    raise Reload(session_id=new_session_id)


from . import (  # noqa: E402
    debug,  # noqa: F401 # type: ignore[reportUnusedImport]
    export_import,  # noqa: F401 # type: ignore[reportUnusedImport]
    oauth,  # noqa: F401 # type: ignore[reportUnusedImport]
    setup,  # noqa: F401 # type: ignore[reportUnusedImport]
    stats,  # noqa: F401 # type: ignore[reportUnusedImport]
    update,  # noqa: F401 # type: ignore[reportUnusedImport]
    usage,  # noqa: F401 # type: ignore[reportUnusedImport]
)
