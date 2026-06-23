from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import re
import shlex
import textwrap
import time
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from pythinker_code.ui.shell.usage_adapters.base import UsageRow

from pythinker_core.chat_provider import (
    APIConnectionError,
    APIEmptyResponseError,
    APIStatusError,
    APITimeoutError,
    ChatProviderError,
)
from rich import box
from rich.align import Align
from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.control import Control
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from pythinker_code.background import list_task_views
from pythinker_code.constant import get_version
from pythinker_code.llm import model_display_name
from pythinker_code.notifications import NotificationManager, NotificationWatcher
from pythinker_code.soul import (
    LLMNotSet,
    LLMNotSupported,
    MaxStepsReached,
    RunCancelled,
    Soul,
    run_soul,
)
from pythinker_code.soul.pythinkersoul import FLOW_COMMAND_PREFIX, PythinkerSoul
from pythinker_code.ui.shell.components.render_utils import (
    cell_width,
    render_message_response,
    sanitize_ansi,
    truncate_to_width,
)
from pythinker_code.ui.shell.console import console, current_console_width
from pythinker_code.ui.shell.echo import render_user_echo_text
from pythinker_code.ui.shell.mcp_status import render_mcp_prompt
from pythinker_code.ui.shell.prompt import (
    BgTaskCounts,
    CustomPromptSession,
    CwdLostError,
    PromptMode,
    UserInput,
    toast,
)
from pythinker_code.ui.shell.replay import replay_recent_history
from pythinker_code.ui.shell.slash import SKILL_COMMAND_PREFIX, shell_mode_registry
from pythinker_code.ui.shell.slash import registry as shell_slash_registry
from pythinker_code.ui.shell.update import (
    MANAGED_CHANNEL_MARKER,
    UpdateResult,
    _detect_upgrade_command,  # pyright: ignore[reportPrivateUsage]
    _mark_auto_update_check_attempt,  # pyright: ignore[reportPrivateUsage]
    _should_auto_check_for_updates,  # pyright: ignore[reportPrivateUsage]
    consume_whats_new,
    format_managed_channel_notice,
    pending_update_notice,
    refresh_update_cache_if_due,
    welcome_update_target,
)
from pythinker_code.ui.shell.update_orchestrator import (
    SMOKE_CHECK_FAILED_PREFIX,
    UpdateJobState,
    read_update_status,
    run_update_job,
)
from pythinker_code.ui.shell.visualize import (
    ApprovalPromptDelegate,
    visualize,
)
from pythinker_code.ui.terminal_capabilities import ascii_glyphs_enabled, motion_disabled
from pythinker_code.ui.theme import BRAND, BrandToken, tui_rich_style
from pythinker_code.ui.theme import get_tui_tokens as _get_tui_tokens
from pythinker_code.update_policy import auto_update_enabled
from pythinker_code.utils.aioqueue import QueueShutDown
from pythinker_code.utils.envvar import get_env_bool
from pythinker_code.utils.logging import logger
from pythinker_code.utils.signals import install_sigint_handler
from pythinker_code.utils.slashcmd import SlashCommand, SlashCommandCall, parse_slash_command_call
from pythinker_code.utils.subprocess_env import get_clean_env
from pythinker_code.utils.term import ensure_new_line, ensure_tty_sane
from pythinker_code.wire.types import (
    ApprovalRequest,
    ApprovalResponse,
    ContentPart,
    StatusUpdate,
    WireMessage,
)


@dataclass(slots=True)
class _PromptEvent:
    kind: str
    user_input: UserInput | None = None


_MAX_BG_AUTO_TRIGGER_FAILURES = 3
"""Stop auto-triggering after this many consecutive failures."""

_BG_AUTO_TRIGGER_INPUT_GRACE_S = 0.75
"""Delay background auto-trigger briefly after local prompt activity."""

_VISIBLE_WORKFLOW_SLASH_PREFIXES = (SKILL_COMMAND_PREFIX, FLOW_COMMAND_PREFIX)
"""Explicit skill/flow prefixes that should remain visible in transcript."""


def _background_idle_reminder(active_running: int) -> str:
    """Build the system-reminder injected when background tasks finish while idle.

    When sibling tasks are still running, steer the model to return control and
    rely on the automatic re-wake instead of blocking on a single task with
    ``TaskOutput(block=true)`` — blocking on one freezes the turn until the
    slowest of them finishes.
    """
    body = "Background tasks completed while you were idle."
    if active_running > 0:
        noun = "task is" if active_running == 1 else "tasks are"
        body += (
            f" {active_running} background {noun} still running. Do not block on a"
            " single task with TaskOutput(block=true); return control now and you"
            " will be automatically re-woken as each one finishes."
        )
    return f"<system-reminder>{body}</system-reminder>"


def _format_local_shell_output(
    *, stdout: str, stderr: str, returncode: int | None
) -> RenderableType | None:
    """Render local shell-mode output in the reference response sequence.

    Stdout appears first, then stderr, then a compact exit/no-output status,
    all under the caller's shared ``⎿`` gutter.
    """
    children: list[RenderableType] = []
    if stdout:
        children.append(Text(sanitize_ansi(stdout).rstrip("\n"), style=tui_rich_style("muted")))
    if stderr:
        children.append(Text(sanitize_ansi(stderr).rstrip("\n"), style=tui_rich_style("error")))
    if not stdout and not stderr:
        children.append(Text("(No output)", style=tui_rich_style("muted")))
    if returncode not in (None, 0):
        children.append(Text(f"exit {returncode}", style=tui_rich_style("error")))
    if not children:
        return None
    return Group(*children) if len(children) > 1 else children[0]


