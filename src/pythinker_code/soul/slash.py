from __future__ import annotations

import re
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pythinker_core.message import Message
from pythinker_host.path import HostPath

import pythinker_code.prompts as prompts
from pythinker_code.config import ConfigError, load_config, save_config
from pythinker_code.soul import wire_send
from pythinker_code.soul.agent import load_agents_md
from pythinker_code.soul.context import Context
from pythinker_code.soul.dynamic_injections.auto_mode import AUTO_DISABLED_REMINDER
from pythinker_code.soul.message import system, system_reminder
from pythinker_code.soul.request_assembly import opaque_manifest_identifier
from pythinker_code.utils.logging import logger
from pythinker_code.utils.path import sanitize_cli_path, shorten_home
from pythinker_code.utils.slashcmd import SlashCommandRegistry
from pythinker_code.wire.types import StatusUpdate, TextPart

if TYPE_CHECKING:
    from pythinker_code.soul.pythinkersoul import PythinkerSoul

type SoulSlashCmdFunc = Callable[[PythinkerSoul, str], None | Awaitable[None]]
"""
A function that runs as a PythinkerSoul-level slash command.

Raises:
    Any exception that can be raised by `Soul.run`.
"""

registry = SlashCommandRegistry[SoulSlashCmdFunc]()
_MANIFEST_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
_MANIFEST_SECRET_IDENTIFIER = re.compile(
    r"(?:sk-(?:ant|proj|[A-Za-z0-9])[A-Za-z0-9_-]{16,}|"
    r"xox(?:a|b|p|r|s)-[A-Za-z0-9-]{10,}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})"
)
_MAX_MANIFEST_IDENTIFIER_LENGTH = 64


def _safe_manifest_reason(identifier: str) -> str:
    if (
        0 < len(identifier) <= _MAX_MANIFEST_IDENTIFIER_LENGTH
        and _MANIFEST_IDENTIFIER.fullmatch(identifier) is not None
        and _MANIFEST_SECRET_IDENTIFIER.search(identifier) is None
    ):
        return identifier
    return "<redacted>"


def _render_prompt_manifest(soul: PythinkerSoul) -> str:
    manifest = soul.latest_request_manifest
    if manifest is None:
        return "No request has been assembled in this session."

    overall_reason = (
        f" reason={_safe_manifest_reason(manifest.reason_code)}"
        if manifest.reason_code is not None
        else ""
    )
    lines = [
        f"Prompt manifest: {manifest.status.value.upper()}{overall_reason}",
        (
            "Budget: "
            f"limit={manifest.budget_tokens} "
            f"budgeted_admitted={manifest.budgeted_admitted_tokens} "
            f"non_budgeted_estimated={manifest.non_budgeted_estimated_tokens}"
        ),
        "Fragments:",
    ]
    for outcome in manifest.outcomes:
        reason = (
            f" reason={_safe_manifest_reason(outcome.reason_code)}"
            if outcome.reason_code is not None
            else ""
        )
        lines.append(
            f"- {opaque_manifest_identifier(outcome.key)} "
            f"[{opaque_manifest_identifier(outcome.source)}]: "
            f"{outcome.requirement.value} {outcome.persistence.value} {outcome.status.value} "
            f"estimated={outcome.estimated_tokens} admitted={outcome.admitted_tokens}{reason}"
        )
    if not manifest.outcomes:
        lines.append("- none")
    return "\n".join(lines)


@registry.command
async def init(soul: PythinkerSoul, args: str) -> None:
    """Analyze the codebase and generate an `AGENTS.md` file"""
    from pythinker_code.soul.pythinkersoul import PythinkerSoul

    with tempfile.TemporaryDirectory() as temp_dir:
        tmp_context = Context(file_backend=Path(temp_dir) / "context.jsonl")
        saved_rearm = soul.runtime.rearm_injection
        tmp_soul = PythinkerSoul(soul.agent, context=tmp_context)
        try:
            await tmp_soul.run(prompts.INIT)
        finally:
            # tmp_soul.__init__ rebound the SHARED agent.toolset plan-mode tools and
            # runtime.rearm_injection to itself; re-point them back at the live soul.
            soul.runtime.rearm_injection = saved_rearm
            soul._bind_plan_mode_tools()  # pyright: ignore[reportPrivateUsage]

    agents_md = await load_agents_md(soul.runtime.builtin_args.PYTHINKER_WORK_DIR)
    system_message = system(
        "The user just ran `/init` slash command. "
        "The system has analyzed the codebase and generated an `AGENTS.md` file. "
        f"Latest AGENTS.md file content:\n{agents_md}"
    )
    await soul.context.append_message(Message(role="user", content=[system_message]))
    from pythinker_code.telemetry import track

    track("init_complete")