class _BackgroundCompletionWatcher:
    """Watches for background task completions and auto-triggers the agent.

    Sits between the idle event loop and the soul: when a background task
    finishes while the agent is idle *and* the LLM hasn't consumed the
    notification yet, it triggers a soul run.

    Important: pre-existing pending notifications alone should not trigger a
    foreground run immediately on session resume. They are consumed either by
    the next actual background completion signal or by the next user-triggered
    turn.
    """

    def __init__(
        self,
        soul: Soul,
        *,
        can_auto_trigger_pending: Callable[[], bool] | None = None,
    ) -> None:
        self._event: asyncio.Event | None = None
        self._notifications: NotificationManager | None = None
        self._can_auto_trigger_pending = can_auto_trigger_pending or (lambda: True)
        if isinstance(soul, PythinkerSoul):
            self._event = soul.runtime.background_tasks.completion_event
            self._notifications = soul.runtime.notifications

    @property
    def enabled(self) -> bool:
        return self._event is not None

    def clear(self) -> None:
        """Clear stale signals from the previous soul run."""
        if self._event is not None:
            self._event.clear()

    async def wait_for_next(self, idle_events: asyncio.Queue[_PromptEvent]) -> _PromptEvent | None:
        """Wait for either a user prompt event or a background completion.

        Returns the prompt event if user input arrived first, or ``None``
        if a background task completed with unclaimed LLM notifications.
        User input always takes priority over background completions.
        """
        if self.enabled and self._has_pending_llm_notifications():
            # Pending notifications already exist (for example after resume).
            # Before the user sends the first foreground turn after resume,
            # pending background notifications should not auto-trigger a run.
            # Once the shell is armed by a user-triggered turn, pending
            # notifications can resume the normal auto-follow-up behavior.
            try:
                return idle_events.get_nowait()
            except asyncio.QueueEmpty:
                if self._can_auto_trigger_pending():
                    return None

        idle_task = asyncio.create_task(idle_events.get())
        if not self.enabled:
            return await idle_task

        assert self._event is not None
        bg_wait_task = asyncio.create_task(self._event.wait())

        done, _ = await asyncio.wait(
            [idle_task, bg_wait_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in (idle_task, bg_wait_task):
            if t not in done:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

        if idle_task in done:
            if bg_wait_task in done:
                self._event.clear()
            return idle_task.result()

        # Only bg fired
        self._event.clear()
        if self._has_pending_llm_notifications():
            if self._can_auto_trigger_pending():
                return None
            return _PromptEvent(kind="bg_noop")
        return _PromptEvent(kind="bg_noop")

    def _has_pending_llm_notifications(self) -> bool:
        if self._notifications is None:
            return False
        return self._notifications.has_pending_for_sink("llm")


class _BackgroundAutoTriggerPromptState(Protocol):
    def has_pending_input(self) -> bool: ...

    def had_recent_input_activity(self, *, within_s: float) -> bool: ...

    def recent_input_activity_remaining(self, *, within_s: float) -> float: ...

    async def wait_for_input_activity(self) -> None: ...


_LM_STUDIO_NCTX_RE = re.compile(r"n_keep:\s*(\d+)\s*>=\s*n_ctx:\s*(\d+)")
_LM_STUDIO_LOAD_FAILED_RE = re.compile(r'Failed to load model\s+"([^"]+)"', re.IGNORECASE)
_LM_STUDIO_JINJA_ERROR_RE = re.compile(r"Error rendering prompt with jinja template", re.IGNORECASE)


def _is_lm_studio_context_too_small(exc: BaseException) -> bool:
    """Detect LM Studio's `n_keep:N >= n_ctx:M` error pattern.

    LM Studio returns an HTTP 400 with this message when the loaded
    context length is smaller than the prompt the agent is trying to send.
    """
    return _LM_STUDIO_NCTX_RE.search(str(exc)) is not None


def _parse_n_keep_n_ctx(message: str) -> tuple[int, int]:
    """Extract (n_keep, n_ctx) from an LM Studio context-too-small error.

    Returns (0, 0) if the pattern doesn't match — caller should have
    gated on `_is_lm_studio_context_too_small` first.
    """
    match = _LM_STUDIO_NCTX_RE.search(message)
    if match is None:
        return (0, 0)
    return (int(match.group(1)), int(match.group(2)))


def _is_lm_studio_load_failed(exc: BaseException) -> bool:
    """Detect LM Studio's `Failed to load model "<id>"` pattern.

    LM Studio returns this when JIT-loading on a chat request fails — usually
    VRAM exhaustion, but also: model file corrupted, model not compatible
    with the runtime, or the user manually evicted the model.
    """
    return _LM_STUDIO_LOAD_FAILED_RE.search(str(exc)) is not None


def _parse_lm_studio_load_failed_model(message: str) -> str:
    """Extract the failing model id; returns '' if the pattern doesn't match."""
    match = _LM_STUDIO_LOAD_FAILED_RE.search(message)
    return match.group(1) if match else ""


def _is_lm_studio_jinja_template_error(exc: BaseException) -> bool:
    """Detect LM Studio's jinja-template rendering errors.

    Many GGUF prompt templates are buggy or version-mismatched (e.g., apply
    string filter to a null value). The fix is on LM Studio's side — either
    switch model variant or override the template — so Pythinker can only
    point the user at the right place.
    """
    return _LM_STUDIO_JINJA_ERROR_RE.search(str(exc)) is not None


def _humanize_seconds(seconds: float) -> str:
    """Render a coarse, human-friendly duration like ``2d 3h`` / ``2h 5m`` / ``4m``."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "under a minute"


def _format_reset_window(error_obj: dict[str, object]) -> str | None:
    """Build ``Resets in 2h 5m (Jun 11 14:13)`` from a 429 payload's timing
    fields (``resets_in_seconds`` / ``resets_at``), or None when absent."""
    from datetime import datetime

    resets_in = error_obj.get("resets_in_seconds")
    resets_at = error_obj.get("resets_at")
    seconds: float | None = None
    if isinstance(resets_in, int | float) and not isinstance(resets_in, bool) and resets_in > 0:
        seconds = float(resets_in)
    when_text = ""
    if isinstance(resets_at, int | float) and not isinstance(resets_at, bool) and resets_at > 0:
        try:
            when_text = (
                datetime.fromtimestamp(float(resets_at)).astimezone().strftime("%b %d %H:%M")
            )
            if seconds is None:
                seconds = max(0.0, float(resets_at) - time.time())
        except (OverflowError, OSError, ValueError):
            pass
    if seconds is None:
        return None
    return f"Resets in {_humanize_seconds(seconds)}" + (f" ({when_text})" if when_text else "")


def _extract_429_detail(exc: BaseException) -> dict[str, str]:
    """Pull a human-readable summary + hint out of a 429 APIStatusError body.

    Providers vary widely in their 429 payload shape. We try the well-known
    `{"error": {"type": ..., "message": ...}}` envelope first (OpenAI /
    OpenCode / Anthropic / etc.), then fall back to scraping a URL hint and
    finally to the stringified exception.
    """
    body: dict[str, object] | None = None
    payload = getattr(exc, "body", None)
    if isinstance(payload, dict):
        body = cast(dict[str, object], payload)
    if body is None:
        for attr in ("response_json", "response_data"):
            value = getattr(exc, attr, None)
            if isinstance(value, dict):
                body = cast(dict[str, object], value)
                break
    if body is None:
        body = _parse_429_body_from_str(str(exc))
    if body is None:
        # Last-resort diagnostic: record the real exception so an unparsable
        # rate-limit payload can be turned into a precise fix instead of a guess.
        _capture_unparsed_429(exc)

    summary = ""
    raw_message = ""
    err_type = ""
    plan_type = ""
    reset_window: str | None = None
    if body is not None:
        err = body.get("error")
        if isinstance(err, dict):
            typed_err = cast(dict[str, object], err)
            err_type = str(typed_err.get("type") or "")
            raw_message = str(typed_err.get("message") or "")
            plan_type = str(typed_err.get("plan_type") or "")
            reset_window = _format_reset_window(typed_err)

    summary = raw_message
    if not summary:
        text = str(exc)
        summary = text if len(text) <= 280 else text[:277] + "..."

    # Rewrite the well-known usage-limit payload as plain English instead of
    # echoing the server's terser "The usage limit has been reached".
    if err_type == "usage_limit_reached" or "usage limit" in summary.lower():
        plan_label = f" on your {plan_type.capitalize()} plan" if plan_type else ""
        summary = f"Usage limit reached{plan_label}."

    hint = "Wait until the limit window resets, or upgrade / top up your plan."
    if "GoUsageLimitError" in err_type:
        hint = "OpenCode-Go monthly limit. Resets in the window the server stated above."
    elif "Anthropic" in str(type(exc).__module__) or "anthropic" in err_type.lower():
        hint = "Anthropic rate limit. Slow request rate, or check your plan tier."
    elif "openai" in str(type(exc).__module__).lower():
        hint = "OpenAI rate or usage limit. Check usage dashboard or wait for the reset window."

    # The raw server detail (type + original message) is kept on its own line so
    # the friendly summary stays clean but the underlying error is still visible.
    server_detail = " — ".join(part for part in (err_type, raw_message) if part)

    return {
        "summary": summary,
        "reset_window": reset_window or "",
        "server_detail": server_detail,
        "hint": hint,
    }


def _parse_429_body_from_str(text: str) -> dict[str, object] | None:
    """Recover the provider JSON from a stringified APIStatusError.

    Providers stringify a 429 in several ways:
      - ``Error code: 429 - {<python dict repr>}``  (OpenAI SDK, uses None/True)
      - ``Error code: 429 - {<json>}``              (uses null/true/false)
      - a bare ``{...}`` body
    We recover the first ``{...}`` object and parse it with ``ast.literal_eval``
    (Python repr) then ``json.loads`` (JSON). Both only parse data, no code runs.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    for parser in (ast.literal_eval, json.loads):
        try:
            parsed = parser(candidate)
        except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
            continue
        if isinstance(parsed, dict):
            return cast(dict[str, object], parsed)
    return None


def _capture_unparsed_429(exc: BaseException) -> None:
    """Append the raw 429 exception shape to a debug file so an unrecognised
    rate-limit payload can be diagnosed precisely. Best-effort; never raises."""
    try:
        from pythinker_code.share import get_share_dir

        path = get_share_dir() / "rate-limit-debug.log"
        body = getattr(exc, "body", None)
        line = (
            f"type={type(exc).__name__} "
            f"str={str(exc)[:1000]!r} "
            f"body_type={type(body).__name__} body={repr(body)[:1000]}\n"
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        logger.debug("Failed to capture unparsed 429 payload to debug log", exc_info=True)


def _render_429_message(detail: dict[str, str], usage_lines: list[str] | None = None) -> str:
    """Build the console line for a 429 rate/usage-limit error, escaping provider text.

    The summary/hint can carry raw provider text, so escape it before it reaches Rich —
    otherwise a message containing ``[...]`` is silently swallowed as invalid markup
    (consistent with how the sibling error branches escape provider strings).

    ``usage_lines`` are concrete reset windows fetched live from the provider's usage
    endpoint (the streaming 429 itself carries none), shown first as the most actionable
    information.
    """
    _t = _get_tui_tokens()
    lines = [f"[{_t.error}]Rate / usage limit hit: {escape(detail['summary'])}[/]"]
    for usage_line in usage_lines or []:
        lines.append(escape(usage_line))
    reset_window = detail.get("reset_window", "")
    if reset_window:
        lines.append(f"{escape(reset_window)}.")
    server_detail = detail.get("server_detail", "")
    if server_detail:
        lines.append(f"[dim]Server: {escape(server_detail)}[/dim]")
    lines.append(f"[dim]{escape(detail['hint'])}[/dim]")
    return "\n".join(lines)


def _format_usage_window_row(row: UsageRow) -> str:
    """Render one usage window (e.g. the 5-hour or weekly Codex limit) as a single
    line: ``5h window: 0% left · resets in 2h 14m``."""
    left = f"{row.used}% left" if row.unit == "%" else f"{row.used}/{row.limit}"
    reset = f" · {row.reset_hint}" if row.reset_hint else ""
    return f"{row.label}: {left}{reset}"


async def _codex_usage_windows(soul: Soul) -> list[str]:
    """Best-effort: fetch the live Codex 5-hour / weekly reset windows for the active
    ChatGPT-OAuth provider so a 429 can show concrete reset times — the streaming 429
    carries none. Returns ``[]`` for non-Codex providers or on any error/timeout."""
    if not isinstance(soul, PythinkerSoul):
        return []
    runtime = soul.runtime
    llm = runtime.llm
    if llm is None or llm.model_config is None:
        return []
    provider = runtime.config.providers.get(llm.model_config.provider)
    if provider is None or provider.type != "openai_codex" or provider.oauth is None:
        return []
    try:
        from pythinker_code.ui.shell.usage_adapters.openai_chatgpt import OpenAIChatGPTAdapter

        report = await asyncio.wait_for(
            OpenAIChatGPTAdapter().fetch(provider, runtime.oauth),
            timeout=3.0,
        )
    except Exception:
        logger.debug("Codex usage lookup for 429 message failed", exc_info=True)
        return []
    rows = ([report.summary] if report.summary else []) + report.limits
    return [_format_usage_window_row(row) for row in rows]


def _is_insufficient_credits_error(exc: BaseException) -> bool:
    """Detect an out-of-credits / billing failure.

    OpenCode Go (and similar gateways) return ``401`` with a
    ``{"error": {"type": "CreditsError", "message": "Insufficient balance ..."}}``
    body when the account has run out of credits. That is a billing problem, not
    a stale credential, so we must not tell the user to ``/login`` again.
    """
    err_type = ""
    message = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = cast(dict[str, object], body).get("error")
        if isinstance(err, dict):
            typed_err = cast(dict[str, object], err)
            err_type = str(typed_err.get("type") or "")
            message = str(typed_err.get("message") or "")
    haystack = f"{err_type} {message} {exc}".lower()
    return "creditserror" in haystack or "insufficient balance" in haystack


class Shell:
    def __init__(
        self,
        soul: Soul,
        welcome_info: list[WelcomeInfoItem] | None = None,
        prefill_text: str | None = None,
        suppress_banner: bool = False,
    ):
        self.soul = soul
        self._welcome_info = list(welcome_info or [])
        self._prefill_text = prefill_text
        # Skip the full welcome splash on an in-session reload (/model, /theme,
        # /new, fork, …): the screen still shows the previous banner and each
        # reload path prints its own "Switched to… / Reloading…" confirmation,
        # so reprinting the splash just stacks a redundant copy below it.
        self._suppress_banner = suppress_banner
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._prompt_session: CustomPromptSession | None = None
        # (timestamp, text) memo for the under-input update line; refreshed on a
        # short TTL so the hot toolbar render path does not stat the update
        # cache on every repaint (mirrors the footer's git-branch TTL).
        self._update_notice_cache: tuple[float, str | None] = (0.0, None)
        self._running_input_handler: Callable[[UserInput], None] | None = None
        self._running_interrupt_handler: Callable[[], None] | None = None
        self._active_approval_sink: Any | None = None
        self._active_view: Any | None = None
        self._pending_approval_requests = deque[ApprovalRequest]()
        self._current_prompt_approval_request: ApprovalRequest | None = None
        self._approval_modal: ApprovalPromptDelegate | None = None
        self._exit_after_run = False
        soul_slash_commands = list(soul.available_slash_commands)
        shell_slash_commands = shell_slash_registry.list_commands()
        self._available_slash_commands: dict[str, SlashCommand[Any]] = {
            **{cmd.name: cmd for cmd in soul_slash_commands},
            **{cmd.name: cmd for cmd in shell_slash_commands},
        }
        """Shell-level slash commands + soul-level slash commands. Primary name mapping."""
        self._available_slash_command_index = self._index_slash_commands(
            [*soul_slash_commands, *shell_slash_commands]
        )
        """Shell-level slash commands + soul-level slash commands.
        Primary name and alias mapping.
        """

    @property
    def available_slash_commands(self) -> dict[str, SlashCommand[Any]]:
        """Get all available slash commands, including shell-level and soul-level commands."""
        return self._available_slash_commands

    @staticmethod
    def _index_slash_commands(commands: list[SlashCommand[Any]]) -> dict[str, SlashCommand[Any]]:
        indexed: dict[str, SlashCommand[Any]] = {}
        for command in commands:
            indexed[command.name] = command
            for alias in command.aliases:
                indexed[alias] = command
        return indexed

    def _find_available_slash_command(self, name: str) -> SlashCommand[Any] | None:
        return self._available_slash_command_index.get(name) or self._available_slash_commands.get(
            name
        )

    def _print_cwd_lost_crash(self) -> None:
        """Print a crash report when the working directory is no longer accessible."""
        runtime = self.soul.runtime if isinstance(self.soul, PythinkerSoul) else None
        session_id = runtime.session.id if runtime else "unknown"
        work_dir = str(runtime.work_dir) if runtime else "unknown"

        info = Table.grid(padding=(0, 1))
        info.add_row("Session:", session_id)
        info.add_row("Working directory:", work_dir)

        panel = Panel(
            Group(
                Text(
                    "The working directory is no longer accessible "
                    "(external drive unplugged, directory deleted, or filesystem unmounted).",
                ),
                Text(""),
                info,
                Text(""),
                Text(
                    "Your conversation history has been saved. "
                    "Restart pythinker in a valid directory to continue.",
                    style="dim",
                ),
            ),
            title="[bold]Session crashed[/bold]",
            border_style=tui_rich_style("error"),
            box=box.ROUNDED,
        )
        console.print()
        console.print(panel)

    @staticmethod
    def _should_exit_input(user_input: UserInput | str) -> bool:
        command = user_input if isinstance(user_input, str) else user_input.command
        return command.strip() in {"exit", "quit", "/exit", "/quit"}

    @staticmethod
    def _agent_slash_command_call(user_input: UserInput) -> SlashCommandCall | None:
        if user_input.mode != PromptMode.AGENT:
            return None
        display_call = parse_slash_command_call(user_input.command)
        if display_call is None:
            return None
        resolved_call = parse_slash_command_call(user_input.resolved_command)
        if resolved_call is None or resolved_call.name != display_call.name:
            return display_call
        return resolved_call

    @staticmethod
    def _should_echo_workflow_slash_input(user_input: UserInput) -> bool:
        command_call = Shell._agent_slash_command_call(user_input)
        return command_call is not None and command_call.name.startswith(
            _VISIBLE_WORKFLOW_SLASH_PREFIXES
        )

    def _should_echo_agent_input(self, user_input: UserInput) -> bool:
        if user_input.mode != PromptMode.AGENT:
            return False
        if Shell._should_exit_input(user_input):
            return False
        # Phase 1 policy: keep operational slash commands hidden, but show
        # explicit `/skill:*` and `/flow:*` inputs because they represent
        # user-visible workflow intent and otherwise vanish from transcript
        # even when the command later fails to resolve.
        if self._should_echo_workflow_slash_input(user_input):
            return True
        return Shell._agent_slash_command_call(user_input) is None

    @staticmethod
    def _echo_agent_input(user_input: UserInput) -> None:
        console.print(render_user_echo_text(user_input.resolved_command))

    def _bind_running_input(
        self,
        on_input: Callable[[UserInput], None],
        on_interrupt: Callable[[], None],
    ) -> None:
        self._running_input_handler = on_input
        self._running_interrupt_handler = on_interrupt

    def _unbind_running_input(self) -> None:
        self._running_input_handler = None
        self._running_interrupt_handler = None

    async def _route_prompt_events(
        self,
        prompt_session: CustomPromptSession,
        idle_events: asyncio.Queue[_PromptEvent],
        resume_prompt: asyncio.Event,
    ) -> None:
        while True:
            # Keep exactly one active prompt read. Idle submissions pause the
            # router until the shell decides whether the next prompt should
            # wait for a blocking action or stay live during an agent run.
            await resume_prompt.wait()
            ensure_tty_sane()
            try:
                ensure_new_line()
                user_input = await prompt_session.prompt_next()
            except KeyboardInterrupt:
                logger.debug("Prompt router got KeyboardInterrupt")
                if (
                    self._running_input_handler is not None
                    and prompt_session.running_prompt_accepts_submission()
                ):
                    if self._running_interrupt_handler is not None:
                        self._running_interrupt_handler()
                    continue
                resume_prompt.clear()
                await idle_events.put(_PromptEvent(kind="interrupt"))
                continue
            except EOFError:
                logger.debug("Prompt router got EOF")
                if (
                    self._running_input_handler is not None
                    and prompt_session.running_prompt_accepts_submission()
                ):
                    self._exit_after_run = True
                    if self._running_interrupt_handler is not None:
                        self._running_interrupt_handler()
                    return
                resume_prompt.clear()
                await idle_events.put(_PromptEvent(kind="eof"))
                return
            except CwdLostError:
                logger.error("Working directory no longer exists")
                resume_prompt.clear()
                await idle_events.put(_PromptEvent(kind="cwd_lost"))
                return
            except Exception:
                logger.exception("Prompt router crashed")
                resume_prompt.clear()
                await idle_events.put(_PromptEvent(kind="error"))
                return

            if prompt_session.last_submission_was_running:  # noqa: SIM102
                if self._running_input_handler is not None:
                    if user_input:
                        self._running_input_handler(user_input)
                    continue
                # Handler already unbound — fall through to idle path.

            resume_prompt.clear()
            await idle_events.put(_PromptEvent(kind="input", user_input=user_input))

    def _register_task_label_resolver(self) -> None:
        """Let TaskOutput/TaskStop headers show a task's friendly description
        (resolved from the background-task store) instead of the opaque id,
        even while the task is still running."""
        if not isinstance(self.soul, PythinkerSoul):
            return
        from pythinker_code.ui.shell.tool_renderers.background import set_task_label_resolver

        store = self.soul.runtime.background_tasks.store

        def _resolve(task_id: str) -> str | None:
            try:
                description = store.merged_view(task_id).spec.description
            except Exception:  # noqa: BLE001 - missing/unreadable task must not crash the UI
                return None
            return description or None

        set_task_label_resolver(_resolve)

    async def run(self, command: str | None = None) -> bool:
        _run_start_time = time.monotonic()

        # Initialize theme + TUI style from config
        if isinstance(self.soul, PythinkerSoul):
            from pythinker_code.extensions import run_pending_extensions
            from pythinker_code.ui.shell.visualize._blocks import set_smooth_streaming
            from pythinker_code.ui.terminal_background import resolve_theme_name
            from pythinker_code.ui.theme import set_active_theme
            from pythinker_code.ui.tui_config import (
                is_card_style,
                set_active_tui_style,
            )
            from pythinker_code.utils.rich.syntax import set_active_code_theme

            set_active_theme(resolve_theme_name(self.soul.runtime.config.theme))
            set_active_tui_style(self.soul.runtime.config.tui.style)
            set_active_code_theme(self.soul.runtime.config.tui.code_theme)
            set_smooth_streaming(self.soul.runtime.config.tui.smooth_streaming)
            if is_card_style():
                from pythinker_code.ui.shell.tool_renderers import (
                    register_builtin_renderers,
                )

                register_builtin_renderers()
                self._register_task_label_resolver()

            # Run any pending extension setup callbacks. Safe to call when
            # nothing's queued — the function returns an empty list and
            # extensions register lazily.
            started = run_pending_extensions()
            if started:
                logger.debug("Started extensions: {names}", names=", ".join(started))

        if command is not None:
            # run single command and exit
            logger.info("Running agent with command: {command}", command=command)
            if isinstance(self.soul, PythinkerSoul):
                self._start_background_task(self._watch_root_wire_hub())
            try:
                if self._should_exit_input(command):
                    console.print("Bye!")
                    return True
                if (slash_cmd_call := parse_slash_command_call(command)) and (
                    shell_slash_registry.find_command(slash_cmd_call.name)
                ):
                    await self._run_slash_command(slash_cmd_call)
                    return True
                return await self.run_soul_command(command)
            finally:
                self._cancel_background_tasks()

        # Auto-update at startup is silent + non-blocking (default on). The old
        # blocking pre-start prompt is intentionally gone; the function remains
        # in update_orchestrator.py for future re-wiring.
        self._schedule_startup_update_task()

        if isinstance(self.soul, PythinkerSoul):
            # Kick off MCP loading before the banner so servers connect in the
            # background while the user reads it; the prompt's MCP status line
            # carries the blinking "connecting" heartbeat without ever
            # blocking input.
            await self.soul.start_background_mcp_loading()
        if not self._suppress_banner:
            _print_welcome_info(
                self.soul.name or "Pythinker CLI",
                self._welcome_info,
                banner=_welcome_banner_chip(),
            )

        # Start telemetry periodic flush and disk retry
        from pythinker_code.telemetry import get_sink

        _telemetry_sink = get_sink()
        if _telemetry_sink is not None:
            _telemetry_sink.start_periodic_flush()
            self._start_background_task(_telemetry_sink.retry_disk_events())

        if isinstance(self.soul, PythinkerSoul):
            watcher = NotificationWatcher(
                self.soul.runtime.notifications,
                sink="shell",
                before_poll=self.soul.runtime.background_tasks.reconcile,
                on_notification=lambda notification: toast(
                    f"[{notification.event.type}] {notification.event.title}",
                    topic="notification",
                    duration=10.0,
                ),
            )
            self._start_background_task(watcher.run_forever())
            self._start_background_task(self._watch_root_wire_hub())
            await replay_recent_history(
                self.soul.context.history,
                wire_file=self.soul.wire_file,
                show_thinking_stream=self.soul.runtime.config.show_thinking_stream,
            )

        async def _plan_mode_toggle() -> bool:
            if isinstance(self.soul, PythinkerSoul):
                return await self.soul.toggle_plan_mode_from_manual()
            return False

        async def _thinking_effort_cycle() -> str | None:
            if isinstance(self.soul, PythinkerSoul):
                return self.soul.cycle_thinking_effort_from_manual()
            return None

        def _mcp_status_block(columns: int):
            if not isinstance(self.soul, PythinkerSoul):
                return None
            snapshot = self.soul.status.mcp_status
            if snapshot is None:
                return None
            return render_mcp_prompt(snapshot)

        def _mcp_status_loading() -> bool:
            if not isinstance(self.soul, PythinkerSoul):
                return False
            snapshot = self.soul.status.mcp_status
            return bool(snapshot and snapshot.loading)

        @dataclass
        class _BgCountCache:
            time: float = 0.0
            counts: BgTaskCounts = BgTaskCounts()

        _bg_cache = _BgCountCache()

        def _bg_task_counts() -> BgTaskCounts:
            if not isinstance(self.soul, PythinkerSoul):
                return BgTaskCounts()
            now = time.monotonic()
            if now - _bg_cache.time < 1.0:
                return _bg_cache.counts
            views = list_task_views(self.soul.runtime.background_tasks, active_only=True)
            bash_n = sum(1 for v in views if v.spec.kind == "bash")
            agent_n = sum(1 for v in views if v.spec.kind == "agent")
            _bg_cache.counts = BgTaskCounts(bash=bash_n, agent=agent_n)
            _bg_cache.time = now
            return _bg_cache.counts

        with CustomPromptSession(
            status_provider=lambda: self.soul.status,
            status_block_provider=_mcp_status_block,
            fast_refresh_provider=_mcp_status_loading,
            background_task_count_provider=_bg_task_counts,
            update_notice_provider=self._update_notice_text,
            model_capabilities=self.soul.model_capabilities or set(),
            model_name=model_display_name(
                self.soul.model_name,
                self.soul.runtime.llm.model_config
                if isinstance(self.soul, PythinkerSoul) and self.soul.runtime.llm
                else None,
            ),
            thinking=self.soul.thinking or False,
            thinking_effort=(
                self.soul.thinking_effort if isinstance(self.soul, PythinkerSoul) else None
            ),
            agent_mode_slash_commands=list(self._available_slash_commands.values()),
            shell_mode_slash_commands=shell_mode_registry.list_commands(),
            editor_command_provider=lambda: (
                self.soul.runtime.config.default_editor
                if isinstance(self.soul, PythinkerSoul)
                else ""
            ),
            turn_recaps_provider=lambda: (
                self.soul.runtime.config.tui.turn_recaps
                if isinstance(self.soul, PythinkerSoul)
                else False
            ),
            plan_mode_toggle_callback=_plan_mode_toggle,
            thinking_effort_cycle_callback=_thinking_effort_cycle,
            history_enabled=(
                self.soul.runtime.config.tui.prompt_history_enabled
                if isinstance(self.soul, PythinkerSoul)
                else True
            ),
            statusline_config=(
                self.soul.runtime.config.tui.statusline
                if isinstance(self.soul, PythinkerSoul)
                else None
            ),
        ) as prompt_session:
            self._prompt_session = prompt_session
            if self._prefill_text:
                prompt_session.set_prefill_text(self._prefill_text)
                self._prefill_text = None
            if isinstance(self.soul, PythinkerSoul):
                pythinker_soul = self.soul
                snapshot = pythinker_soul.status.mcp_status
                if snapshot is not None:

                    async def _invalidate_after_mcp_loading() -> None:
                        try:
                            await pythinker_soul.wait_for_background_mcp_loading()
                        except Exception:
                            logger.debug("MCP loading finished with error while refreshing prompt")
                        # Loading finished: repaint so the bottom-toolbar MCP line
                        # (rendered below the input) drops away once it is no longer
                        # loading.
                        if self._prompt_session is prompt_session:
                            prompt_session.invalidate()

                    self._start_background_task(_invalidate_after_mcp_loading())
            self._exit_after_run = False
            idle_events: asyncio.Queue[_PromptEvent] = asyncio.Queue()
            # resume_prompt controls whether the prompt router reads input.
            # Set BEFORE an await = prompt stays live during the operation
            # (agent runs that accept steer input); set AFTER = prompt is
            # paused until the operation finishes.
            resume_prompt = asyncio.Event()
            resume_prompt.set()
            prompt_task = asyncio.create_task(
                self._route_prompt_events(prompt_session, idle_events, resume_prompt)
            )
            background_autotrigger_armed = False

            def _can_auto_trigger_pending() -> bool:
                return background_autotrigger_armed

            bg_watcher = _BackgroundCompletionWatcher(
                self.soul,
                can_auto_trigger_pending=_can_auto_trigger_pending,
            )

            shell_ok = True
            bg_auto_failures = 0
            deferred_bg_trigger = False
            try:
                while True:
                    if deferred_bg_trigger and not self._should_defer_background_auto_trigger(
                        prompt_session
                    ):
                        result = None
                    elif deferred_bg_trigger:
                        result = await self._wait_for_input_or_activity(
                            prompt_session,
                            idle_events,
                            timeout_s=self._background_auto_trigger_timeout_s(prompt_session),
                        )
                    else:
                        bg_watcher.clear()
                        if bg_auto_failures >= _MAX_BG_AUTO_TRIGGER_FAILURES:
                            result = await idle_events.get()
                        else:
                            result = await bg_watcher.wait_for_next(idle_events)

                    if result is None:
                        if self._should_defer_background_auto_trigger(prompt_session):
                            deferred_bg_trigger = True
                            resume_prompt.set()
                            continue
                        deferred_bg_trigger = False
                        logger.info("Background task completed while idle, triggering agent")
                        resume_prompt.set()
                        active_running = 0
                        if isinstance(self.soul, PythinkerSoul):
                            try:
                                active_running = len(
                                    list_task_views(
                                        self.soul.runtime.background_tasks, active_only=True
                                    )
                                )
                            except Exception:
                                logger.debug(
                                    "Failed to compute active background task count for "
                                    "idle reminder",
                                    exc_info=True,
                                )
                        ok = await self.run_soul_command(_background_idle_reminder(active_running))
                        console.print()
                        if not ok:
                            bg_auto_failures += 1
                            logger.warning(
                                "Background auto-trigger failed ({n}/{max})",
                                n=bg_auto_failures,
                                max=_MAX_BG_AUTO_TRIGGER_FAILURES,
                            )
                        else:
                            bg_auto_failures = 0
                        if self._exit_after_run:
                            console.print("Bye!")
                            break
                        continue

                    event = result

                    if event.kind == "input_activity":
                        continue

                    if event.kind == "bg_noop":
                        continue

                    if event.kind == "interrupt":
                        _t = _get_tui_tokens()
                        console.print(f"[{_t.muted}]Tip: press Ctrl-D or send 'exit' to quit[/]")
                        resume_prompt.set()
                        continue

                    if event.kind == "eof":
                        console.print("Bye!")
                        break

                    if event.kind == "cwd_lost":
                        self._print_cwd_lost_crash()
                        shell_ok = False
                        break

                    if event.kind == "error":
                        shell_ok = False
                        break

                    user_input = event.user_input
                    assert user_input is not None
                    bg_auto_failures = 0
                    deferred_bg_trigger = False
                    if not user_input:
                        logger.debug("Got empty input, skipping")
                        resume_prompt.set()
                        continue
                    logger.debug("Got user input: {user_input}", user_input=user_input)

                    if self._should_echo_agent_input(user_input):
                        self._echo_agent_input(user_input)

                    if self._should_exit_input(user_input):
                        logger.debug("Exiting by slash command")
                        console.print("Bye!")
                        break

                    if user_input.mode == PromptMode.SHELL:
                        await self._run_shell_command(user_input.resolved_command)
                        resume_prompt.set()
                        continue

                    # Unified input routing — intercept local commands
                    # before they reach the soul/wire.
                    from pythinker_code.ui.shell.visualize import InputAction, classify_input

                    # Use resolved_command (placeholder-expanded) so /btw
                    # receives the actual pasted content, not "[Pasted text #1]".
                    input_text = (
                        user_input.resolved_command
                        if hasattr(user_input, "resolved_command")
                        else str(user_input)
                    )
                    action = classify_input(input_text, is_streaming=False)
                    if action.kind == InputAction.BTW and isinstance(self.soul, PythinkerSoul):
                        from pythinker_code.telemetry import track

                        track("input_btw")
                        await self._run_btw_modal(action.args, prompt_session)
                        resume_prompt.set()
                        continue
                    if action.kind == InputAction.IGNORED:
                        console.print(f"[dim]{escape(str(action.args))}[/dim]")
                        resume_prompt.set()
                        continue

                    if slash_cmd_call := self._agent_slash_command_call(user_input):
                        available_command = self._find_available_slash_command(slash_cmd_call.name)
                        is_soul_slash = (
                            available_command is not None
                            and shell_slash_registry.find_command(slash_cmd_call.name) is None
                        )
                        if is_soul_slash:
                            from pythinker_code.telemetry import track

                            track("input_command", command=slash_cmd_call.name)
                            background_autotrigger_armed = True
                            resume_prompt.set()
                            await self.run_soul_command(slash_cmd_call.raw_input)
                            console.print()
                            if self._exit_after_run:
                                console.print("Bye!")
                                break
                        else:
                            await self._run_slash_command(slash_cmd_call)
                            resume_prompt.set()
                        continue

                    background_autotrigger_armed = True
                    resume_prompt.set()
                    await self.run_soul_command(user_input.content)
                    console.print()
                    if self._exit_after_run:
                        console.print("Bye!")
                        break
            finally:
                prompt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await prompt_task
                self._running_input_handler = None
                self._running_interrupt_handler = None
                if self._prompt_session is prompt_session and self._approval_modal is not None:
                    prompt_session.detach_modal(self._approval_modal)
                    self._approval_modal = None
                self._prompt_session = None
                self._cancel_background_tasks()
                # Track exit and flush remaining telemetry events.
                # Cap the exit-path flush at 3 s so we don't block for ~50 s
                # when the endpoint is unreachable (in-process retry backoff).
                # On timeout the CancelledError handler in transport.send()
                # persists in-flight events to disk; flush_sync() catches any
                # events still in the buffer.
                from pythinker_code.telemetry import track

                track("exit", duration_s=time.monotonic() - _run_start_time)
                if _telemetry_sink is not None:
                    _telemetry_sink.stop_periodic_flush()
                    try:
                        await asyncio.wait_for(_telemetry_sink.flush(), timeout=3.0)
                    except (TimeoutError, Exception):
                        _telemetry_sink.flush_sync()
                ensure_tty_sane()

        return shell_ok

    async def _run_shell_command(self, command: str) -> None:
        """Run a shell command in foreground."""
        if not command.strip():
            return

        # Check if it's an allowed slash command in shell mode
        if slash_cmd_call := parse_slash_command_call(command):
            if shell_mode_registry.find_command(slash_cmd_call.name):
                await self._run_slash_command(slash_cmd_call)
                return
            else:
                _t = _get_tui_tokens()
                console.print(
                    f'[{_t.warning}]"/{slash_cmd_call.name}" is not available in shell mode. '
                    "Press Ctrl-X to switch to agent mode.[/]"
                )
                return

        # Check if user is trying to use 'cd' command
        stripped_cmd = command.strip()
        split_cmd: list[str] | None = None
        try:
            split_cmd = shlex.split(stripped_cmd)
        except ValueError as exc:
            logger.debug("Failed to parse shell command for cd check: {error}", error=exc)
        if split_cmd and len(split_cmd) == 2 and split_cmd[0] == "cd":
            _t = _get_tui_tokens()
            console.print(
                f"[{_t.warning}]Warning: Directory changes are not preserved "
                "across command executions.[/]"
            )
            return

        logger.info("Running shell command: {cmd}", cmd=command)
        from pythinker_code.telemetry import track

        track("input_bash")

        proc: asyncio.subprocess.Process | None = None
        max_output_bytes = 1_000_000

        async def _read_stream_limited(stream: asyncio.StreamReader | None, limit: int) -> bytes:
            if stream is None:
                return b""
            chunks: list[bytes] = []
            total = 0
            truncated = False
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                remaining = limit - total
                if remaining > 0:
                    chunks.append(chunk[:remaining])
                    total += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    truncated = True
            if truncated:
                chunks.append(b"\n... output truncated ...\n")
            return b"".join(chunks)

        def _handler():
            logger.debug("SIGINT received.")
            if proc:
                proc.terminate()

        loop = asyncio.get_running_loop()
        remove_sigint = install_sigint_handler(loop, _handler)
        try:
            # TODO: For the sake of simplicity, we now use `create_subprocess_shell`.
            # Later we should consider making this behave like a real shell.
            proc = await asyncio.create_subprocess_shell(
                command,
                env=get_clean_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_task = asyncio.create_task(_read_stream_limited(proc.stdout, max_output_bytes))
            stderr_task = asyncio.create_task(_read_stream_limited(proc.stderr, max_output_bytes))
            await proc.wait()
            stdout_bytes, stderr_bytes = await asyncio.gather(stdout_task, stderr_task)
            stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
            output = _format_local_shell_output(
                stdout=stdout,
                stderr=stderr,
                returncode=proc.returncode,
            )
            if output is not None:
                console.print(render_message_response(output))
        except Exception as e:
            logger.exception("Failed to run shell command:")
            console.print(
                f"[{_get_tui_tokens().error}]Failed to run shell command: {escape(str(e))}[/]"
            )
        finally:
            remove_sigint()

    async def _run_slash_command(self, command_call: SlashCommandCall) -> None:
        from pythinker_code.cli import Reload, SwitchToDashboard, SwitchToWeb
        from pythinker_code.telemetry import track

        available_command = self._find_available_slash_command(command_call.name)
        if available_command is None:
            logger.info("Unknown slash command /{command}", command=command_call.name)
            track("input_command_invalid")
            _t = _get_tui_tokens()
            console.print(
                f'[{_t.error}]Unknown slash command "/{command_call.name}", '
                'type "/" for all available commands[/]'
            )
            return

        track("input_command", command=available_command.name)

        command = shell_slash_registry.find_command(command_call.name)
        if command is None:
            # the input is a soul-level slash command call
            await self.run_soul_command(command_call.raw_input)
            return

        logger.debug(
            "Running shell-level slash command: /{command} with args: {args}",
            command=command_call.name,
            args=command_call.args,
        )

        try:
            ret = command.func(self, command_call.args)
            if isinstance(ret, Awaitable):
                await ret
        except (Reload, SwitchToWeb, SwitchToDashboard):
            # just propagate
            raise
        except (asyncio.CancelledError, KeyboardInterrupt):
            # Handle Ctrl-C during slash command execution, return to shell prompt
            logger.debug("Slash command interrupted by KeyboardInterrupt")
            console.print(f"[{_get_tui_tokens().error}]Interrupted by user[/]")
        except Exception as e:
            logger.exception("Unknown error:")
            console.print(f"[{_get_tui_tokens().error}]Unknown error: {escape(str(e))}[/]")
            raise  # re-raise unknown error

    async def _run_slash_command_during_task(self, command_call: SlashCommandCall) -> None:
        """Run a task-safe shell command typed while a turn is streaming.

        Reload/mode-switch control flow cannot be honored mid-turn; any config
        change is already saved, so report that it applies later instead of
        letting the exception escape the fire-and-forget task.
        """
        from pythinker_code.cli import Reload, SwitchToDashboard, SwitchToWeb

        _t = _get_tui_tokens()
        try:
            await self._run_slash_command(command_call)
        except Reload:
            console.print(
                f"[{_t.warning}]Settings saved — restart pythinker after the "
                f"current task to apply them.[/]"
            )
        except (SwitchToWeb, SwitchToDashboard):
            console.print(
                f"[{_t.warning}]Mode switches are unavailable while a task is in progress.[/]"
            )
        except Exception:
            logger.exception("Error running /{command} during task", command=command_call.name)

    async def run_soul_command(self, user_input: str | list[ContentPart]) -> bool:
        """
        Run the soul and handle any known exceptions.

        Returns:
            bool: Whether the run is successful.
        """
        logger.info("Running soul with user input: {user_input}", user_input=user_input)

        cancel_event = asyncio.Event()

        def _handler():
            logger.debug("SIGINT received.")
            cancel_event.set()

        loop = asyncio.get_running_loop()
        remove_sigint = install_sigint_handler(loop, _handler)

        # Declare before try so finally can always access it.
        from pythinker_code.ui.shell.visualize import (
            _PromptLiveView,  # pyright: ignore[reportPrivateUsage]
        )

        captured_view: _PromptLiveView | None = None
        pending: list[UserInput] = []  # queued messages being drained

        try:
            snap = self.soul.status
            runtime = self.soul.runtime if isinstance(self.soul, PythinkerSoul) else None
            show_thinking_stream = runtime.config.show_thinking_stream if runtime else False
            show_turn_recaps = runtime.config.tui.turn_recaps if runtime else False
            # Capture view reference via closure — _clear_active_view sets
            # _active_view=None inside visualize()'s finally (before run_soul
            # returns), so we must capture the view object independently.

            def _on_view_ready(view: Any) -> None:
                nonlocal captured_view
                self._set_active_view(view)
                if isinstance(view, _PromptLiveView):
                    captured_view = view

            if runtime is not None:
                runtime.background_tasks.begin_turn()
            await run_soul(
                self.soul,
                user_input,
                lambda wire: visualize(
                    wire.ui_side(merge=False),  # shell UI maintain its own merge buffer
                    initial_status=StatusUpdate(
                        context_usage=snap.context_usage,
                        context_tokens=snap.context_tokens,
                        max_context_tokens=snap.max_context_tokens,
                        mcp_status=snap.mcp_status,
                    ),
                    cancel_event=cancel_event,
                    prompt_session=self._prompt_session,
                    steer=self.soul.steer if isinstance(self.soul, PythinkerSoul) else None,
                    btw_runner=self._make_btw_runner(),
                    shell_command_runner=self._run_slash_command_during_task,
                    bind_running_input=self._bind_running_input,
                    unbind_running_input=self._unbind_running_input,
                    on_view_ready=_on_view_ready,
                    on_view_closed=self._clear_active_view,
                    show_thinking_stream=show_thinking_stream,
                    show_turn_recaps=show_turn_recaps,
                ),
                cancel_event,
                runtime.session.wire_file if runtime else None,
                runtime,
            )
            # If btw is still showing, wait for user dismiss BEFORE draining
            # queue.  This runs AFTER visualize_loop returns (within run_soul's
            # 0.5s ui_task timeout), so the btw modal is still attached to
            # prompt_session and key events continue to work.
            if captured_view is not None:
                await captured_view.wait_for_btw_dismiss()

            # Clear cancel_event so queued turns aren't tainted by a
            # Ctrl+C that fired during btw dismiss wait.
            cancel_event.clear()

            # Drain queued messages and send each as a new turn.
            # Safety valve: cap at 20 "generations" (new batches of messages
            # from the view). A one-time backlog of 25 messages = 1 generation,
            # but a user adding new messages every turn = 1 generation per turn.
            _MAX_DRAIN_GENERATIONS = 20
            pending.clear()
            drain_generation = 0
            while captured_view is not None and drain_generation < _MAX_DRAIN_GENERATIONS:
                new_messages = captured_view.drain_queued_messages()
                if new_messages:
                    drain_generation += 1
                pending.extend(new_messages)
                if not pending:
                    break
                queued = pending.pop(0)
                console.print(render_user_echo_text(queued.resolved_command))
                if runtime is not None:
                    runtime.background_tasks.begin_turn()
                await run_soul(
                    self.soul,
                    queued.content,
                    lambda wire: visualize(
                        wire.ui_side(merge=False),
                        initial_status=StatusUpdate(
                            context_usage=self.soul.status.context_usage,
                            context_tokens=self.soul.status.context_tokens,
                            max_context_tokens=self.soul.status.max_context_tokens,
                            mcp_status=self.soul.status.mcp_status,
                        ),
                        cancel_event=cancel_event,
                        prompt_session=self._prompt_session,
                        steer=self.soul.steer if isinstance(self.soul, PythinkerSoul) else None,
                        btw_runner=self._make_btw_runner(),
                        shell_command_runner=self._run_slash_command_during_task,
                        bind_running_input=self._bind_running_input,
                        unbind_running_input=self._unbind_running_input,
                        on_view_ready=_on_view_ready,
                        on_view_closed=self._clear_active_view,
                        show_thinking_stream=show_thinking_stream,
                        show_turn_recaps=show_turn_recaps,
                    ),
                    cancel_event,
                    runtime.session.wire_file if runtime else None,
                    runtime,
                )
                # Wait for btw dismiss if one was triggered during this queued turn
                if captured_view is not None:
                    await captured_view.wait_for_btw_dismiss()
                cancel_event.clear()  # same rationale as above
                # captured_view is now the view from this turn;
                # next iteration drains it for any new messages.
            if drain_generation >= _MAX_DRAIN_GENERATIONS:
                logger.warning(
                    "Queue drain hit safety limit ({n} generations)",
                    n=_MAX_DRAIN_GENERATIONS,
                )
                # Warn about remaining items in the local pending buffer.
                # Clear after printing so finally doesn't duplicate.
                for msg in pending:
                    console.print(
                        f"[{_get_tui_tokens().warning}]Queued message dropped: {msg.command}[/]"
                    )
                pending.clear()
            return True
        except LLMNotSet:
            _t = _get_tui_tokens()
            logger.warning("LLM not set — user has no provider configured")
            console.print(f'[{_t.error}]LLM not set, send "/login" to login[/]')
        except LLMNotSupported as e:
            # actually unsupported input/mode should already be blocked by prompt session
            _t = _get_tui_tokens()
            logger.exception("LLM not supported:")
            console.print(f"[{_t.error}]{escape(str(e))}[/]")
        except ChatProviderError as e:
            _t = _get_tui_tokens()
            logger.exception("LLM provider error:")
            if isinstance(e, APIStatusError) and e.status_code == 401:
                if _is_insufficient_credits_error(e):
                    console.print(
                        f"[{_t.error}]Insufficient balance — your account is out of credits.[/]\n"
                        "[dim]This is a billing issue, not a login problem. Top up or manage "
                        "billing (see the link in the server message below), then retry.[/dim]\n"
                        f"[dim]Server: {e}[/dim]"
                    )
                else:
                    console.print(
                        f"[{_t.error}]Authorization failed. Your session may have expired.[/]\n"
                        "[dim]Type [bold]/login[/bold] to re-authenticate.[/dim]\n"
                        f"[dim]Server: {e}[/dim]"
                    )
            elif isinstance(e, APIStatusError) and e.status_code == 402:
                console.print(
                    f"[{_t.error}]Membership expired, please renew your plan[/]\n"
                    f"[dim]Server: {e}[/dim]"
                )
            elif isinstance(e, APIStatusError) and e.status_code == 403:
                console.print(
                    f"[{_t.error}]Quota exceeded, please upgrade your plan or retry later[/]\n"
                    f"[dim]Server: {e}[/dim]"
                )
            elif isinstance(e, APIStatusError) and e.status_code == 429:
                usage_lines = await _codex_usage_windows(self.soul)
                console.print(_render_429_message(_extract_429_detail(e), usage_lines=usage_lines))
            elif isinstance(e, APIConnectionError):
                console.print(
                    f"[{_t.error}]Network connection failed: {e}[/]\n"
                    "[dim]Please check your network and try again.[/dim]"
                )
            elif isinstance(e, APITimeoutError):
                console.print(
                    f"[{_t.error}]Request timed out: {e}[/]\n"
                    "[dim]The server may be slow or unreachable. Please try again later.[/dim]"
                )
            elif isinstance(e, APIEmptyResponseError):
                console.print(
                    f"[{_t.error}]The server returned an empty response.[/]\n"
                    "[dim]This is usually a temporary issue. Please try again.[/dim]"
                )
            elif _is_lm_studio_context_too_small(e):
                n_keep, n_ctx = _parse_n_keep_n_ctx(str(e))
                console.print(
                    f"[{_t.error}]LM Studio's loaded context window is too small "
                    f"(loaded n_ctx={n_ctx}, agent needs at least {n_keep}).[/]\n"
                    "[dim]To fix:[/dim]\n"
                    "[dim]  1. In LM Studio, open the model in the Chat tab "
                    "and click the gear/settings icon (or use 'My Models' → 'Edit').[/dim]\n"
                    "[dim]  2. Set [bold]Context Length[/bold] to at least "
                    f"[bold]{max(n_keep + 4096, 32768)}[/bold] tokens (or the model's max).[/dim]\n"
                    "[dim]  3. Reload the model.[/dim]\n"
                    "[dim]  4. Restart pythinker (Ctrl+D then `uv run pythinker` "
                    "or `pythinker -r <session-id>` to resume).[/dim]"
                )
            elif _is_lm_studio_load_failed(e):
                failed_model = _parse_lm_studio_load_failed_model(str(e))
                model_label = failed_model or "the requested model"
                console.print(
                    f"[{_t.error}]LM Studio could not load {model_label}.[/]\n"
                    "[dim]Most common cause: VRAM exhausted (the model is too big "
                    "for your GPU at its current quantization).[/dim]\n"
                    "[dim]To fix:[/dim]\n"
                    "[dim]  1. Switch to a smaller model: [bold]/model[/bold] and "
                    "pick one with fewer parameters or a lower-bit quantization "
                    "(e.g., Q4_K_M instead of Q8_0).[/dim]\n"
                    "[dim]  2. Or in LM Studio: unload other models "
                    "(My Models → eject), then try again.[/dim]\n"
                    "[dim]  3. Check the LM Studio app for the underlying error "
                    "(Developer → Logs).[/dim]\n"
                    "[dim]Note: the model is registered as a Pythinker alias even "
                    "if LM Studio can't currently load it — you don't need to "
                    "re-run [bold]/login --lm-studio[/bold].[/dim]"
                )
            elif _is_lm_studio_jinja_template_error(e):
                console.print(
                    f"[{_t.error}]LM Studio failed to render this model's prompt template.[/]\n"
                    "[dim]This is a model-side bug (broken or version-mismatched "
                    "Jinja template baked into the GGUF), not a Pythinker issue.[/dim]\n"
                    "[dim]To fix:[/dim]\n"
                    "[dim]  1. Easiest: switch to a different model with "
                    "[bold]/model[/bold] (most well-known models work out of the box).[/dim]\n"
                    "[dim]  2. Re-download the model from the [bold]lmstudio-community[/bold] "
                    "namespace in LM Studio's model browser — those have audited templates.[/dim]\n"
                    "[dim]  3. Or override the template manually in LM Studio: "
                    "[bold]My Models → model settings → Prompt Template[/bold].[/dim]\n"
                    f"[dim]Server: {escape(str(e))}[/dim]"
                )
            else:
                console.print(f"[{_t.error}]LLM provider error: {escape(str(e))}[/]")
            if not isinstance(e, APIStatusError) or e.status_code not in (401, 402, 403, 429):
                console.print(
                    "[dim]If this persists, run [bold]pythinker export[/bold] and send the "
                    "exported data to support for assistance. "
                    "Please do not share the exported file publicly.[/dim]"
                )
        except MaxStepsReached as e:
            _t = _get_tui_tokens()
            logger.warning("Max steps reached: {n_steps}", n_steps=e.n_steps)
            console.print(
                f"[{_t.warning}]{e}[/]\n"
                "[dim]Send another message to continue where it left off.[/dim]"
            )
            # Graceful handoff: a tools-disabled summary of progress / next steps so
            # the human resuming doesn't have to reconstruct state (best-effort).
            if isinstance(self.soul, PythinkerSoul):
                from pythinker_code.soul.btw import generate_max_steps_handoff

                try:
                    handoff = await generate_max_steps_handoff(self.soul)
                except Exception:
                    logger.warning("Max-steps handoff failed", exc_info=True)
                    handoff = None
                if handoff:
                    console.print(f"\n[{_t.muted}]── handoff ──[/]\n{escape(handoff)}")
        except RunCancelled:
            logger.info("Cancelled by user")
            from pythinker_code.telemetry import track

            _at_step = (
                getattr(self.soul, "_current_step_no", 0)
                if isinstance(self.soul, PythinkerSoul)
                else 0
            )
            track("turn_interrupted", at_step=_at_step)
            console.print(f"[{_get_tui_tokens().error}]Interrupted by user[/]")
            # ESC must stop everything the interrupted turn started — without
            # this, background subagents spawned during the turn keep running
            # and re-deliver the abandoned task via completion notifications.
            if isinstance(self.soul, PythinkerSoul):
                try:
                    killed = self.soul.runtime.background_tasks.kill_turn_tasks(
                        reason="Interrupted by user"
                    )
                except Exception:
                    logger.exception("Failed to kill background tasks on interrupt")
                    killed = []
                if killed:
                    console.print(
                        f"[{_get_tui_tokens().muted}]Stopped {len(killed)} background "
                        f"task{'s' if len(killed) != 1 else ''} started this turn[/]"
                    )
        except Exception as e:
            _t = _get_tui_tokens()
            logger.exception("Unexpected error:")
            console.print(
                f"[{_t.error}]Unexpected error: {e}[/]\n"
                "[dim]Run [bold]pythinker export[/bold] and send the exported data to support "
                "for assistance. Please do not share the exported file publicly.[/dim]"
            )
            raise  # re-raise unknown error
        finally:
            # Clean up btw modal if it's still attached (exception skipped wait_for_btw_dismiss)
            if captured_view is not None:
                captured_view._dismiss_btw()  # pyright: ignore[reportPrivateUsage]
            # Warn about queued messages lost due to error/cancel.
            # Check both: pending (already drained from view) and view (not yet drained).
            all_lost: list[UserInput] = list(pending)
            pending.clear()
            if captured_view is not None:
                all_lost.extend(captured_view.drain_queued_messages())
            for msg in all_lost:
                console.print(
                    f"[{_get_tui_tokens().warning}]Queued message dropped: {msg.command}[/]"
                )
            self._maybe_present_pending_approvals()
            remove_sigint()
        return False

    @staticmethod
    def _should_defer_background_auto_trigger(
        prompt_session: _BackgroundAutoTriggerPromptState | None,
    ) -> bool:
        if prompt_session is None:
            return False
        return prompt_session.has_pending_input() or prompt_session.had_recent_input_activity(
            within_s=_BG_AUTO_TRIGGER_INPUT_GRACE_S
        )

    @staticmethod
    def _background_auto_trigger_timeout_s(
        prompt_session: _BackgroundAutoTriggerPromptState | None,
    ) -> float | None:
        if prompt_session is None or prompt_session.has_pending_input():
            return None
        remaining = prompt_session.recent_input_activity_remaining(
            within_s=_BG_AUTO_TRIGGER_INPUT_GRACE_S
        )
        return remaining if remaining > 0 else None

    async def _wait_for_input_or_activity(
        self,
        prompt_session: _BackgroundAutoTriggerPromptState,
        idle_events: asyncio.Queue[_PromptEvent],
        *,
        timeout_s: float | None = None,
    ) -> _PromptEvent:
        idle_task = asyncio.create_task(idle_events.get())
        activity_task = asyncio.create_task(prompt_session.wait_for_input_activity())
        timeout_task = (
            asyncio.create_task(asyncio.sleep(timeout_s)) if timeout_s is not None else None
        )
        done: set[asyncio.Task[Any]] = set()
        try:
            done, _ = await asyncio.wait(
                [task for task in (idle_task, activity_task, timeout_task) if task is not None],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (idle_task, activity_task, timeout_task):
                if task is None:
                    continue
                if task.done():
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if idle_task in done:
            return idle_task.result()
        return _PromptEvent(kind="input_activity")

    async def _watch_root_wire_hub(self) -> None:
        if not isinstance(self.soul, PythinkerSoul):
            return
        if self.soul.runtime.root_wire_hub is None:
            return
        queue = self.soul.runtime.root_wire_hub.subscribe()
        try:
            while True:
                try:
                    msg = await queue.get()
                except QueueShutDown:
                    return
                try:
                    await self._handle_root_hub_message(msg)
                except Exception:
                    logger.exception("Failed to handle root hub message:")
        finally:
            self.soul.runtime.root_wire_hub.unsubscribe(queue)

    async def _handle_root_hub_message(self, msg: WireMessage) -> None:
        if not isinstance(self.soul, PythinkerSoul):
            return
        match msg:
            case ApprovalRequest() as request:
                request = self._enrich_approval_request_for_ui(request)
                if self.soul.runtime.approval_runtime is None:
                    return
                record = self.soul.runtime.approval_runtime.get_request(request.id)
                if record is None or record.status != "pending":
                    return
                if self._prompt_session is not None:
                    # Interactive mode: queue and present via modal
                    self._queue_approval_request(request)
                    self._maybe_present_pending_approvals()
                    self._prompt_session.invalidate()
                elif self._active_approval_sink is not None:
                    # Non-interactive with live view: forward to sink
                    self._forward_approval_to_sink(request)
                else:
                    # Queue for later
                    self._queue_approval_request(request)
            case ApprovalResponse() as response:
                # External resolution (e.g. from web UI)
                if (
                    self._approval_modal is not None
                    and self._approval_modal.request.id == response.request_id
                ):
                    if not self._approval_modal.request.resolved:
                        self._approval_modal.request.resolve(response.response)
                    self._clear_current_prompt_approval_request(response.request_id)
                    self._activate_prompt_approval_modal()
                self._remove_pending_approval_request(response.request_id)
                self._maybe_present_pending_approvals()
                if self._prompt_session is not None:
                    self._prompt_session.invalidate()
            case _:
                return

    def _enrich_approval_request_for_ui(self, request: ApprovalRequest) -> ApprovalRequest:
        if not isinstance(self.soul, PythinkerSoul):
            return request
        if request.agent_id is None:
            return request
        if self.soul.runtime.subagent_store is None:
            return request
        record = self.soul.runtime.subagent_store.get_instance(request.agent_id)
        if record is None:
            return request
        return request.model_copy(update={"source_description": record.description})

    async def _run_btw_modal(
        self,
        question: str,
        prompt_session: CustomPromptSession,
    ) -> None:
        """Run /btw using the prompt session's modal system.

        Attaches a ``_BtwModalDelegate`` that replaces the input line with
        the btw panel.  A refresh loop animates the spinner.  After the LLM
        responds, we start a new prompt read so prompt_toolkit can render the
        result and accept dismiss keys.
        """
        from pythinker_code.soul.btw import execute_side_question
        from pythinker_code.ui.shell.visualize import (
            _BtwModalDelegate,  # pyright: ignore[reportPrivateUsage]
        )

        assert isinstance(self.soul, PythinkerSoul)

        dismiss_event = asyncio.Event()
        modal = _BtwModalDelegate(on_dismiss=lambda: dismiss_event.set())
        import time

        modal._question = question  # pyright: ignore[reportPrivateUsage]
        modal.set_start_time(time.monotonic())
        prompt_session.attach_modal(modal)

        # Refresh loop for spinner animation
        async def _refresh() -> None:
            try:
                while True:
                    await asyncio.sleep(0.08)
                    prompt_session.invalidate()
            except asyncio.CancelledError:
                pass

        refresh_task = asyncio.create_task(_refresh())
        prompt_task: asyncio.Task[None] | None = None
        llm_task: asyncio.Task[tuple[str | None, str | None]] | None = None

        try:

            def _on_chunk(chunk: str) -> None:
                modal.append_text(chunk)

            # Start a prompt read concurrently — renders the modal and
            # handles key events while the LLM call runs in parallel.
            async def _wait_for_dismiss() -> None:
                while not dismiss_event.is_set():
                    try:
                        await prompt_session.prompt_next()
                    except (KeyboardInterrupt, EOFError):
                        dismiss_event.set()
                        break

            prompt_task = asyncio.create_task(_wait_for_dismiss())

            # Run LLM call as a separate task so Escape can cancel it
            llm_task = asyncio.create_task(
                execute_side_question(self.soul, question, on_text_chunk=_on_chunk)
            )

            # Wait for either LLM completion or user dismiss
            dismiss_task = asyncio.create_task(dismiss_event.wait())
            _done, _ = await asyncio.wait(
                [llm_task, dismiss_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            if llm_task.done() and not llm_task.cancelled():
                # LLM finished — show result, wait for user to dismiss
                dismiss_task.cancel()
                response, error = llm_task.result()
                modal.set_result(response, error)
                prompt_session.invalidate()
                await dismiss_event.wait()
            else:
                # User dismissed during loading — cancel the LLM call
                llm_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await llm_task
        finally:
            # Cancel ALL child tasks
            if llm_task is not None and not llm_task.done():
                llm_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await llm_task
            if prompt_task is not None:
                prompt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await prompt_task
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task
            prompt_session.detach_modal(modal)

    def _make_btw_runner(self):
        """Create a btw_runner callback bound to the current soul."""
        if not isinstance(self.soul, PythinkerSoul):
            return None

        soul = self.soul

        async def _runner(
            question: str,
            on_text_chunk: Callable[[str], None] | None = None,
        ) -> tuple[str | None, str | None]:
            from pythinker_code.soul.btw import execute_side_question

            return await execute_side_question(soul, question, on_text_chunk)

        return _runner

    def _set_active_view(self, view: Any) -> None:
        self._active_approval_sink = view
        self._active_view = view
        # In interactive mode, approvals are handled by the prompt modal,
        # not by the live view sink. Don't flush to avoid losing requests.
        if self._prompt_session is not None:
            return
        # Flush pending approvals to the newly active sink
        while self._pending_approval_requests:
            request = self._pending_approval_requests.popleft()

            if (
                not isinstance(self.soul, PythinkerSoul)
                or self.soul.runtime.approval_runtime is None
            ):
                break
            record = self.soul.runtime.approval_runtime.get_request(request.id)
            if record is None or record.status != "pending":
                continue
            self._forward_approval_to_sink(request)

    def _clear_active_view(self) -> None:
        self._active_approval_sink = None
        self._active_view = None
        # Re-queue any approval requests that were forwarded to the sink
        # but not yet resolved.  Without this, those requests would be
        # silently lost when the live view closes between turns.
        if not isinstance(self.soul, PythinkerSoul) or self.soul.runtime.approval_runtime is None:
            return
        for record in self.soul.runtime.approval_runtime.list_pending():
            self._queue_approval_request(
                self._enrich_approval_request_for_ui(
                    ApprovalRequest(
                        id=record.id,
                        tool_call_id=record.tool_call_id,
                        sender=record.sender,
                        action=record.action,
                        description=record.description,
                        display=record.display,
                        source_kind=record.source.kind,
                        source_id=record.source.id,
                        agent_id=record.source.agent_id,
                        subagent_type=record.source.subagent_type,
                    )
                )
            )

    def _forward_approval_to_sink(self, request: ApprovalRequest) -> None:
        """Forward an approval request to the active live view sink and bridge the response."""
        if self._active_approval_sink is None:
            self._queue_approval_request(request)
            return
        self._active_approval_sink.enqueue_external_message(request)

        async def _bridge() -> None:
            try:
                response = await request.wait()
                if (
                    isinstance(self.soul, PythinkerSoul)
                    and self.soul.runtime.approval_runtime is not None
                ):
                    self.soul.runtime.approval_runtime.resolve(
                        request.id, response, feedback=request.feedback
                    )
            finally:
                if self._prompt_session is not None:
                    self._prompt_session.invalidate()

        self._start_background_task(_bridge())

    def _queue_approval_request(self, request: ApprovalRequest) -> None:
        if self._approval_modal is not None and self._approval_modal.request.id == request.id:
            return
        if (
            self._current_prompt_approval_request is not None
            and self._current_prompt_approval_request.id == request.id
        ):
            return
        if any(r.id == request.id for r in self._pending_approval_requests):
            return
        self._pending_approval_requests.append(request)

    def _remove_pending_approval_request(self, request_id: str) -> None:
        self._clear_current_prompt_approval_request(request_id)
        self._pending_approval_requests = deque(
            r for r in self._pending_approval_requests if r.id != request_id
        )

    def _clear_current_prompt_approval_request(self, request_id: str) -> None:
        if (
            self._current_prompt_approval_request is not None
            and self._current_prompt_approval_request.id == request_id
        ):
            self._current_prompt_approval_request = None

    def _maybe_present_pending_approvals(self) -> None:
        if self._prompt_session is not None:
            self._activate_prompt_approval_modal()
            return
        if self._active_approval_sink is not None:
            while self._pending_approval_requests:
                request = self._pending_approval_requests.popleft()

                if not isinstance(self.soul, PythinkerSoul):
                    break
                if self.soul.runtime.approval_runtime is None:
                    break
                record = self.soul.runtime.approval_runtime.get_request(request.id)
                if record is None or record.status != "pending":
                    continue
                self._forward_approval_to_sink(request)

    def _get_default_buffer_text_and_cursor(self) -> tuple[str, int]:
        if self._prompt_session is None:
            return "", 0
        buf = self._prompt_session._session.default_buffer  # pyright: ignore[reportPrivateUsage]
        return buf.text, buf.cursor_position

    def _activate_prompt_approval_modal(self) -> None:
        if self._prompt_session is None:
            return
        current_request = self._current_prompt_approval_request
        if current_request is None:
            current_request = self._pop_next_pending_approval_request()
            self._current_prompt_approval_request = current_request
        if current_request is None:
            if self._approval_modal is not None:
                self._prompt_session.detach_modal(self._approval_modal)
                self._approval_modal = None
            return
        if self._approval_modal is None:
            self._approval_modal = ApprovalPromptDelegate(
                current_request,
                on_response=self._handle_prompt_approval_response,
                buffer_state_provider=self._get_default_buffer_text_and_cursor,
                text_expander=self._prompt_session._get_placeholder_manager().serialize_for_history,  # pyright: ignore[reportPrivateUsage]
            )
            self._prompt_session.attach_modal(self._approval_modal)
        else:
            if self._approval_modal.request.id != current_request.id:
                self._approval_modal.set_request(current_request)
        self._prompt_session.invalidate()

    def _handle_prompt_approval_response(
        self,
        request: ApprovalRequest,
        response: ApprovalResponse.Kind,
        feedback: str = "",
    ) -> None:
        if not isinstance(self.soul, PythinkerSoul):
            return
        if self.soul.runtime.approval_runtime is None:
            return
        self.soul.runtime.approval_runtime.resolve(request.id, response, feedback=feedback)
        self._clear_current_prompt_approval_request(request.id)
        self._activate_prompt_approval_modal()

    def _pop_next_pending_approval_request(self) -> ApprovalRequest | None:
        if not isinstance(self.soul, PythinkerSoul) or self.soul.runtime.approval_runtime is None:
            return None
        while self._pending_approval_requests:
            request = self._pending_approval_requests.popleft()

            record = self.soul.runtime.approval_runtime.get_request(request.id)
            if record is None or record.status != "pending":
                continue
            return request
        return None

    async def _auto_update(self) -> None:
        # Background-refresh the cached latest version (throttled); never blocks startup.
        await refresh_update_cache_if_due()
        # The persistent under-input line renders the cached update hint; refresh
        # it when the cache changes instead of duplicating the text as a toast.
        if pending_update_notice():
            self._refresh_update_notice_line()

    async def _silent_auto_update(self) -> None:
        """Install a newer release silently in the background at startup."""
        if not _should_auto_check_for_updates():
            return

        result = await self._run_silent_update_job()
        # Throttle only after a completed round-trip. Marking before the network
        # call (or after a FAILED one) would suppress updates for the whole
        # interval on a transient startup blip — mirrors _refresh_update_cache.
        if result is not None and result is not UpdateResult.FAILED:
            _mark_auto_update_check_attempt()
        if result is UpdateResult.UPDATED:
            self._surface_installed_update_notice()
        elif result is UpdateResult.UPDATE_AVAILABLE:
            self._surface_managed_channel_notice()
        # FAILED / UP_TO_DATE / UNSUPPORTED / None → silent (recorded in the job log).

    async def _run_silent_update_job(self) -> UpdateResult | None:
        try:
            return await run_update_job(print_output=False, check_only=False, source="startup-auto")
        except SystemExit:
            raise
        except Exception:
            # Boundary-only recovery: update failure must not abort the shell,
            # and run_update_job has already persisted status/log details.
            logger.exception("Silent auto-update failed:")
            return None

    def _surface_installed_update_notice(self) -> None:
        if self._installed_update_smoke_check_failed():
            self._update_toast(
                "Update installed but verification failed; see update.log.",
                style="fg:ansiyellow",
            )
            return
        # The persistent under-input line (_append_update_notice) already renders
        # the restart message; a toast duplicates it on the footer's second row.
        self._refresh_update_notice_line()

    def _refresh_update_notice_line(self) -> None:
        """Drop the update-notice memo and repaint so the footer picks up new text."""
        self._update_notice_cache = (0.0, None)
        if self._prompt_session is not None:
            self._prompt_session.invalidate()

    def _installed_update_smoke_check_failed(self) -> bool:
        status = read_update_status()
        message = status.message if status else None
        return bool(message and message.startswith(SMOKE_CHECK_FAILED_PREFIX))

    def _installed_update_restart_notice(self) -> str:
        from pythinker_code.constant import VERSION as current_version

        status = read_update_status()
        new_version = (
            (status.target_version if status else None)
            or welcome_update_target()
            or "the latest version"
        )
        return f"Updated {current_version} → {new_version}. Restart Pythinker to apply."

    def _surface_managed_channel_notice(self) -> None:
        notice = self._managed_channel_notice() or pending_update_notice()
        if notice:
            self._update_toast(notice, style="fg:ansibrightyellow bold")

    def _managed_channel_notice(self) -> str | None:
        from pythinker_code.constant import VERSION as current_version

        # Managed-channel fast-path: skip the welcome lookup for non-managed
        # installs; format_managed_channel_notice re-validates the marker.
        if _detect_upgrade_command()[:1] != [MANAGED_CHANNEL_MARKER]:
            return None
        latest = welcome_update_target()
        if latest is None:
            return None
        return format_managed_channel_notice(current_version, latest)

    def _update_toast(self, notice: str, *, style: str) -> None:
        toast(notice, topic="update", duration=30.0, immediate=True, style=style)
        if self._prompt_session is not None:
            self._prompt_session.invalidate()

    def _update_notice_text(self) -> str | None:
        """Persistent under-input update line, or None when up to date.

        Memoized on a short TTL (5s) so the hot toolbar render path doesn't stat
        the update cache on every repaint; the background updater invalidates the
        prompt when it changes the cache, so the line still appears promptly.
        """
        now = time.monotonic()
        cached_at, cached = self._update_notice_cache
        if now - cached_at < 5.0:
            return cached
        text = self._compute_update_notice()
        self._update_notice_cache = (now, text)
        return text

    def _compute_update_notice(self) -> str | None:
        target = welcome_update_target()
        if not target:
            return None
        # A release already installed this session needs a restart, not /update —
        # surface that here instead of telling the user to re-run an update that
        # has already landed.
        status = read_update_status()
        installed = (
            status is not None
            and status.state is UpdateJobState.UPDATED
            and status.target_version == target
        )
        if installed and not self._installed_update_smoke_check_failed():
            text = self._installed_update_restart_notice()
        else:
            text = f"↑ Update available — v{target} · /update"
        if ascii_glyphs_enabled():
            text = text.translate(_WELCOME_ASCII_FALLBACKS)
        return text

    def _schedule_startup_update_task(self) -> None:
        """Pick the startup update behavior and schedule it (non-blocking).

        - env kill-switch set → nothing (cache filters already suppress the
          notice, matching today's hard-disable behavior).
        - enabled → silent background install.
        - config-disabled OR source checkout → refresh the persistent notice
          only (`_auto_update`); self-suppresses for source checkouts because
          `pending_update_notice()` returns None in that path.
        - non-PythinkerSoul → same notice-refresh path (no runtime config to
          consult), matching the prior unconditional `_auto_update` behavior.
        """
        if get_env_bool("PYTHINKER_CLI_NO_AUTO_UPDATE"):
            logger.info("Auto-update disabled by PYTHINKER_CLI_NO_AUTO_UPDATE environment variable")
            return
        if isinstance(self.soul, PythinkerSoul) and auto_update_enabled(self.soul.runtime.config):
            self._start_background_task(self._silent_auto_update())
        else:
            self._start_background_task(self._auto_update())

    def _start_background_task(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _cleanup(t: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(t)
            try:
                t.result()
            except asyncio.CancelledError:
                pass
            except SystemExit:
                # The silent updater's Windows native/pip path raises SystemExit
                # so the installer can replace the binary; don't crash the shell.
                logger.info("Background task requested process exit (update installer launched).")
            except Exception:
                logger.exception("Background task failed:")

        task.add_done_callback(_cleanup)
        return task

    def _cancel_background_tasks(self) -> None:
        """Cancel all background tasks (notification watcher, auto-update, etc.)."""
        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()


# Fixed brand palette — theme-independent robot mark colors (see ui/theme/palettes.py).
_LOGO_NAVY = BRAND[BrandToken.NAVY]
_LOGO_FACE = BRAND[BrandToken.FACE]
_LOGO_CORAL = BRAND[BrandToken.CORAL]
_LOGO_CORAL_LIT = BRAND[BrandToken.CORAL_LIT]
_LOGO_IRIS = BRAND[BrandToken.IRIS]

# Head-only robot mark (antenna, ears, eyes, mouth). Only rendered when
# ascii_glyphs_enabled() is false; ASCII terminals get the text-only banner
# instead of a garbled silhouette. ``{antenna_style}`` is filled per render so
# the antenna ball can carry the terminal's slow-blink attribute.
_LOGO_TEMPLATE = (
    "      [{antenna_style}]●[/]\n"
    f"      [{_LOGO_NAVY}]│[/]\n"
    f"  [{_LOGO_NAVY}]▛[/][{_LOGO_FACE}]▀▀▀▀▀▀▀[/][{_LOGO_NAVY}]▜[/]\n"
    f" [{_LOGO_CORAL}]◖[/][{_LOGO_NAVY}]█[/][{_LOGO_FACE}] [/]"
    f"[{_LOGO_IRIS}]◉[/][{_LOGO_FACE}]   [/][{_LOGO_IRIS}]◉[/]"
    f"[{_LOGO_FACE}] [/][{_LOGO_NAVY}]█[/][{_LOGO_CORAL}]◗[/]\n"
    f"  [{_LOGO_NAVY}]▙▄▄▄[/][{_LOGO_FACE}]≡[/][{_LOGO_NAVY}]▄▄▄▟[/]"
)


def _logo_text() -> Text:
    """Robot mark with a glowing antenna ball.

    The ball renders steady; the boot animation (`_blink_antenna`) blinks it a
    fixed number of times after the welcome banner prints, instead of the old
    indefinite SGR slow-blink. Reduced motion pins the ball muted.
    """
    antenna_style = _LOGO_CORAL if motion_disabled() else f"bold {_LOGO_CORAL_LIT}"
    return Text.from_markup(_LOGO_TEMPLATE.format(antenna_style=antenna_style))


# Boot animation: blink the antenna ball this many times once the agent has
# loaded and the welcome banner is on screen, then pin it steady.
_ANTENNA_BLINKS = 7
_ANTENNA_BLINK_OFF_SECONDS = 0.07
_ANTENNA_BLINK_ON_SECONDS = 0.09
_ANTENNA_GLYPH = "●"


def _antenna_cell(panel: Panel, panel_width: int) -> tuple[int, int] | None:
    """Locate the antenna ball in the rendered panel.

    Returns ``(rows_above_cursor, column)`` valid immediately after the panel
    prints (cursor sits on the line below it), or None when no antenna is
    rendered. Scans top-down so the first ● found is the antenna, never a
    same-glyph chip in the panel subtitle.
    """
    options = console.options.update_width(panel_width)
    lines = console.render_lines(panel, options, pad=False)
    for row, segments in enumerate(lines):
        column = 0
        for segment in segments:
            found = segment.text.find(_ANTENNA_GLYPH)
            if found != -1:
                return len(lines) - row, column + cell_len(segment.text[:found])
            column += cell_len(segment.text)
    return None


def _blink_antenna(rows_up: int, column: int) -> None:
    """Blink the antenna ball ``_ANTENNA_BLINKS`` times, then pin it steady.

    Deliberately synchronous: nothing else writes to the terminal while it
    runs, so the cursor-relative addressing stays valid. Runs only on the
    startup path, bounded to ~1.1s total.
    """
    states: list[tuple[Text, float]] = []
    for _ in range(_ANTENNA_BLINKS):
        states.append((Text(_ANTENNA_GLYPH, style=_LOGO_NAVY), _ANTENNA_BLINK_OFF_SECONDS))
        states.append(
            (Text(_ANTENNA_GLYPH, style=f"bold {_LOGO_CORAL_LIT}"), _ANTENNA_BLINK_ON_SECONDS)
        )
    states.append((Text(_ANTENNA_GLYPH, style=f"bold {_LOGO_CORAL_LIT}"), 0.0))
    try:
        console.control(Control.show_cursor(False))
        for glyph, delay in states:
            console.control(Control.move(y=-rows_up), Control.move_to_column(column))
            console.print(glyph, end="")
            console.control(Control.move(y=rows_up), Control.move_to_column(0))
            if delay:
                time.sleep(delay)
    finally:
        console.control(Control.show_cursor(True))


# 1:1 ASCII stand-ins for every decorative glyph the welcome banner emits
# (mirrors the server-banner fallback in utils/server.py). Welcome copy and
# chips pass through this when ascii_glyphs_enabled() is true so legacy code
# pages never see a raw Unicode glyph in the startup path.
_WELCOME_ASCII_FALLBACKS = str.maketrans(
    {
        "✦": "*",
        "↑": "^",
        "•": "*",
        "·": "-",
        "—": "-",
        "…": "~",
        "─": "-",
    }
)


@dataclass(slots=True)
class WelcomeInfoItem:
    class Level(Enum):
        INFO = "grey50"
        WARN = _LOGO_CORAL  # muted coral, matching the robot's antenna accent
        ERROR = "red"

    name: str
    value: str
    level: Level = Level.INFO


def _value_style_for_label(label: str, level: WelcomeInfoItem.Level) -> str:
    """INFO-level styling per label; WARN/ERROR colors always win."""
    if level is not WelcomeInfoItem.Level.INFO:
        return level.value
    from pythinker_code.ui.theme import get_tui_tokens

    tokens = get_tui_tokens()
    label = label.strip()
    if label == "Directory":
        return tokens.info or "#AFE3F1"
    if label == "Session":
        return tokens.dim or "grey39"
    if label == "Model":
        return tokens.warning or "#EAB85F"
    if label == "Branch":
        return tokens.thinking_text or "grey70"
    if label == "Auto-save":
        return tokens.muted or "grey50"
    return level.value


def _welcome_banner_chip() -> Text | None:
    """One-line chip for the top-right of the welcome banner, or None.

    Precedence: update-restart > update-available > what's-new > nothing.
    Mirrors the under-input ``_compute_update_notice`` precedence so the
    banner and footer never disagree after a successful /update.
    ``consume_whats_new`` is always called first so the 'last seen' mark is
    written regardless of which chip wins the display.
    """
    _t = _get_tui_tokens()
    whats_new_version = consume_whats_new()
    update_target = welcome_update_target()

    def _chip(markup: str, style: str) -> Text:
        if ascii_glyphs_enabled():
            markup = markup.translate(_WELCOME_ASCII_FALLBACKS)
        chip = Text.from_markup(markup)
        chip.highlight_regex(r"/[A-Za-z][A-Za-z0-9_-]*", style)
        return chip

    if update_target:
        # ponytail: mirror _compute_update_notice — if /update already landed
        # this session, show the restart line instead of "Update available".
        status = read_update_status()
        installed = (
            status is not None
            and status.state is UpdateJobState.UPDATED
            and status.target_version == update_target
            and not (status.message and status.message.startswith(SMOKE_CHECK_FAILED_PREFIX))
        )
        if installed:
            from pythinker_code.constant import VERSION as current_version

            return _chip(
                f"[{_t.info}]↻ Updated {current_version} → v{update_target}. Restart to apply.[/]",
                _t.info,
            )
        return _chip(
            f"[{_t.warning}]↑ Update available — v{update_target} · /update[/]", _t.warning
        )

    if whats_new_version:
        return _chip(f"[{_t.info}]✦ What's new in v{whats_new_version} · /changelog[/]", _t.info)

    return None


_WELCOME_LABEL_WIDTH = 10
_WELCOME_PANEL_CHROME_WIDTH = 6  # border + horizontal padding used below
# Content cells needed before tips move into a divided right-hand column
# (welcome copy + robot on the left, tips on the right, facts full-width
# below). Narrower terminals stack the same blocks vertically instead.
_WELCOME_COLUMNS_MIN_WIDTH = 84
# Two-column proportions: the left column stays narrow — wide enough
# for the strapline (52 cells), growing up to the max to fit fact rows —
# and tips absorb the remaining width.
_WELCOME_LEFT_COLUMN_WIDTH = 52
_WELCOME_LEFT_COLUMN_MAX_WIDTH = 64
_WELCOME_TIPS_MIN_WIDTH = 24
_WELCOME_COLUMNS_CHROME_WIDTH = 3  # divider + one pad cell each side


def _take_cells_left(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    out: list[str] = []
    used = 0
    for char in text:
        width = cell_width(char)
        if used + width > max_width:
            break
        out.append(char)
        used += width
    return "".join(out)


def _take_cells_right(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    out: list[str] = []
    used = 0
    for char in reversed(text):
        width = cell_width(char)
        if used + width > max_width:
            break
        out.append(char)
        used += width
    return "".join(reversed(out))


def _truncate_middle_to_width(text: str, max_width: int, *, ellipsis: str = "…") -> str:
    """Cell-aware middle truncation for paths and UUID-like values."""
    if max_width <= 0:
        return ""
    cleaned = sanitize_ansi(text).replace("\r", " ").replace("\n", " ")
    if cell_width(cleaned) <= max_width:
        return cleaned
    if max_width <= 1:
        return truncate_to_width(cleaned, max_width, ellipsis=ellipsis)
    left_width = max(1, (max_width - 1) // 2)
    right_width = max(1, max_width - 1 - left_width)
    return (
        f"{_take_cells_left(cleaned, left_width)}{ellipsis}"
        f"{_take_cells_right(cleaned, right_width)}"
    )


def _welcome_panel_width() -> int:
    # Span the full terminal, matching the prompt/input rules below the
    # banner; re-queried at print time so every tab/terminal gets its own fit.
    return max(1, current_console_width(console, default=80))


def _welcome_value(label: str, value: str, max_width: int, *, ellipsis: str = "…") -> str:
    cleaned = sanitize_ansi(value).replace("\r", " ").replace("\n", " ")
    if label.strip() in {"Directory", "Auto-save", "Session"}:
        return _truncate_middle_to_width(cleaned, max_width, ellipsis=ellipsis)
    return truncate_to_width(cleaned, max_width, ellipsis=ellipsis)


def _welcome_tip_lines(value: str, max_width: int, *, ellipsis: str = "…") -> list[str]:
    cleaned = sanitize_ansi(value).replace("\r", " ").replace("\n", " ").strip()
    if not cleaned:
        return [""]
    lines = textwrap.wrap(
        cleaned,
        width=max(1, max_width),
        break_long_words=False,
        break_on_hyphens=False,
    ) or [cleaned]
    return [truncate_to_width(line, max_width, ellipsis=ellipsis) for line in lines]


def _print_welcome_info(
    name: str, info_items: list[WelcomeInfoItem], *, banner: Text | None = None
) -> None:
    """Print the welcome banner once; it must never block the prompt."""
    _t = _get_tui_tokens()
    ascii_mode = ascii_glyphs_enabled()
    ellipsis = "~" if ascii_mode else "…"
    panel_box = box.ASCII if ascii_mode else box.ROUNDED
    panel_width = _welcome_panel_width()
    content_width = max(1, panel_width - _WELCOME_PANEL_CHROME_WIDTH)

    def _copy(markup: str) -> Text:
        if ascii_mode:
            markup = markup.translate(_WELCOME_ASCII_FALLBACKS)
        return Text.from_markup(markup)

    head = _copy("[bold]Welcome to Pythinker — think first, then code.[/]")
    strapline = _copy(f"[{_t.muted}]Review · Secure · Diagnose · Build with confidence.[/]")
    help_text = _copy(f"[{_t.muted}]Type /help for commands.[/]")
    help_text.highlight_regex(r"/help\b", _LOGO_CORAL)

    if ascii_mode:
        # Caller-provided values (tips, notices) may carry the same decorative
        # glyphs as our own copy; degrade them through the same table.
        info_items = [
            WelcomeInfoItem(
                name=item.name,
                value=item.value.translate(_WELCOME_ASCII_FALLBACKS),
                level=item.level,
            )
            for item in info_items
        ]

    facts = [item for item in info_items if item.name.strip() != "Tip"]
    tips = [item for item in info_items if item.name.strip() == "Tip"]

    def _facts_grid(width: int) -> Table:
        label_width = min(_WELCOME_LABEL_WIDTH, max(4, width // 3))
        value_width = max(4, width - label_width - 2)
        info_table = Table.grid(padding=(0, 1))
        info_table.add_column(
            justify="right",
            style=tui_rich_style("muted"),
            no_wrap=True,
            width=label_width,
        )
        info_table.add_column(justify="left", no_wrap=True, width=value_width)
        for item in facts:
            value_style = _value_style_for_label(item.name, item.level)
            value = _welcome_value(item.name, item.value, value_width, ellipsis=ellipsis)
            info_table.add_row(item.name, Text(value, style=value_style, no_wrap=True))
        return info_table

    def _tips_block(width: int, *, with_rule: bool) -> Group:
        gutter = 2
        tip_width = max(4, width - gutter)
        parts: list[RenderableType] = [Text("Tips", style=tui_rich_style("muted"))]
        if with_rule:
            rule_char = "-" if ascii_mode else "─"
            parts.append(Text(rule_char * max(4, width), style=tui_rich_style("muted")))
        tips_table = Table.grid(padding=(0, 0))
        tips_table.add_column(style=tui_rich_style("muted"), no_wrap=True, width=gutter)
        tips_table.add_column(justify="left", no_wrap=True, width=tip_width)
        bullet = "* " if ascii_mode else "• "
        for item in tips:
            lines = _welcome_tip_lines(item.value, tip_width, ellipsis=ellipsis)
            for index, line in enumerate(lines):
                tip_text = Text(line, style=item.level.value, no_wrap=True)
                tip_text.highlight_regex(r"/[A-Za-z][A-Za-z0-9_-]*", _LOGO_CORAL)
                tips_table.add_row(bullet if index == 0 else "  ", tip_text)
        parts.append(tips_table)
        return Group(*parts)

    show_logo = not ascii_mode
    use_columns = bool(tips) and content_width >= _WELCOME_COLUMNS_MIN_WIDTH
    # The robot sits beside the copy when there's room (~68 cells); in a compact
    # terminal it stacks centered above the copy instead of being dropped, so the
    # mark stays visible at any width that can render its Unicode glyphs.
    logo_beside_copy = show_logo and not use_columns and content_width >= 68
    logo_rendered = show_logo  # columns-centered, beside, or stacked — only ASCII hides it

    version_title = Text.assemble(
        ("Pythinker Code", tui_rich_style("muted")),
        (f" v{get_version()}", tui_rich_style("dim")),
    )

    def _panel() -> Panel:
        rows: list[RenderableType] = []
        if use_columns:
            # Two-column split: welcome copy, the robot mark, and the fact
            # rows on the left; tips in a divided right-hand column. The left
            # column grows (within its cap) to fit the longest fact row so
            # paths don't truncate while the tips column has slack.
            wanted_left = _WELCOME_LEFT_COLUMN_WIDTH
            if facts:
                longest_fact = max(cell_width(item.value) for item in facts)
                wanted_left = max(
                    wanted_left,
                    min(_WELCOME_LEFT_COLUMN_MAX_WIDTH, longest_fact + _WELCOME_LABEL_WIDTH + 2),
                )
            left_width = max(
                _WELCOME_LEFT_COLUMN_WIDTH,
                min(
                    wanted_left,
                    content_width - _WELCOME_COLUMNS_CHROME_WIDTH - _WELCOME_TIPS_MIN_WIDTH,
                ),
            )
            tips_width = content_width - _WELCOME_COLUMNS_CHROME_WIDTH - left_width
            left_rows: list[RenderableType] = [head, strapline, help_text]
            if show_logo:
                left_rows.extend([Text(""), Align.center(_logo_text())])
            if facts:
                left_rows.extend([Text(""), _facts_grid(left_width)])
            columns = Table(
                box=panel_box,
                show_header=False,
                show_edge=False,
                show_lines=False,
                pad_edge=False,
                padding=(0, 1),
                border_style=tui_rich_style("border"),
                expand=False,
            )
            columns.add_column(width=left_width, justify="left", vertical="top")
            columns.add_column(width=tips_width, justify="left", vertical="top")
            columns.add_row(Group(*left_rows), _tips_block(tips_width, with_rule=True))
            rows.append(columns)
        else:
            if logo_beside_copy:
                # Logo on the left; the text block centers vertically against
                # the robot so the lines sit beside the face while the antenna
                # floats above.
                table = Table.grid(padding=(0, 1))
                table.add_column(justify="left", no_wrap=True)
                table.add_column(justify="left", vertical="middle", no_wrap=True)
                table.add_row(_logo_text(), Group(head, strapline, help_text))
                rows.append(table)
            elif show_logo:
                # Compact terminal: too narrow to seat the robot beside the
                # copy, so stack it centered above instead of dropping it.
                rows.extend([Align.center(_logo_text()), Text(""), head, strapline, help_text])
            else:
                rows.extend([head, strapline, help_text])

            if facts:
                rows.append(Text(""))  # empty line
                rows.append(_facts_grid(content_width))

            if tips:
                rows.append(Text(""))  # empty line
                rows.append(_tips_block(content_width, with_rule=False))

        return Panel(
            Group(*rows),
            title=version_title,
            title_align="left",
            subtitle=banner,
            subtitle_align="right",
            border_style=tui_rich_style("border"),
            box=panel_box,
            expand=False,
            width=panel_width,
            padding=(1, 2),
        )

    panel = _panel()
    console.print(panel)

    # Boot animation: blink the antenna 7 times, then stop. Only when the
    # Unicode logo actually rendered, on a real terminal tall enough that the
    # antenna row is still on screen, and never under reduced motion.
    if logo_rendered and console.is_terminal and not console.record and not motion_disabled():
        cell = _antenna_cell(panel, panel_width)
        if cell is not None:
            rows_up, column = cell
            if rows_up < console.size.height:
                _blink_antenna(rows_up, column)