@registry.command
async def recap(soul: PythinkerSoul, args: str) -> None:
    """Recap Pythinker sessions. Usage: /recap [on|off|today|yesterday|week|YYYY-MM-DD]"""
    from pythinker_code.session_recap import build_pythinker_recap

    mode = args.strip().lower()
    if mode in {"on", "off"}:
        enabled = mode == "on"
        if soul.runtime.config.tui.turn_recaps == enabled:
            wire_send(TextPart(text=f"Turn recaps already {mode}."))
            return
        previous = soul.runtime.config.tui.turn_recaps
        soul.runtime.config.tui.turn_recaps = enabled
        config_file = soul.runtime.config.source_file
        if config_file is None:
            wire_send(
                TextPart(
                    text=(
                        f"Turn recaps {mode} for the current session only "
                        "(no config file available to persist)."
                    )
                )
            )
            return
        try:
            config_for_save = load_config(config_file)
            config_for_save.tui.turn_recaps = enabled
            save_config(config_for_save, config_file)
        except (ConfigError, OSError) as exc:
            # Persistence failed: revert the in-memory toggle so runtime state
            # matches the reported failure instead of silently diverging.
            soul.runtime.config.tui.turn_recaps = previous
            wire_send(TextPart(text=f"Failed to save recap setting: {exc}"))
            return
        wire_send(TextPart(text=f"Turn recaps {mode}."))
        return

    try:
        text = await build_pythinker_recap(soul.runtime.work_dir, args)
    except ValueError as exc:
        wire_send(TextPart(text=str(exc)))
        return
    wire_send(TextPart(text=text))


@registry.command(name="prompt-manifest")
def prompt_manifest(soul: PythinkerSoul, args: str) -> None:
    """Show the latest sanitized request assembly manifest"""
    del args
    wire_send(TextPart(text=_render_prompt_manifest(soul)))


@registry.command
async def compact(soul: PythinkerSoul, args: str):
    """Compact the context (optionally with a custom focus, e.g. /compact keep db discussions)"""
    if soul.context.n_checkpoints == 0:
        wire_send(TextPart(text="The context is empty."))
        return

    logger.info("Running `/compact`")
    await soul.compact_context(custom_instruction=args.strip())
    snap = soul.status
    wire_send(
        StatusUpdate(
            context_usage=snap.context_usage,
            context_tokens=snap.context_tokens,
            max_context_tokens=snap.max_context_tokens,
        )
    )


@registry.command(aliases=["reset"])
async def clear(soul: PythinkerSoul, args: str):
    """Clear the context"""
    logger.info("Running `/clear`")
    await soul.context.clear()
    await soul.context.write_system_prompt(soul.agent.system_prompt)
    await soul.notify_history_rebuilt()
    wire_send(TextPart(text="The context has been cleared."))
    snap = soul.status
    wire_send(
        StatusUpdate(
            context_usage=snap.context_usage,
            context_tokens=snap.context_tokens,
            max_context_tokens=snap.max_context_tokens,
        )
    )


@registry.command
async def yolo(soul: PythinkerSoul, args: str):
    """Toggle YOLO mode (auto-approve all actions)"""
    from pythinker_code.telemetry import track

    # Inspect only the yolo flag: auto mode is independent and is toggled by /auto.
    if soul.runtime.approval.is_yolo_flag():
        soul.runtime.approval.set_yolo(False)
        track("yolo_toggle", enabled=False)
        if soul.runtime.approval.is_auto():
            # Yolo off but auto mode still on -> tool calls remain auto-approved.
            # Don't mislead the user into thinking approvals just came back.
            wire_send(
                TextPart(
                    text=(
                        "Yolo disabled, but auto mode is still on — tool calls remain "
                        "auto-approved. Use /auto to turn off auto mode."
                    )
                )
            )
        else:
            wire_send(TextPart(text="You only die once! Actions will require approval."))
    else:
        soul.runtime.approval.set_yolo(True)
        track("yolo_toggle", enabled=True)
        wire_send(TextPart(text="You only live once! All actions will be auto-approved."))


@registry.command
async def auto(soul: PythinkerSoul, args: str):
    """Toggle auto mode (no user present: auto-dismiss AskUserQuestion, auto-approve tool calls)"""
    from pythinker_code.telemetry import track

    if soul.runtime.approval.is_auto():
        soul.runtime.approval.set_auto(False)
        await soul.notify_auto_changed(False)
        await soul.context.append_message(
            Message(role="user", content=[system_reminder(AUTO_DISABLED_REMINDER)])
        )
        track("auto_toggle", enabled=False)
        if soul.runtime.approval.is_yolo_flag():
            wire_send(
                TextPart(
                    text=("Auto mode disabled. You are back at the terminal. Yolo is still on.")
                )
            )
        else:
            wire_send(TextPart(text="Auto mode disabled. You are back at the terminal."))
    else:
        soul.runtime.approval.set_auto(True)
        await soul.notify_auto_changed(True)
        track("auto_toggle", enabled=True)
        wire_send(
            TextPart(
                text=(
                    "Auto mode enabled. AskUserQuestion will be auto-dismissed "
                    "and tool calls auto-approved."
                )
            )
        )


@registry.command(name="accept-edits")
async def accept_edits(soul: PythinkerSoul, args: str) -> None:
    """Toggle accept-edits mode (auto-approve reversible in-workspace file edits)"""
    approval = soul.runtime.approval
    if approval.is_accept_edits():
        approval.set_accept_edits(False)
        wire_send(TextPart(text="Accept-edits disabled. File edits will require approval."))
    else:
        approval.set_accept_edits(True)
        if approval.is_safe_mode():
            # Safe mode suppresses every auto-approval path, so be truthful: edits will
            # still prompt until safe mode is turned off.
            message = (
                "Accept-edits enabled, but safe mode is on — edits will still prompt until "
                "you turn safe mode off."
            )
        else:
            message = (
                "Accept-edits enabled. Reversible in-workspace file edits are "
                "auto-approved; shell, destructive, outside-workspace, and sensitive "
                "host edits still prompt."
            )
        wire_send(TextPart(text=message))


@registry.command
async def plan(soul: PythinkerSoul, args: str):
    """Toggle plan mode. Usage: /plan [on|off|view|clear]"""
    subcmd = args.strip().lower()

    if subcmd == "on":
        if not soul.plan_mode:
            await soul.toggle_plan_mode_from_manual()
        plan_path = soul.get_plan_file_path()
        wire_send(TextPart(text=f"Plan mode ON. Plan file: {plan_path}"))
        wire_send(StatusUpdate(plan_mode=soul.plan_mode))
    elif subcmd == "off":
        if soul.plan_mode:
            await soul.toggle_plan_mode_from_manual()
        wire_send(TextPart(text="Plan mode OFF. All tools are now available."))
        wire_send(StatusUpdate(plan_mode=soul.plan_mode))
    elif subcmd == "view":
        content = soul.read_current_plan()
        if content:
            wire_send(TextPart(text=content))
        else:
            wire_send(TextPart(text="No plan file found for this session."))
    elif subcmd == "clear":
        soul.clear_current_plan()
        wire_send(TextPart(text="Plan cleared."))
    else:
        # Default: toggle
        new_state = await soul.toggle_plan_mode_from_manual()
        if new_state:
            plan_path = soul.get_plan_file_path()
            wire_send(
                TextPart(
                    text=f"Plan mode ON. Write your plan to: {plan_path}\n"
                    "Use ExitPlanMode when done, or /plan off to exit manually."
                )
            )
        else:
            wire_send(TextPart(text="Plan mode OFF. All tools are now available."))
        wire_send(StatusUpdate(plan_mode=soul.plan_mode))


_GOAL_USAGE = "Usage: /goal <objective> | /goal view | /goal pause | /goal resume | /goal clear"


@registry.command
async def goal(soul: PythinkerSoul, args: str):
    """Set a thread goal pursued across turns until verified. Usage: /goal <objective> | view | pause | resume | clear"""  # noqa: E501
    from pythinker_code.session_state import GoalState

    text = args.strip()
    state = soul.runtime.session.state
    subcmd = text.lower()

    if subcmd in ("", "view"):
        if state.goal is not None:
            wire_send(
                TextPart(
                    text=f"Goal ({state.goal.status}):\n{state.goal.objective}\n\n"
                    "Use /goal clear to remove it, /goal pause|resume to toggle it, "
                    "or /goal <new objective> to replace it."
                )
            )
        else:
            wire_send(TextPart(text=f"No active goal. {_GOAL_USAGE}"))
        return

    if subcmd == "clear":
        if state.goal is None:
            wire_send(TextPart(text="No active goal."))
            return
        state.goal = None
        soul.runtime.session.save_state()
        logger.info("Goal cleared via /goal")
        wire_send(TextPart(text="Goal cleared."))
        return

    if subcmd == "pause":
        if state.goal is None:
            wire_send(TextPart(text="No active goal."))
            return
        state.goal = GoalState(objective=state.goal.objective, status="paused")
        soul.runtime.session.save_state()
        wire_send(TextPart(text="Goal paused. Use /goal resume to pick it back up."))
        return

    if subcmd == "resume":
        if state.goal is None:
            wire_send(TextPart(text="No active goal."))
            return
        state.goal = GoalState(objective=state.goal.objective, status="active")
        soul.runtime.session.save_state()
        wire_send(TextPart(text="Goal resumed. The agent will pursue it again next turn."))
        return

    replaced = state.goal is not None
    state.goal = GoalState(objective=text, status="active")
    soul.runtime.session.save_state()
    from pythinker_code.telemetry import track

    track("goal_set", replaced=replaced)
    logger.info("Goal set via /goal")
    wire_send(
        TextPart(
            text=("Goal replaced: " if replaced else "Goal set: ")
            + text
            + "\nThe agent will pursue it across turns until verified or cleared "
            "with /goal clear."
        )
    )
    await soul.turn(Message(role="user", content=prompts.GOAL_SET.format(objective=text)))


@registry.command
async def learn(soul: PythinkerSoul, args: str):
    """Extract reusable lessons from this session and save them to project memory"""
    focus = args.strip()
    focus_line = (
        f"Focus especially on: {focus}"
        if focus
        else "No specific focus was given; review the whole session."
    )
    wire_send(TextPart(text="Reviewing the session for lessons worth keeping..."))
    await soul.turn(Message(role="user", content=prompts.LEARN.format(focus=focus_line)))


@registry.command(name="best-practices", aliases=["bp"])
async def best_practices(soul: PythinkerSoul, args: str):
    """Inject engineering best practices (code changes, testing, todos, debugging) into context"""
    section = args.strip()
    if section:
        content = _best_practices_section(section)
        if content is None:
            headings = ", ".join(_best_practices_headings())
            wire_send(
                TextPart(
                    text=f"Unknown section: {section}. Available sections: {headings}. "
                    "Run /best-practices without arguments to inject all of them."
                )
            )
            return
    else:
        content = prompts.BEST_PRACTICES

    system_message = system(content)
    await soul.context.append_message(Message(role="user", content=[system_message]))
    scope = f"section '{section}'" if section else "full guidance"
    wire_send(
        TextPart(text=f"Best practices injected ({scope}) — applied for the rest of this session.")
    )


@registry.command
async def benchmark(soul: PythinkerSoul, args: str) -> None:
    """
    Run native Pythinker Benchmark tasks.
    Usage: /benchmark <start|compare|estimate|list|show|report>
    """
    await _benchmark_dispatch(soul, args)


@registry.command(name="benchmark:start")
async def benchmark_start(soul: PythinkerSoul, args: str) -> None:
    """Start Pythinker Benchmark. Usage: /benchmark:start [--task <id> | --suite <name>]"""
    await _benchmark_dispatch(soul, _join_benchmark_args("start", args))


@registry.command(name="benchmark:all")
async def benchmark_all(soul: PythinkerSoul, args: str) -> None:
    """Run the default Pythinker Benchmark suite. Usage: /benchmark:all"""
    await _benchmark_dispatch(soul, _join_benchmark_args("start", args))


@registry.command(name="benchmark:estimate")
async def benchmark_estimate(soul: PythinkerSoul, args: str) -> None:
    """Estimate Pythinker Benchmark. Usage: /benchmark:estimate [--task <id> | --suite <name>]"""
    await _benchmark_dispatch(soul, _join_benchmark_args("estimate", args))


@registry.command(name="benchmark:list")
async def benchmark_list(soul: PythinkerSoul, args: str) -> None:
    """List Pythinker Benchmark tasks and suites. Usage: /benchmark:list"""
    await _benchmark_dispatch(soul, _join_benchmark_args("list", args))


@registry.command(name="benchmark:show")
async def benchmark_show(soul: PythinkerSoul, args: str) -> None:
    """Show a Pythinker Benchmark run. Usage: /benchmark:show <run-id>"""
    await _benchmark_dispatch(soul, _join_benchmark_args("show", args))


@registry.command(name="benchmark:report")
async def benchmark_report(soul: PythinkerSoul, args: str) -> None:
    """Show Pythinker Benchmark report index. Usage: /benchmark:report [--suite <name>]"""
    await _benchmark_dispatch(soul, _join_benchmark_args("report", args))


@registry.command(name="benchmark:compare")
async def benchmark_compare(soul: PythinkerSoul, args: str) -> None:
    """Compare Pythinker Benchmark models. Usage: /benchmark:compare --models <a,b>"""
    await _benchmark_dispatch(soul, _join_benchmark_args("compare", args))


@registry.command(name="benchmark:swe")
async def benchmark_swe(soul: PythinkerSoul, args: str) -> None:
    """Run trusted SWE-style local fixture benchmark tasks.

    Usage: /benchmark:swe --dataset <path> --trusted-dataset true
    """
    await _benchmark_dispatch(soul, _join_benchmark_args("swe", args))


async def _benchmark_dispatch(soul: PythinkerSoul, args: str) -> None:
    from pythinker_code.benchmark.commands import benchmark_usage, dispatch_benchmark
    from pythinker_code.benchmark.errors import BenchmarkError

    try:
        text = await dispatch_benchmark(soul, args)
    except BenchmarkError as exc:
        text = str(exc) or benchmark_usage()
    wire_send(TextPart(text=text))


def _join_benchmark_args(subcommand: str, args: str) -> str:
    return f"{subcommand} {args}".strip()


def _best_practices_headings() -> list[str]:
    return [
        line.removeprefix("## ").strip()
        for line in prompts.BEST_PRACTICES.splitlines()
        if line.startswith("## ")
    ]


def _best_practices_section(name: str) -> str | None:
    """Return the preamble plus the single `## ` section matching ``name``, or None."""
    lines = prompts.BEST_PRACTICES.splitlines()
    preamble: list[str] = []
    section: list[str] = []
    in_section = False
    matched = False
    for line in lines:
        if line.startswith("## "):
            heading = line.removeprefix("## ").strip()
            in_section = name.lower() in heading.lower()
            matched = matched or in_section
        elif not matched and not in_section:
            preamble.append(line)
        if in_section:
            section.append(line)
    if not matched:
        return None
    return "\n".join([*preamble, *section]).strip() + "\n"


@registry.command(name="add-dir")
async def add_dir(soul: PythinkerSoul, args: str):
    """Add a directory to the workspace. Usage: /add-dir <path>. Run without args to list added dirs"""  # noqa: E501
    from pythinker_host.path import HostPath

    from pythinker_code.utils.path import is_within_directory, list_directory

    args = sanitize_cli_path(args)
    if not args:
        if not soul.runtime.additional_dirs:
            wire_send(TextPart(text="No additional directories. Usage: /add-dir <path>"))
        else:
            lines = ["Additional directories:"]
            for d in soul.runtime.additional_dirs:
                lines.append(f"  - {d}")
            wire_send(TextPart(text="\n".join(lines)))
        return

    path = HostPath(args).expanduser().canonical()

    if not await path.exists():
        wire_send(TextPart(text=f"Directory does not exist: {path}"))
        return
    if not await path.is_dir():
        wire_send(TextPart(text=f"Not a directory: {path}"))
        return

    # Check if already added (exact match)
    if path in soul.runtime.additional_dirs:
        wire_send(TextPart(text=f"Directory already in workspace: {path}"))
        return

    # Check if it's within the work_dir (already accessible)
    work_dir = soul.runtime.builtin_args.PYTHINKER_WORK_DIR
    if is_within_directory(path, work_dir):
        wire_send(TextPart(text=f"Directory is already within the working directory: {path}"))
        return

    # Check if it's within an already-added additional directory (redundant)
    for existing in soul.runtime.additional_dirs:
        if is_within_directory(path, existing):
            wire_send(
                TextPart(
                    text=f"Directory is already within an added directory `{existing}`: {path}"
                )
            )
            return

    # Validate readability before committing any state changes
    try:
        ls_output = await list_directory(path)
    except OSError as e:
        wire_send(TextPart(text=f"Cannot read directory: {path} ({e})"))
        return

    # Add the directory (only after readability is confirmed)
    soul.runtime.additional_dirs.append(path)

    # Persist to session state
    soul.runtime.session.state.additional_dirs.append(str(path))
    soul.runtime.session.save_state()

    # Inject a system message to inform the LLM about the new directory
    system_message = system(
        f"The user has added an additional directory to the workspace: `{path}`\n\n"
        f"Directory listing:\n```\n{ls_output}\n```\n\n"
        "You can now read, write, search, and glob files in this directory "
        "as if it were part of the working directory."
    )
    await soul.context.append_message(Message(role="user", content=[system_message]))

    wire_send(TextPart(text=f"Added directory to workspace: {path}"))
    logger.info("Added additional directory: {path}", path=path)


@registry.command
async def export(soul: PythinkerSoul, args: str):
    """Export current session context to a markdown file"""
    from pythinker_code.utils.export import perform_export

    session = soul.runtime.session
    result = await perform_export(
        history=list(soul.context.history),
        session_id=session.id,
        work_dir=str(session.work_dir),
        token_count=soul.context.token_count,
        args=args,
        default_dir=Path(str(session.work_dir)),
    )
    if isinstance(result, str):
        wire_send(TextPart(text=result))
        return
    output, count = result
    display = shorten_home(HostPath(str(output)))
    wire_send(TextPart(text=f"Exported {count} messages to {display}"))
    wire_send(
        TextPart(
            text="  Note: The exported file may contain sensitive information. "
            "Please be cautious when sharing it externally."
        )
    )


@registry.command(name="import")
async def import_context(soul: PythinkerSoul, args: str):
    """Import context from a file or session ID"""
    from pythinker_code.utils.export import parse_import_args, perform_import

    target, force = parse_import_args(args)
    if not target:
        wire_send(TextPart(text="Usage: /import <file_path or session_id>"))
        return

    session = soul.runtime.session
    raw_max_context_size = (
        soul.runtime.llm.max_context_size if soul.runtime.llm is not None else None
    )
    max_context_size = (
        raw_max_context_size
        if isinstance(raw_max_context_size, int) and raw_max_context_size > 0
        else None
    )
    result = await perform_import(
        target=target,
        current_session_id=session.id,
        work_dir=session.work_dir,
        context=soul.context,
        max_context_size=max_context_size,
        force=force,
    )
    if isinstance(result, str):
        wire_send(TextPart(text=result))
        return

    source_desc, content_len = result
    wire_send(TextPart(text=f"Imported context from {source_desc} ({content_len} chars)."))
