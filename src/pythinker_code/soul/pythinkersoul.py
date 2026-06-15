from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import pythinker_core
import tenacity
from pythinker_core import StepResult
from pythinker_core.chat_provider import (
    APIConnectionError,
    APIEmptyResponseError,
    APIStatusError,
    APITimeoutError,
    RetryableChatProvider,
    ThinkingEffort,
    TokenUsage,
)
from pythinker_core.message import Message, ToolCall
from pythinker_core.tooling.error import ToolRuntimeError
from tenacity import RetryCallState, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from pythinker_code.approval_runtime import (
    ApprovalSource,
    get_current_approval_source_or_none,
    reset_current_approval_source,
    set_current_approval_source,
)
from pythinker_code.background import build_active_task_snapshot
from pythinker_code.hooks.engine import HookEngine
from pythinker_code.hooks.runner import HookResult
from pythinker_code.llm import ModelCapability, create_llm
from pythinker_code.notifications import (
    NotificationView,
    build_notification_message,
    extract_notification_ids,
)
from pythinker_code.prompt_templates import PromptTemplate, expand_prompt_template
from pythinker_code.prompts import BUDGET_CONTINUATION_NUDGE
from pythinker_code.skill import Skill, read_skill_text_with_local_specialization
from pythinker_code.soul import (
    LLMNotSet,
    LLMNotSupported,
    MaxStepsReached,
    Soul,
    StatusSnapshot,
    wire_send,
)
from pythinker_code.soul.agent import (
    Agent,
    BuiltinSystemPromptArgs,
    Runtime,
    render_agents_md_reminder,
)

# classify_api_error is re-exported so telemetry tests and existing imports
# keep resolving against this module.
from pythinker_code.soul.api_errors import classify_api_error as classify_api_error
from pythinker_code.soul.approval import deliberation_scope
from pythinker_code.soul.compaction import (
    CompactionResult,
    SimpleCompaction,
    cap_stale_tool_result_bodies,
    estimate_text_tokens,
    prune_stale_tool_outputs,
    should_auto_compact,
    should_prune,
)
from pythinker_code.soul.compaction_restore import (
    build_compaction_restore_context,
    build_hook_context_message,
    compact_summary_text,
)
from pythinker_code.soul.context import Context
from pythinker_code.soul.dynamic_injection import (
    DynamicInjection,
    DynamicInjectionProvider,
    collect_within_budget,
    dynamic_to_candidate,
    injection_budget_from_runtime,
    normalize_history,
)
from pythinker_code.soul.dynamic_injections.agent_list import AgentListInjectionProvider
from pythinker_code.soul.dynamic_injections.auto_mode import AutoModeInjectionProvider
from pythinker_code.soul.dynamic_injections.goal_mode import GoalModeInjectionProvider
from pythinker_code.soul.dynamic_injections.inline_commands import InlineCommandReminderProvider
from pythinker_code.soul.dynamic_injections.model_defense import ModelDefenseInjectionProvider
from pythinker_code.soul.dynamic_injections.orchestration import OrchestrationInjectionProvider
from pythinker_code.soul.dynamic_injections.permissions_state import PermissionsInjectionProvider
from pythinker_code.soul.dynamic_injections.plan_mode import PlanModeInjectionProvider
from pythinker_code.soul.flow_runner import FLOW_COMMAND_PREFIX, FlowRunner
from pythinker_code.soul.message import (
    check_message,
    system,
    system_reminder,
    tool_result_to_message,
)
from pythinker_code.soul.permission import (
    permission_profile_for_runtime,
    reset_step_permission_profile,
    set_step_permission_profile,
)
from pythinker_code.soul.slash import registry as soul_slash_registry
from pythinker_code.soul.toolset import PythinkerToolset
from pythinker_code.subagents.usage import accumulate_usage, estimate_cost_usd
from pythinker_code.thinking import (
    available_thinking_levels,
    bool_to_thinking_effort,
    clamp_thinking_effort,
    next_thinking_level,
    thinking_effort_enabled,
)
from pythinker_code.tools.dmail import NAME as SendDMail_NAME
from pythinker_code.tools.utils import ToolRejectedError
from pythinker_code.utils.logging import logger
from pythinker_code.utils.slashcmd import SlashCommand, parse_slash_command_call
from pythinker_code.utils.sleep_inhibitor import SleepInhibitor
from pythinker_code.utils.trust import UntrustedData
from pythinker_code.wire.file import WireFile
from pythinker_code.wire.types import (
    CompactionBegin,
    CompactionEnd,
    ContentPart,
    ContextOverflowRecovered,
    MCPLoadingBegin,
    MCPLoadingEnd,
    QuestionItem,
    StatusUpdate,
    SteerInput,
    StepBegin,
    StepInterrupted,
    StepRetry,
    TextPart,
    ToolResult,
    TurnBegin,
    TurnEnd,
)

if TYPE_CHECKING:

    def type_check(soul: PythinkerSoul):
        _: Soul = soul


SKILL_COMMAND_PREFIX = "skill:"


def _safe_cwd(fallback: str) -> str:
    """Return the current working directory as a string.

    Falls back to *fallback* if the process CWD has been deleted (e.g. the
    project directory was removed mid-session by a shell command).
    """
    try:
        return str(Path.cwd())
    except FileNotFoundError:
        return str(fallback)


def classify_llm_system(chat_provider: object | None) -> str:
    """Classify a chat provider into a stable gen_ai.system telemetry value."""
    try:
        provider_class = type(chat_provider).__name__.lower() if chat_provider is not None else ""
        if "anthropic" in provider_class:
            return "anthropic"
        if "openai" in provider_class:
            return "openai"
        if "google" in provider_class or "gemini" in provider_class:
            return "google"
        return provider_class or "unknown"
    except Exception:
        return "unknown"


def _is_hard_usage_limit(exception: BaseException) -> bool:
    """Whether a 429 is a subscription usage cap (resets in hours) rather than a
    transient RPM/TPM burst (clears in seconds).

    Hard caps — e.g. ChatGPT ``usage_limit_reached`` — should NOT be retried:
    the backoff just delays the inevitable failure. Detected from the parsed body
    when present, else from the stringified message (the streaming 429 often
    carries only the bare text)."""
    body = getattr(exception, "body", None)
    if isinstance(body, dict):
        err = cast(dict[str, object], body).get("error")
        if isinstance(err, dict):
            err_type = cast(dict[str, object], err).get("type")
            if str(err_type or "") == "usage_limit_reached":
                return True
    text = str(exception).lower()
    return "usage_limit_reached" in text or "usage limit" in text


type StepStopReason = Literal["no_tool_calls", "tool_rejected", "stuck", "budget_exhausted"]


_MISSING_REQUIRED_FIELD_RE = re.compile(
    r"^\s*(?P<field>[A-Za-z_][A-Za-z0-9_\-.]*)\s*\n\s+Field required",
    re.MULTILINE,
)


def _missing_required_fields_from_validation_error(message: str) -> list[str]:
    """Extract field names from Pydantic's missing-field validation text."""
    if "Error validating JSON arguments:" not in message:
        return []
    return [match.group("field") for match in _MISSING_REQUIRED_FIELD_RE.finditer(message)]


def _is_empty_tool_arguments(tool_call: ToolCall) -> bool:
    raw = tool_call.function.arguments
    return raw is None or raw.strip() in {"", "{}"}


def _malformed_empty_tool_call_summary(
    tool_calls: Sequence[ToolCall], tool_results: Sequence[ToolResult]
) -> str | None:
    """Return a concise summary when a step only produced empty invalid tool calls.

    Some models can get stuck emitting `{}`/empty arguments for tools with required
    fields. Continuing the agent loop just floods the UI with repeated validation
    cards. Detect that dead-end shape and stop after the first invalid batch.
    """
    if not tool_calls or len(tool_calls) != len(tool_results):
        return None

    calls_by_id = {call.id: call for call in tool_calls}
    missing_by_tool: list[str] = []
    for result in tool_results:
        call = calls_by_id.get(result.tool_call_id)
        if call is None or not _is_empty_tool_arguments(call):
            return None
        if not result.return_value.is_error:
            return None
        fields = _missing_required_fields_from_validation_error(result.return_value.message)
        if not fields:
            return None
        missing_by_tool.append(f"{call.function.name}.{', '.join(fields)}")

    if not missing_by_tool:
        return None
    return "; ".join(missing_by_tool)


async def _settle_shielded(task: asyncio.Task[None]) -> None:
    """Wait out *task* even while further cancellations keep arriving.

    asyncio.shield protects the task from a surrounding cancellation, but each
    NEW cancellation re-raises out of the await while the task keeps running
    detached. Re-awaiting until the task is done guarantees a context write is
    never orphaned mid-flight; a real exception from the task still propagates.
    """
    while not task.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(task)


def _is_all_error_batch(tool_results: Sequence[ToolResult]) -> bool:
    """True when a non-empty tool batch had *every* call fail."""
    return bool(tool_results) and all(r.return_value.is_error for r in tool_results)


def _is_over_cost_ceiling(session_cost_usd: float, ceiling: float | None) -> bool:
    """True when a positive session spend ceiling has been reached.

    Best-effort: a ``None`` or non-positive ceiling is treated as disabled, and cost is
    ``0.0`` for models with unknown pricing, so the ceiling fails open (never blocks)
    rather than blocking on spend that cannot be estimated.
    """
    return ceiling is not None and ceiling > 0 and session_cost_usd >= ceiling


def _budget_exhausted_message(session_cost_usd: float, ceiling: float) -> Message:
    """Handoff message when the session reaches its configured spend ceiling."""
    text = (
        f"Stopping: this session has reached its configured spend ceiling "
        f"(estimated ${session_cost_usd:.2f} of ${ceiling:.2f}). Raise "
        f"`loop_control.max_session_cost_usd` in config, or start a new session, to continue."
    )
    return Message(role="assistant", content=[TextPart(text=text)])


def _crossed_budget_nudge_threshold(
    *,
    before_usd: float,
    after_usd: float,
    ceiling: float | None,
    ratio: float,
    stop_reason: StepStopReason,
) -> bool:
    if ceiling is None or ceiling <= 0 or after_usd <= 0:
        return False
    if stop_reason == "budget_exhausted":
        return False
    threshold = ratio * ceiling
    return before_usd < threshold <= after_usd


def _budget_nudge_message(*, session_cost_usd: float, ceiling: float, ratio: float) -> Message:
    text = BUDGET_CONTINUATION_NUDGE.format(
        ratio=ratio,
        spent=session_cost_usd,
        ceiling=ceiling,
    )
    return Message(role="user", content=[system_reminder(text[:500])])


def _user_message_with_hook_context(
    user_input: str | list[ContentPart], results: Sequence[HookResult]
) -> Message:
    """Build the user-turn message, appending non-block ``additional_context`` from
    UserPromptSubmit hooks as a system reminder so the model sees it as context for this
    prompt. Blocking results are handled separately and never contribute context.

    Hook stdout is external, untrusted content (it may echo git logs, issue bodies, or
    fetched pages), so it is wrapped in the untrusted-data envelope before injection —
    matching how fetch/search/shell/grep output is neutralized.
    """
    context = "\n\n".join(
        r.additional_context.strip()
        for r in results
        if r.action != "block" and r.additional_context.strip()
    )
    if not context:
        return Message(role="user", content=user_input)
    base: list[ContentPart] = (
        list(user_input) if isinstance(user_input, list) else [TextPart(text=user_input)]
    )
    reminder = system_reminder(
        "A UserPromptSubmit hook added context for this prompt:\n\n"
        + UntrustedData(context).render_for_prompt()
    )
    return Message(role="user", content=[*base, reminder])


def _with_agents_md_preamble(
    history: Sequence[Message], builtin_args: BuiltinSystemPromptArgs
) -> list[Message]:
    """Return *history* with the merged AGENTS.md prepended as a leading user-role
    ``<system-reminder>``, or a plain copy of *history* when no AGENTS.md applies.

    The preamble is assembled fresh from ``builtin_args`` on every step and is NEVER
    appended to ``context.history``. That is precisely what keeps the project instructions
    immune to the two failure modes a persisted home would hit: context compaction cannot
    summarize them away (they are not in the history it rewrites), and the dynamic-injection
    token budget cannot truncate them (they are not a budgeted injection). The input is left
    unmutated. See :func:`pythinker_code.soul.agent.render_agents_md_reminder`.
    """
    reminder = render_agents_md_reminder(builtin_args)
    if reminder is None:
        return list(history)
    preamble = Message(role="user", content=[system_reminder(reminder)])
    return [preamble, *history]


def _should_nudge_truncation(
    truncated: bool, has_tool_calls: bool, recoveries: int, limit: int
) -> bool:
    """Whether a truncated (output-cap) response should be nudged to continue.

    Only fires for a truncated response with NO tool calls — a tool-call response continues
    the loop via its results anyway. ``recoveries < limit`` bounds the retries per turn so a
    persistently-truncating model cannot loop forever.
    """
    return truncated and not has_tool_calls and recoveries < limit


def _stuck_summary_message(
    failures: int, tool_calls: Sequence[ToolCall], tool_results: Sequence[ToolResult]
) -> Message:
    """Build a concise handoff message when the loop yields on a degenerate stuck loop.

    Surfaces a count of consecutive all-error steps and a brief of what the last
    step tried, so the human can take over without reconstructing state.
    """
    calls_by_id = {call.id: call for call in tool_calls}
    tried: list[str] = []
    for result in tool_results:
        call = calls_by_id.get(result.tool_call_id)
        name = call.function.name if call else "tool"
        rv = result.return_value
        # A whitespace-only brief is truthy but strips to "", whose .splitlines() is []
        # — guard the [0] so the stuck backstop never crashes on the very errors it handles.
        brief_lines = (rv.brief or rv.message or "error").strip().splitlines()
        brief = brief_lines[0] if brief_lines else "error"
        if len(brief) > 200:
            brief = brief[:200] + "…"
        tried.append(f"- {name}: {brief}")
    text = (
        f"I appear to be stuck — the last {failures} steps each had every tool call "
        "fail, so I'm stopping and handing control back to you rather than continuing.\n\n"
        "What I last tried:\n" + "\n".join(tried) + "\n\n"
        "You can adjust the request, fix the underlying issue, or tell me how to proceed."
    )
    return Message(role="assistant", content=[TextPart(text=text)])


_UNFINISHED_INTENT_LEAD_RE = re.compile(
    r"^(let me|let's|let us|now let me|now i'?ll|i'?ll|i will|i'?m going to|"
    r"i am going to|first,?\s+i'?ll|next,?\s+i'?ll)\b",
    re.IGNORECASE,
)
_UNFINISHED_INTENT_ACTION_RE = re.compile(
    r"\b(synthesi|summari|writ|creat|compil|prepar|draft|put together|generat|"
    r"produc|present|report|provid|run|check|cross-check|verif|read|search|"
    r"analy|review|implement|fix|updat|build|gather|look|examin|continu|"
    r"proceed|start|begin|assembl|consolidat|finaliz|outlin|map out|"
    r"investigat|explor|dig into|pull together)\w*",
    re.IGNORECASE,
)


def _looks_like_unfinished_intent(text: str) -> bool:
    """Return True when *text* is essentially a statement of intent to act, with
    no actual result delivered.

    Models sometimes end a message with a transitional preamble such as
    "Let me synthesize the findings into a unified report." but attach no tool
    call and produce no result. The agent loop treats any tool-call-free message
    as the final answer, so the turn ends before the promised work is done. This
    detects that shape (conservatively) so the loop can nudge one more step.
    """
    text = text.strip()
    if not text or len(text) > 400 or text.endswith("?"):
        return False
    sentences = [s.strip() for s in re.split(r"[.!\n]+", text) if s.strip()]
    if not sentences:
        return False
    last = sentences[-1]
    if "let me know" in last.lower():
        return False
    return bool(
        _UNFINISHED_INTENT_LEAD_RE.match(last) and _UNFINISHED_INTENT_ACTION_RE.search(last)
    )


@dataclass(frozen=True, slots=True)
class StepOutcome:
    stop_reason: StepStopReason
    assistant_message: Message


type TurnStopReason = StepStopReason


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    stop_reason: TurnStopReason
    final_message: Message | None
    step_count: int

    @property
    def produced_answer(self) -> bool:
        """True when the turn ended with a substantive assistant answer.

        A turn can end without an exception yet not deliver a usable answer: a forced
        handoff (``stuck`` / ``budget_exhausted``), a rejected tool call (no final
        message), or an empty/whitespace final message. Those are degenerate stops, not
        completions — distinguishing them lets callers avoid reporting a non-answer as a
        clean success (e.g. a future print-mode exit-code mapping).
        """
        if self.stop_reason != "no_tool_calls" or self.final_message is None:
            return False
        return bool(self.final_message.extract_text(" ").strip())


class PythinkerSoul:
    """The soul of Pythinker CLI."""

    def __init__(
        self,
        agent: Agent,
        *,
        context: Context,
    ):
        """
        Initialize the soul.

        Args:
            agent (Agent): The agent to run.
            context (Context): The context of the agent.
        """
        self._agent = agent
        self._runtime = agent.runtime
        self._denwa_renji = agent.runtime.denwa_renji
        self._approval = agent.runtime.approval
        self._context = context
        self._loop_control = agent.runtime.config.loop_control
        if agent.steps is not None:
            self._loop_control = self._loop_control.model_copy(
                update={"max_steps_per_turn": agent.steps}
            )
        self._current_step_no = 0
        self._consecutive_failures = 0
        self._truncation_recoveries = 0
        # Cumulative LLM token usage for this soul instance (one run), so a subagent
        # can report its spend back to the orchestrating parent (subagent-2). A
        # resumed session runs on a fresh soul, so this counts the current run.
        self._cumulative_usage = TokenUsage(
            input_other=0, output=0, input_cache_read=0, input_cache_creation=0
        )
        self._session_cost_usd = 0.0
        self._deliberation_generation = 0
        self._sleep_inhibitor = SleepInhibitor(enabled=agent.runtime.config.prevent_idle_sleep)
        self._compaction = SimpleCompaction(base_prompt=self._runtime.config.compact_prompt)

        for tool in agent.toolset.tools:
            if tool.name == SendDMail_NAME:
                self._checkpoint_with_user_message = True
                break
        else:
            self._checkpoint_with_user_message = False

        self._steer_queue: asyncio.Queue[str | list[ContentPart]] = asyncio.Queue()
        self._prompt_queue_lock = asyncio.Lock()
        # Tool calls made in the previous step, fed to the toolset's dedup
        # tracking at the start of each step (see PythinkerToolset.begin_step).
        self._last_tool_calls: list[tuple[str, str]] = []
        self._current_turn_id: str = ""
        self._plan_mode: bool = self._runtime.session.state.plan_mode
        self._plan_session_id: str | None = self._runtime.session.state.plan_session_id
        # Pre-warm slug cache so the persisted slug survives process restarts
        if self._plan_session_id is not None and self._runtime.session.state.plan_slug is not None:
            from pythinker_code.tools.plan.heroes import seed_slug_cache

            seed_slug_cache(self._plan_session_id, self._runtime.session.state.plan_slug)
        self._pending_plan_activation_injection: bool = False
        if self._plan_mode:
            self._ensure_plan_session_id()
        self._injection_providers: list[DynamicInjectionProvider] = [
            PlanModeInjectionProvider(),
            # Self-filtering: injects only while session state holds a /goal contract.
            GoalModeInjectionProvider(),
            # Self-filtering: emits a fragment only when the active model matches a
            # known-quirk family, so it is safe to register unconditionally.
            ModelDefenseInjectionProvider(),
            # Self-filtering: root-only; flags inline /command references in the
            # latest user message that the shell could not have executed.
            InlineCommandReminderProvider(),
            # Self-filtering: root-only; nudges substantial normal-mode tasks toward
            # direct tools, todos, RunAgents, and verification.
            OrchestrationInjectionProvider(),
            # Self-filtering: root-only; posture-fingerprinted so it re-emits
            # exactly when yolo/auto/safe-mode/profile/session-approvals change.
            PermissionsInjectionProvider(),
            # Self-filtering: root-only; keeps the model's subagent list current
            # without tying it to the static tool description cache.
            AgentListInjectionProvider(),
            *(
                []
                if self._runtime.config.skip_auto_prompt_injection
                else [AutoModeInjectionProvider()]
            ),
        ]
        self._hook_engine: HookEngine = HookEngine()
        self._stop_hook_active: bool = False
        if self._runtime.role == "root":
            self._runtime.notifications.ack_ids("llm", extract_notification_ids(context.history))

        # Bind plan mode state to tools that support it
        self._bind_plan_mode_tools()

        self._runtime.rearm_injection = self.rearm_injection

        self._slash_commands = self._build_slash_commands()
        self._slash_command_map = self._index_slash_commands(self._slash_commands)

    @property
    def name(self) -> str:
        return self._agent.name

    @property
    def model_name(self) -> str:
        return self._runtime.llm.chat_provider.model_name if self._runtime.llm else ""

    @property
    def cumulative_usage(self) -> TokenUsage:
        """Total LLM token usage consumed by this soul instance (this run).

        Counts every step's LLM call plus compaction calls. A resumed session
        runs on a fresh soul, so this reflects the current run, not prior runs.
        """
        return self._cumulative_usage

    @property
    def model_capabilities(self) -> set[ModelCapability] | None:
        if self._runtime.llm is None:
            return None
        return self._runtime.llm.capabilities

    @property
    def is_yolo(self) -> bool:
        """Whether explicit yolo mode is active."""
        return self._approval.is_yolo()

    @property
    def is_auto_approve(self) -> bool:
        """Whether tool approvals are bypassed (explicit yolo, or implied by auto mode)."""
        return self._approval.is_auto_approve()

    @property
    def is_auto(self) -> bool:
        """Whether no user is present (auto mode)."""
        return self._approval.is_auto()

    @property
    def is_auto_flag(self) -> bool:
        """Whether persisted auto mode is active."""
        return self._approval.is_auto_flag()

    @property
    def is_subagent(self) -> bool:
        """Whether this soul is running as a subagent rather than the root session."""
        return self._runtime.role == "subagent"

    @property
    def plan_mode(self) -> bool:
        """Whether plan mode (read-only research and planning) is active."""
        return self._plan_mode

    @property
    def hook_engine(self) -> HookEngine:
        return self._hook_engine

    def set_hook_engine(self, engine: HookEngine) -> None:
        self._hook_engine = engine
        if isinstance(self._agent.toolset, PythinkerToolset):
            self._agent.toolset.set_hook_engine(engine)

    def add_injection_provider(self, provider: DynamicInjectionProvider) -> None:
        """Register an additional dynamic injection provider."""
        self._injection_providers.append(provider)

    def rearm_injection(self, key: str) -> None:
        """Re-arm matching dynamic injection providers after related state changes."""
        for provider in self._injection_providers:
            try:
                provider.rearm(key)
            except Exception:
                logger.debug("injection provider rearm failed")

    async def _collect_injections(self) -> list[DynamicInjection]:
        """Collect dynamic injections from all registered providers."""
        injections: list[DynamicInjection] = []
        for provider in self._injection_providers:
            try:
                result = await provider.get_injections(self._context.history, self)
                injections.extend(result)
            except Exception as exc:
                from pythinker_code.telemetry.errors import report_handled_error

                report_handled_error(
                    exc,
                    site="soul.injection.get",
                    provider=type(provider).__name__,
                )
                logger.warning(
                    "injection provider %s failed",
                    type(provider).__name__,
                    exc_info=True,
                )
        memory_config = getattr(self._runtime.config, "memory", None)
        if not getattr(memory_config, "injection_bus", True):
            return injections
        candidates = [dynamic_to_candidate(injection) for injection in injections]
        budget = injection_budget_from_runtime(self._runtime).injection_budget_tokens
        budgeted = collect_within_budget(candidates, budget)
        return [
            DynamicInjection(type=candidate.type, content=candidate.content)
            for candidate in budgeted
        ]

    async def _notify_injection_providers_compacted(self) -> None:
        """Notify all injection providers that the context has been compacted.

        Failures are isolated per-provider so a buggy third-party provider
        cannot abort compaction (which would skip CompactionEnd wire events
        and PostCompact telemetry).
        """
        for provider in self._injection_providers:
            try:
                await provider.on_context_compacted()
            except Exception as exc:
                from pythinker_code.telemetry.errors import report_handled_error

                report_handled_error(
                    exc,
                    site="soul.injection.on_context_compacted",
                    provider=type(provider).__name__,
                )
                logger.warning(
                    "injection provider %s on_context_compacted failed",
                    type(provider).__name__,
                    exc_info=True,
                )

    async def notify_auto_changed(self, enabled: bool) -> None:
        """Notify dynamic injection providers that auto mode changed."""
        for provider in self._injection_providers:
            try:
                await provider.on_auto_changed(enabled)
            except Exception as exc:
                from pythinker_code.telemetry.errors import report_handled_error

                report_handled_error(
                    exc,
                    site="soul.injection.on_auto_changed",
                    provider=type(provider).__name__,
                )
                logger.warning(
                    "injection provider %s on_auto_changed failed",
                    type(provider).__name__,
                    exc_info=True,
                )

    def _bind_plan_mode_tools(self) -> None:
        """Bind plan mode state to tools that support it."""
        if not isinstance(self._agent.toolset, PythinkerToolset):
            return

        def checker() -> bool:
            return self._plan_mode

        def path_getter() -> Path | None:
            return self.get_plan_file_path()

        # WriteFile gets both checker and path_getter (for plan file auto-approve)
        from pythinker_code.tools.file.write import WriteFile

        write_tool = self._agent.toolset.find("WriteFile")
        if isinstance(write_tool, WriteFile):
            write_tool.bind_plan_mode(checker, path_getter)

        from pythinker_code.tools.file.replace import StrReplaceFile

        replace_tool = self._agent.toolset.find("StrReplaceFile")
        if isinstance(replace_tool, StrReplaceFile):
            replace_tool.bind_plan_mode(checker, path_getter)

        # ExitPlanMode has a special bind() method
        from pythinker_code.tools.plan import ExitPlanMode

        exit_tool = self._agent.toolset.find("ExitPlanMode")
        if isinstance(exit_tool, ExitPlanMode):
            exit_tool.bind(
                self.toggle_plan_mode,
                path_getter,
                checker,
                self._approval.is_auto,
            )

        # EnterPlanMode has a special bind() method
        from pythinker_code.tools.plan.enter import EnterPlanMode

        enter_tool = self._agent.toolset.find("EnterPlanMode")
        if isinstance(enter_tool, EnterPlanMode):
            enter_tool.bind(
                self.toggle_plan_mode,
                path_getter,
                checker,
                # Match ExitPlanMode: gate on user presence (is_auto), not is_auto_approve.
                # Yolo skips approvals but the user is still present, so an interactive
                # yolo session should not silently slip into plan mode without confirming.
                self._approval.is_auto,
            )

        # AskUserQuestion — bind auto-mode checker for auto-dismiss.
        # Yolo alone keeps the tool live; only auto mode (no user present) dismisses.
        from pythinker_code.tools.ask_user import AskUserQuestion

        ask_tool = self._agent.toolset.find("AskUserQuestion")
        if isinstance(ask_tool, AskUserQuestion):
            ask_tool.bind_auto(
                self._approval.is_auto,
                policy=self._runtime.config.ask_user_question_policy,
            )

            async def _advise(questions: list[QuestionItem]) -> str | None:
                from pythinker_code.soul.deliberation import blind_advisor_verdict

                return await blind_advisor_verdict(self, questions)

            ask_tool.bind_deliberation(_advise)

    def _ensure_plan_session_id(self) -> None:
        """Allocate a stable plan session ID on first activation."""
        if self._plan_session_id is None:
            import uuid

            self._plan_session_id = uuid.uuid4().hex
            self._runtime.session.state.plan_session_id = self._plan_session_id
            # Compute and persist slug immediately so the path survives process restarts
            from pythinker_code.tools.plan.heroes import get_or_create_slug

            slug = get_or_create_slug(self._plan_session_id)
            self._runtime.session.state.plan_slug = slug
            self._runtime.session.save_state()

    def _set_plan_mode(self, enabled: bool, *, source: Literal["manual", "tool"]) -> bool:
        """Update plan mode state for either manual or tool-driven toggles."""
        if enabled == self._plan_mode:
            return self._plan_mode
        self._plan_mode = enabled
        if enabled:
            self._ensure_plan_session_id()
            self._pending_plan_activation_injection = source == "manual"
        else:
            self._pending_plan_activation_injection = False
            self._plan_session_id = None
            self._runtime.session.state.plan_session_id = None
            self._runtime.session.state.plan_slug = None
        # Persist plan mode to session state so it survives process restarts
        self._runtime.session.state.plan_mode = self._plan_mode
        self._runtime.session.save_state()
        return self._plan_mode

    def get_plan_file_path(self) -> Path | None:
        """Get the plan file path for the current session."""
        if self._plan_session_id is None:
            return None
        from pythinker_code.tools.plan.heroes import get_plan_file_path

        return get_plan_file_path(self._plan_session_id)

    def read_current_plan(self) -> str | None:
        """Read the current plan file content."""
        if self._plan_session_id is None:
            return None
        from pythinker_code.tools.plan.heroes import read_plan_file

        return read_plan_file(self._plan_session_id)

    def clear_current_plan(self) -> None:
        """Delete the current plan file."""
        path = self.get_plan_file_path()
        if path and path.exists():
            path.unlink()

    async def toggle_plan_mode(self) -> bool:
        """Toggle plan mode on/off. Returns the new state.

        Tools are not hidden/unhidden — instead, each tool checks plan mode
        state at call time and rejects if blocked.
        Periodic reminders are handled by the dynamic injection system.
        """
        return self._set_plan_mode(not self._plan_mode, source="tool")

    async def toggle_plan_mode_from_manual(self) -> bool:
        """Toggle plan mode from UI/manual entry points (slash command, keybinding)."""
        return self._set_plan_mode(not self._plan_mode, source="manual")

    async def set_plan_mode_from_manual(self, enabled: bool) -> bool:
        """Set plan mode to a specific state from UI/manual entry points.

        Unlike toggle, this accepts the desired state directly, avoiding
        race conditions when the caller already knows the target value.
        """
        return self._set_plan_mode(enabled, source="manual")

    def schedule_plan_activation_reminder(self) -> None:
        """Schedule a plan-mode activation reminder for the next turn.

        Use this when plan mode is already active (e.g. restored session with
        ``--plan`` flag) and ``_set_plan_mode`` would early-return because the
        state hasn't actually changed.
        """
        if self._plan_mode:
            self._pending_plan_activation_injection = True

    def consume_pending_plan_activation_injection(self) -> bool:
        """Consume the next-step activation reminder scheduled by a manual toggle."""
        if not self._plan_mode or not self._pending_plan_activation_injection:
            return False
        self._pending_plan_activation_injection = False
        return True

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        """Current thinking effort level, if known."""
        if self._runtime.llm is None:
            return None
        if self._runtime.llm.thinking_effort is not None:
            return self._runtime.llm.thinking_effort
        if thinking_effort := self._runtime.llm.chat_provider.thinking_effort:
            return thinking_effort
        return bool_to_thinking_effort(self._runtime.llm.thinking)

    @property
    def thinking(self) -> bool | None:
        """Whether thinking mode is enabled."""
        effort = self.thinking_effort
        if effort is not None:
            return thinking_effort_enabled(effort)
        return None

    def available_thinking_efforts(self) -> tuple[ThinkingEffort, ...]:
        """Selectable thinking levels for the current model."""
        if self._runtime.llm is None:
            return ("off",)
        return available_thinking_levels(self._runtime.llm.capabilities)

    def set_thinking_effort_from_manual(self, effort: ThinkingEffort) -> ThinkingEffort | None:
        """Apply a user-selected thinking level to the live runtime.

        Returns the effective/clamped level, or ``None`` when no LLM/model is
        active. Persistence is best-effort: a config write failure must not
        prevent the current session from using the new level.
        """
        if self._runtime.llm is None or self._runtime.llm.model_config is None:
            return None
        model = self._runtime.llm.model_config
        provider = self._runtime.config.providers.get(model.provider)
        if provider is None:
            provider = self._runtime.llm.provider_config
        if provider is None:
            return None

        levels = self.available_thinking_efforts()
        effective_effort = clamp_thinking_effort(effort, levels)
        new_llm = create_llm(
            provider,
            model,
            thinking=thinking_effort_enabled(effective_effort),
            thinking_effort=effective_effort,
            session_id=self._runtime.session.id,
            oauth=self._runtime.oauth,
        )
        if new_llm is None:
            return None
        self._runtime.llm = new_llm
        self._runtime.config.default_thinking = thinking_effort_enabled(effective_effort)
        self._runtime.config.default_thinking_effort = effective_effort

        config_file = self._runtime.config.source_file
        if config_file is not None:
            from pythinker_code.config import load_config, save_config
            from pythinker_code.exception import ConfigError

            try:
                config_for_save = load_config(config_file)
                config_for_save.default_thinking = thinking_effort_enabled(effective_effort)
                config_for_save.default_thinking_effort = effective_effort
                save_config(config_for_save, config_file)
            except (ConfigError, OSError) as exc:
                logger.warning(
                    "Failed to persist thinking effort change: {error}",
                    error=exc,
                )
        return effective_effort

    def cycle_thinking_effort_from_manual(self) -> ThinkingEffort | None:
        """Cycle to the next thinking level for the current model."""
        levels = self.available_thinking_efforts()
        if levels == ("off",):
            return None
        current = self.thinking_effort or "off"
        return self.set_thinking_effort_from_manual(next_thinking_level(current, levels))

    @property
    def status(self) -> StatusSnapshot:
        token_count = self._context.token_count
        max_size = self._runtime.llm.max_context_size if self._runtime.llm is not None else 0
        return StatusSnapshot(
            context_usage=self._context_usage,
            yolo_enabled=self._approval.is_yolo_flag(),
            auto_enabled=self._approval.is_auto(),
            plan_mode=self._plan_mode,
            context_tokens=token_count,
            max_context_tokens=max_size,
            mcp_status=self._mcp_status_snapshot(),
            session_cost_usd=self._session_cost_usd,
            total_input_tokens=self._cumulative_usage.input,
            total_output_tokens=self._cumulative_usage.output,
        )

    @property
    def agent(self) -> Agent:
        return self._agent

    @property
    def runtime(self) -> Runtime:
        return self._runtime

    @property
    def context(self) -> Context:
        return self._context

    @property
    def _context_usage(self) -> float:
        if self._runtime.llm is None or self._runtime.llm.max_context_size <= 0:
            return 0.0
        return self._context.token_count / self._runtime.llm.max_context_size

    @property
    def wire_file(self) -> WireFile:
        return self._runtime.session.wire_file

    def _mcp_status_snapshot(self):
        if not isinstance(self._agent.toolset, PythinkerToolset):
            return None
        return self._agent.toolset.mcp_status_snapshot()

    async def start_background_mcp_loading(self) -> bool:
        """Start deferred MCP loading, if any, without exposing toolset internals."""
        if not isinstance(self._agent.toolset, PythinkerToolset):
            return False
        return await self._agent.toolset.start_deferred_mcp_tool_loading()

    async def wait_for_background_mcp_loading(self) -> None:
        """Wait for any in-flight MCP startup to finish."""
        if not isinstance(self._agent.toolset, PythinkerToolset):
            return
        await self._agent.toolset.wait_for_mcp_tools()

    async def _checkpoint(self):
        await self._context.checkpoint(self._checkpoint_with_user_message)

    def steer(self, content: str | list[ContentPart]) -> None:
        """Queue a steer message for injection into the current turn."""
        self._steer_queue.put_nowait(content)

    async def _consume_pending_steers(self) -> bool:
        """Drain the steer queue and inject as follow-up user messages.

        Returns True if any steers were consumed.

        Note: /btw is intercepted at the UI layer (``classify_input``) before
        reaching the steer queue, so it never appears here.
        """
        consumed = False
        while True:
            try:
                content = self._steer_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            await self._inject_steer(content)
            wire_send(SteerInput(user_input=content))
            consumed = True
        return consumed

    async def _inject_steer(self, content: str | list[ContentPart]) -> None:
        """Inject a single steer as a regular follow-up user message."""
        parts = cast(
            list[ContentPart],
            [TextPart(text=content)] if isinstance(content, str) else list(content),
        )
        message = Message(role="user", content=parts)
        if self._runtime.llm is None:
            raise LLMNotSet()
        if missing_caps := check_message(message, self._runtime.llm.capabilities):
            raise LLMNotSupported(self._runtime.llm, list(missing_caps))
        await self._context.append_message(message)

    @property
    def available_slash_commands(self) -> list[SlashCommand[Any]]:
        return self._slash_commands

    async def run(
        self,
        user_input: str | list[ContentPart],
        *,
        skip_user_prompt_hook: bool = False,
    ):
        await self._prompt_queue_lock.acquire()
        approval_source_token = None
        created_approval_source: ApprovalSource | None = None
        turn_started = False
        turn_finished = False
        if get_current_approval_source_or_none() is None:
            created_approval_source = ApprovalSource(kind="foreground_turn", id=uuid.uuid4().hex)
            approval_source_token = set_current_approval_source(created_approval_source)
        try:
            # Refresh OAuth tokens on each turn to avoid idle-time expirations.
            await self._runtime.oauth.ensure_fresh(self._runtime)

            # Set session_id ContextVar for toolset hooks
            from pythinker_code.soul.toolset import set_session_id

            set_session_id(self._runtime.session.id)

            from pythinker_code.hooks import events

            # --- UserPromptSubmit hook ---
            # Synthetic internal prompts (e.g. background-task notification
            # follow-ups injected by ``Print`` after a bg task finishes or
            # the wait ceiling is hit) must bypass ``UserPromptSubmit``:
            # they are not user input, and a user-configured prompt-blocking
            # hook would drop the notification and hang the wait loop.
            hook_results: Sequence[HookResult] = []
            if not skip_user_prompt_hook:
                text_input_for_hook = user_input if isinstance(user_input, str) else ""

                hook_results = await self._hook_engine.trigger(
                    "UserPromptSubmit",
                    matcher_value=text_input_for_hook,
                    input_data=events.user_prompt_submit(
                        session_id=self._runtime.session.id,
                        cwd=_safe_cwd(str(self._runtime.work_dir)),
                        prompt=text_input_for_hook,
                    ),
                )
                for result in hook_results:
                    if result.action == "block":
                        wire_send(TurnBegin(user_input=user_input))
                        turn_started = True
                        wire_send(TextPart(text=result.reason or "Prompt blocked by hook."))
                        wire_send(TurnEnd())
                        turn_finished = True
                        return

            wire_send(TurnBegin(user_input=user_input))
            turn_started = True
            # Inject any non-block additional_context from UserPromptSubmit hooks into the
            # user turn so the model sees it as context for this prompt.
            user_message = _user_message_with_hook_context(user_input, hook_results)
            # Slash-command parsing must see only the user's text, never appended hook context.
            text_input = Message(role="user", content=user_input).extract_text(" ").strip()
            stop_harvest_start_index = len(self._context.history)

            primary_outcome: TurnOutcome | None = None
            if command_call := parse_slash_command_call(text_input):
                command = self._find_slash_command(command_call.name)
                if command is None:
                    # this should not happen actually, the shell should have filtered it out
                    wire_send(TextPart(text=f'Unknown slash command "/{command_call.name}".'))
                else:
                    ret = command.func(self, command_call.args)
                    if isinstance(ret, Awaitable):
                        await ret
            elif self._loop_control.max_ralph_iterations != 0:
                runner = FlowRunner.ralph_loop(
                    user_message,
                    self._loop_control.max_ralph_iterations,
                )
                await runner.run(self, "")
            else:
                primary_outcome = await self._turn(user_message)

            # --- Stop hook (max 1 re-trigger to prevent infinite loop) ---
            if not self._stop_hook_active:
                stop_results = await self._hook_engine.trigger(
                    "Stop",
                    input_data=events.stop(
                        session_id=self._runtime.session.id,
                        cwd=_safe_cwd(str(self._runtime.work_dir)),
                        stop_hook_active=False,
                    ),
                )
                for result in stop_results:
                    if result.action == "block" and result.reason:
                        self._stop_hook_active = True
                        try:
                            await self._turn(Message(role="user", content=result.reason))
                        finally:
                            self._stop_hook_active = False
                        break

            if primary_outcome is not None:
                await self._run_goal_continuations(primary_outcome)

            if getattr(self._runtime.config.memory, "harvest_on_stop", False):
                await self._harvest_on_stop(stop_harvest_start_index)

            wire_send(TurnEnd())
            turn_finished = True

            # Auto-set title after first real turn (skip slash commands)
            if not command_call:
                session = self._runtime.session
                if session.state.custom_title is None:
                    from pythinker_code.utils.string import shorten

                    title = shorten(
                        Message(role="user", content=user_input).extract_text(" "),
                        width=50,
                    )
                    if title:
                        from pythinker_code.session_state import (
                            load_session_state,
                            save_session_state,
                        )

                        # Read-modify-write: load fresh state to avoid
                        # overwriting concurrent web changes
                        fresh = load_session_state(session.dir)
                        if fresh.custom_title is None:
                            fresh.custom_title = title
                            save_session_state(fresh, session.dir)
                        session.state.custom_title = fresh.custom_title
                        if session.state.custom_title:
                            from pythinker_code.scratchpad import (
                                append_scratch_event_sync,
                                rename_session_scratch,
                            )

                            rename_session_scratch(
                                session.work_dir,
                                session_id=session.id,
                                session_title=session.state.custom_title,
                            )
                            await asyncio.to_thread(
                                append_scratch_event_sync,
                                session.work_dir,
                                session_id=session.id,
                                session_title=session.state.custom_title,
                                title="session title set",
                                details=[f"title: {session.state.custom_title}"],
                                labels=[f"scope:{session.state.custom_title}"],
                            )
        finally:
            if turn_started and not turn_finished:
                wire_send(TurnEnd())
            if created_approval_source is not None and self._runtime.approval_runtime is not None:
                self._runtime.approval_runtime.cancel_by_source(
                    created_approval_source.kind,
                    created_approval_source.id,
                )
            if approval_source_token is not None:
                reset_current_approval_source(approval_source_token)
            self._prompt_queue_lock.release()

    async def _run_goal_continuations(self, primary_outcome: TurnOutcome) -> None:
        """Auto-continue toward the active /goal after the primary turn.

        Automatic goal continuations, bounded per user
        submission by ``goal.max_continuations``. Hard stops (cancellation,
        MaxStepsReached, provider errors) propagate out of ``_turn`` and end
        the loop together with the run; a rejected tool call, a stuck turn, or
        a goal marked complete/blocked (via UpdateGoal) ends it gracefully.
        """
        if primary_outcome.stop_reason != "no_tool_calls":
            return
        goal_config = self._runtime.config.goal
        if not goal_config.auto_continue or self.is_subagent or self.plan_mode:
            return

        import pythinker_code.prompts as prompts

        for i in range(goal_config.max_continuations):
            goal = self._runtime.session.state.goal
            if goal is None or goal.status != "active":
                return
            content = prompts.GOAL_CONTINUATION.format(objective=goal.objective)
            if i == goal_config.max_continuations - 1:
                content += "\n\n" + prompts.GOAL_WRAP_UP
            outcome = await self._turn(Message(role="user", content=content))
            if outcome.stop_reason != "no_tool_calls":
                return

    async def turn(self, user_message: Message) -> TurnOutcome:
        """
        Run one full agent turn for ``user_message`` and return its outcome.

        The message is appended to the context, then the agent loop steps the
        model — executing any tool calls it makes — until the model stops.
        The returned ``TurnOutcome`` carries the ``stop_reason``
        (``"no_tool_calls"``: the model finished with a plain response;
        ``"tool_rejected"``: the user rejected a tool call; ``"stuck"``: the
        turn was cut short as a degenerate tool-call loop), the final
        assistant message (``None`` when a tool call was rejected), and the
        number of steps taken.

        This is the public entry point for external drivers (flows, slash
        command handlers). It emits per-step wire framing but NOT
        ``TurnBegin``/``TurnEnd``. Callers starting a new wire-level turn
        (e.g. ``FlowRunner``) must wrap the call in ``TurnBegin``/``TurnEnd``;
        callers already executing inside a framed turn (e.g. slash-command
        handlers running under ``run()``) must not add extra framing.

        Raises:
            LLMNotSet: When the LLM is not set.
            LLMNotSupported: When the LLM does not have required capabilities.
            ChatProviderError: When the LLM provider returns an error.
            MaxStepsReached: When the per-turn step limit is reached.
            asyncio.CancelledError: When the turn is cancelled by user.
        """
        # Thin delegate by design: many tests monkeypatch ``soul._turn`` to
        # stub turn execution, so ``_turn`` must remain the single
        # implementation/patch point that both ``turn()`` and internal
        # callers go through.
        return await self._turn(user_message)

    async def _turn(self, user_message: Message) -> TurnOutcome:
        from pythinker_code.extensions import shared_event_bus
        from pythinker_code.telemetry import metrics as _m
        from pythinker_code.telemetry import otel as _otel

        if self._runtime.llm is None:
            raise LLMNotSet()

        if missing_caps := check_message(user_message, self._runtime.llm.capabilities):
            raise LLMNotSupported(self._runtime.llm, list(missing_caps))

        self._current_turn_id = uuid.uuid4().hex
        self._last_tool_calls = []
        self._sleep_inhibitor.set_turn_running(True)
        try:
            bus = shared_event_bus()
            bus.emit(
                "user.message",
                {
                    "session_id": self._runtime.session.id,
                    "message": user_message,
                },
            )

            with _otel.start_span(
                "pythinker.turn",
                {
                    "session.id": self._runtime.session.id,
                    "agent.role": self._runtime.role,
                    "model": self._runtime.llm.model_name,
                    "plan_mode": self._plan_mode,
                    "gen_ai.operation.name": "invoke_agent",
                },
            ) as span:
                turn_t0 = time.monotonic()
                await self._checkpoint()  # this creates the checkpoint 0 on first run
                await self._context.append_message(user_message)
                logger.debug("Appended user message to context")
                cost_before_turn = self._session_cost_usd
                outcome = await self._agent_loop()
                ceiling = self._loop_control.max_session_cost_usd
                goal_config = self._runtime.config.goal
                if not goal_config.auto_continue and _crossed_budget_nudge_threshold(
                    before_usd=cost_before_turn,
                    after_usd=self._session_cost_usd,
                    ceiling=ceiling,
                    ratio=self._loop_control.budget_nudge_ratio,
                    stop_reason=outcome.stop_reason,
                ):
                    assert ceiling is not None
                    await self._context.append_message(
                        _budget_nudge_message(
                            session_cost_usd=self._session_cost_usd,
                            ceiling=ceiling,
                            ratio=self._loop_control.budget_nudge_ratio,
                        )
                    )
                span.set_attribute("turn.stop_reason", outcome.stop_reason)
                span.set_attribute("turn.step_count", outcome.step_count)
                # Observable signal that a turn ended without a substantive answer (a
                # degenerate stop), so the degenerate-completion rate is measurable before
                # any exit-code mapping consumes it.
                span.set_attribute("turn.produced_answer", outcome.produced_answer)
                _m.record_turn(
                    duration_seconds=time.monotonic() - turn_t0,
                    step_count=outcome.step_count,
                    stop_reason=outcome.stop_reason,
                )
                bus.emit(
                    "turn.end",
                    {
                        "session_id": self._runtime.session.id,
                        "stop_reason": outcome.stop_reason,
                        "step_count": outcome.step_count,
                        "produced_answer": outcome.produced_answer,
                    },
                )
                return outcome
        finally:
            self._sleep_inhibitor.set_turn_running(False)

    def _build_slash_commands(self) -> list[SlashCommand[Any]]:
        commands: list[SlashCommand[Any]] = list(soul_slash_registry.list_commands())
        seen_names = {cmd.name for cmd in commands}

        for template in self._runtime.prompt_templates.values():
            if template.name in seen_names:
                logger.warning(
                    "Skipping prompt template /{name}: name already registered",
                    name=template.name,
                )
                continue
            commands.append(
                SlashCommand(
                    name=template.name,
                    func=self._make_prompt_template_runner(template),
                    description=template.description or "",
                    aliases=[],
                )
            )
            seen_names.add(template.name)

        for skill in self._runtime.skills.values():
            if skill.type not in ("standard", "flow"):
                continue
            name = f"{SKILL_COMMAND_PREFIX}{skill.name}"
            if name in seen_names:
                logger.warning(
                    "Skipping skill slash command /{name}: name already registered",
                    name=name,
                )
                continue
            commands.append(
                SlashCommand(
                    name=name,
                    func=self._make_skill_runner(skill),
                    description=skill.description or "",
                    aliases=[],
                )
            )
            seen_names.add(name)

        for skill in self._runtime.skills.values():
            if skill.type != "flow":
                continue
            if skill.flow is None:
                logger.warning("Flow skill {name} has no flow; skipping", name=skill.name)
                continue
            command_name = f"{FLOW_COMMAND_PREFIX}{skill.name}"
            if command_name in seen_names:
                logger.warning(
                    "Skipping prompt flow slash command /{name}: name already registered",
                    name=command_name,
                )
                continue
            runner = FlowRunner(skill.flow, name=skill.name)
            commands.append(
                SlashCommand(
                    name=command_name,
                    func=runner.run,
                    description=skill.description or "",
                    aliases=[],
                )
            )
            seen_names.add(command_name)

        return commands

    @staticmethod
    def _index_slash_commands(
        commands: list[SlashCommand[Any]],
    ) -> dict[str, SlashCommand[Any]]:
        indexed: dict[str, SlashCommand[Any]] = {}
        for command in commands:
            indexed[command.name] = command
            for alias in command.aliases:
                indexed[alias] = command
        return indexed

    def _find_slash_command(self, name: str) -> SlashCommand[Any] | None:
        return self._slash_command_map.get(name)

    def _make_prompt_template_runner(
        self, template: PromptTemplate
    ) -> Callable[[PythinkerSoul, str], None | Awaitable[None]]:
        async def _run_template(
            soul: PythinkerSoul,
            args: str,
            *,
            _template: PromptTemplate = template,
        ) -> None:
            from pythinker_code.telemetry import track

            track("prompt_template_invoked", template_name=_template.name)
            expanded = expand_prompt_template(_template, args)
            await soul._turn(Message(role="user", content=expanded))

        _run_template.__doc__ = template.description
        return _run_template

    def _record_invoked_skill(self, skill_name: str) -> None:
        """Remember a slash-invoked skill so compaction can restore its instructions."""
        active_skills = self._runtime.session.state.active_skills
        if skill_name in active_skills:
            return
        active_skills.append(skill_name)
        try:
            self._runtime.session.save_state()
        except Exception:
            logger.warning("Failed to persist active skill state", exc_info=True)

    def _make_skill_runner(
        self, skill: Skill
    ) -> Callable[[PythinkerSoul, str], None | Awaitable[None]]:
        async def _run_skill(soul: PythinkerSoul, args: str, *, _skill: Skill = skill) -> None:
            from pythinker_code.telemetry import track

            track("skill_invoked", skill_name=_skill.name)
            skill_text = await read_skill_text_with_local_specialization(
                _skill, soul._runtime.skills
            )
            if skill_text is None:
                wire_send(
                    TextPart(text=f'Failed to load skill "/{SKILL_COMMAND_PREFIX}{_skill.name}".')
                )
                return
            soul._record_invoked_skill(_skill.name)
            extra = args.strip()
            if extra:
                skill_text = f"{skill_text}\n\nUser request:\n{extra}"
            await soul._turn(Message(role="user", content=skill_text))

        _run_skill.__doc__ = skill.description
        return _run_skill

    async def _agent_loop(self) -> TurnOutcome:
        """The main agent loop for one run."""
        assert self._runtime.llm is not None

        # Discard any stale steers from a previous turn.
        while True:
            try:
                self._steer_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        if isinstance(self._agent.toolset, PythinkerToolset):
            await self.start_background_mcp_loading()
            loading = bool((snapshot := self._mcp_status_snapshot()) and snapshot.loading)
            if loading:
                wire_send(StatusUpdate(mcp_status=snapshot))
                wire_send(MCPLoadingBegin())
            try:
                await self.wait_for_background_mcp_loading()
                # Track MCP connection result
                if loading:
                    from pythinker_code.telemetry import track as _track_mcp

                    mcp_snap = self._mcp_status_snapshot()
                    if mcp_snap:
                        if mcp_snap.connected > 0:
                            _track_mcp(
                                "mcp_connected",
                                server_count=mcp_snap.connected,
                                total_count=mcp_snap.total,
                            )
                        _failed = mcp_snap.total - mcp_snap.connected
                        if _failed > 0:
                            _track_mcp(
                                "mcp_failed",
                                failed_count=_failed,
                                total_count=mcp_snap.total,
                            )
            finally:
                if loading:
                    wire_send(StatusUpdate(mcp_status=self._mcp_status_snapshot()))
                    wire_send(MCPLoadingEnd())

        step_no = 0
        self._current_step_no = 0
        # One-shot per turn: nudge at most once when a step ends on a bare
        # statement of intent (see `_looks_like_unfinished_intent`).
        self._intent_nudge_used = False
        # Reset the degenerate-loop failure tracker at the start of each turn.
        self._consecutive_failures = 0
        # Bounded per turn: nudges to continue after an output-token-limit truncation.
        self._truncation_recoveries = 0
        # One-shot per turn: reactive compact-and-retry after a provider
        # context-length rejection (proactive thresholds can undercount).
        # A second overflow in the same turn must propagate instead of looping
        # through repeated compactions.
        overflow_recovery_used = False
        while True:
            # Spend ceiling: stop before starting another (paid) step once the session's
            # accumulated estimated cost reaches the configured ceiling. Checked before the
            # step so an already-exhausted budget ends the turn without a fresh model call.
            ceiling = self._loop_control.max_session_cost_usd
            if _is_over_cost_ceiling(self._session_cost_usd, ceiling):
                assert ceiling is not None  # narrowed by _is_over_cost_ceiling
                message = _budget_exhausted_message(self._session_cost_usd, ceiling)
                await self._context.append_message(message)
                wire_send(TextPart(text=message.extract_text(" ")))
                return TurnOutcome(
                    stop_reason="budget_exhausted",
                    final_message=message,
                    step_count=step_no,
                )
            step_no += 1
            if step_no > self._loop_control.max_steps_per_turn:
                raise MaxStepsReached(self._loop_control.max_steps_per_turn)

            self._current_step_no = step_no
            wire_send(StepBegin(n=step_no))
            back_to_the_future: BackToTheFuture | None = None
            step_outcome: StepOutcome | None = None
            try:
                # Cheap tier first: prune stale tool outputs when usage crosses the
                # (lower) prune threshold, to defer or avoid the lossy full summary.
                if should_prune(
                    self._context.token_count_with_pending,
                    self._runtime.llm.max_context_size,
                    ratio=self._loop_control.prune_trigger_ratio,
                ):
                    try:
                        await self.prune_context()
                    except Exception as prune_err:
                        from pythinker_code.telemetry.errors import report_handled_error

                        report_handled_error(prune_err, site="soul.context.prune")
                        logger.warning(
                            "Context prune failed at step {step_no}: {error}",
                            step_no=step_no,
                            error=prune_err,
                        )
                # compact the context if needed (still over the higher threshold)
                if should_auto_compact(
                    self._context.token_count_with_pending,
                    self._runtime.llm.max_context_size,
                    trigger_ratio=self._loop_control.compaction_trigger_ratio,
                    reserved_context_size=self._loop_control.reserved_context_size,
                ):
                    logger.info("Context too long, compacting...")
                    try:
                        await self.compact_context()
                    except Exception as compact_err:
                        from pythinker_code.telemetry.errors import report_handled_error

                        report_handled_error(compact_err, site="soul.context.compact")
                        logger.error(
                            "Context compaction failed at step {step_no}: {error_type}: {error}",
                            step_no=step_no,
                            error_type=type(compact_err).__name__,
                            error=compact_err,
                        )
                        raise

                    # Compaction makes a billable LLM call that folds into
                    # self._session_cost_usd. Re-check the ceiling here so a session
                    # just under the limit cannot pay for compaction *and* a full
                    # step before the top-of-loop guard fires again next iteration.
                    if _is_over_cost_ceiling(self._session_cost_usd, ceiling):
                        assert ceiling is not None  # narrowed by _is_over_cost_ceiling
                        message = _budget_exhausted_message(self._session_cost_usd, ceiling)
                        await self._context.append_message(message)
                        wire_send(TextPart(text=message.extract_text(" ")))
                        return TurnOutcome(
                            stop_reason="budget_exhausted",
                            final_message=message,
                            step_count=step_no - 1,  # this step's _step() never ran
                        )

                logger.debug("Beginning step {step_no}", step_no=step_no)
                await self._checkpoint()
                self._denwa_renji.set_n_checkpoints(self._context.n_checkpoints)
                step_outcome = await self._step()
            except BackToTheFuture as e:
                back_to_the_future = e
            except Exception as e:
                from pythinker_code.telemetry.errors import report_handled_error

                report_handled_error(e, site="soul.step.error")
                # any other exception should interrupt the step
                req_id = getattr(e, "request_id", None)
                logger.error(
                    "Agent step {step_no} failed: {error_type}: {error}"
                    + (" (request_id={request_id})" if req_id else ""),
                    step_no=step_no,
                    error_type=type(e).__name__,
                    error=e,
                    request_id=req_id,
                )
                wire_send(StepInterrupted())
                # Track API/step errors
                from pythinker_code.telemetry import track

                error_type, status_code = classify_api_error(e)
                from pythinker_code.telemetry.metrics import classify_model_family

                api_error_props: dict[str, bool | int | float | str | None] = {
                    "error_type": error_type,
                    "gen_ai_system": classify_llm_system(self._runtime.llm.chat_provider),
                    "model": self._runtime.llm.chat_provider.model_name,
                    "model_family": classify_model_family(
                        self._runtime.llm.chat_provider.model_name
                    ),
                }
                if status_code is not None:
                    api_error_props["status_code"] = status_code
                track("api_error", **api_error_props)
                if error_type == "context_overflow" and not overflow_recovery_used:
                    overflow_recovery_used = True
                    recovered = await self._recover_from_context_overflow(step_no)
                    wire_send(
                        ContextOverflowRecovered(
                            outcome="recovered" if recovered else "failed",
                            trigger_step=step_no,
                        )
                    )
                    if recovered:
                        continue
                # --- StopFailure hook ---
                from pythinker_code.hooks import events as _hook_events

                self._hook_engine.fire_and_forget_trigger(
                    "StopFailure",
                    matcher_value=type(e).__name__,
                    input_data=_hook_events.stop_failure(
                        session_id=self._runtime.session.id,
                        cwd=_safe_cwd(str(self._runtime.work_dir)),
                        error_type=type(e).__name__,
                        error_message=str(e),
                    ),
                )
                # break the agent loop
                raise

            if step_outcome is not None:
                has_steers = await self._consume_pending_steers()
                if has_steers:
                    continue  # steers injected, force another LLM step

                final_message = (
                    step_outcome.assistant_message
                    if step_outcome.stop_reason in ("no_tool_calls", "stuck")
                    else None
                )
                return TurnOutcome(
                    stop_reason=step_outcome.stop_reason,
                    final_message=final_message,
                    step_count=step_no,
                )

            if back_to_the_future is not None:
                await self._context.revert_to(back_to_the_future.checkpoint_id)
                # The reverted history no longer contains the last step's calls,
                # so they must not seed cross-step dedup for the next step.
                self._last_tool_calls = []
                await self._checkpoint()
                await self._context.append_message(back_to_the_future.messages)

            # Consume any pending steers between steps
            await self._consume_pending_steers()

    async def _step(self) -> StepOutcome | None:
        """Run a single step and return a stop outcome, or None to continue."""
        # already checked in `run`
        assert self._runtime.llm is not None
        chat_provider = self._runtime.llm.chat_provider
        self._deliberation_generation += 1
        deliberation_generation = self._deliberation_generation
        approval_source = get_current_approval_source_or_none()
        if approval_source is not None:
            deliberation_context_id = f"{approval_source.kind}:{approval_source.id}"
            if approval_source.agent_id is not None:
                deliberation_context_id = f"{deliberation_context_id}:{approval_source.agent_id}"
        elif self._runtime.subagent_id is not None:
            deliberation_context_id = self._runtime.subagent_id
        elif self._runtime.role == "root":
            deliberation_context_id = "root"
        else:
            deliberation_context_id = f"subagent:{self._runtime.session.id}"

        if self._runtime.role == "root":

            async def _append_notification(view: NotificationView) -> None:
                await self._context.append_message(build_notification_message(view, self._runtime))
                # --- Notification hook ---
                from pythinker_code.hooks import events

                self._hook_engine.fire_and_forget_trigger(
                    "Notification",
                    matcher_value=view.event.type,
                    input_data=events.notification(
                        session_id=self._runtime.session.id,
                        cwd=_safe_cwd(str(self._runtime.work_dir)),
                        sink="llm",
                        notification_type=view.event.type,
                        title=view.event.title,
                        body=view.event.body,
                        severity=view.event.severity,
                    ),
                )

            await self._runtime.notifications.deliver_pending(
                "llm",
                limit=4,
                before_claim=self._runtime.background_tasks.reconcile,
                on_notification=_append_notification,
            )

        # Dynamic injection
        injections = await self._collect_injections()
        if injections:
            combined_reminders = "\n".join(system_reminder(inj.content).text for inj in injections)
            await self._context.append_message(
                Message(
                    role="user",
                    content=[TextPart(text=combined_reminders)],
                )
            )

        # Prepend the merged AGENTS.md as a leading <system-reminder> (assembled fresh from
        # runtime args, never persisted to history) so the project instructions are immune to
        # compaction and the injection budget, then normalize to merge adjacent user messages.
        effective_history = normalize_history(
            _with_agents_md_preamble(self._context.history, self._runtime.builtin_args)
        )

        # Capture tool results as they stream in. If the batch is interrupted
        # mid-flight, already-completed calls must keep their real output rather
        # than being overwritten with a synthetic "interrupted" marker; only the
        # still-pending calls get the marker (see the CancelledError handler).
        completed_tool_results: dict[str, ToolResult] = {}

        def _on_tool_result(tool_result: ToolResult) -> None:
            completed_tool_results[tool_result.tool_call_id] = tool_result
            wire_send(tool_result)

        async def _run_step_once() -> StepResult:
            # Reset per-step dedup state. Inside the retry wrapper on purpose: a
            # retried step must not await tool tasks cancelled by the failed attempt.
            if isinstance(self._agent.toolset, PythinkerToolset):
                self._agent.toolset.begin_step(
                    self._last_tool_calls,
                    step_no=self._current_step_no,
                    turn_id=self._current_turn_id,
                )
            # run an LLM step (may be interrupted)
            from pythinker_code.telemetry import metrics as _m
            from pythinker_code.telemetry import otel as _otel

            # Resolve gen_ai.system once so spans and metrics agree.
            gen_ai_system = classify_llm_system(chat_provider)

            with _otel.start_span(
                "pythinker.llm",
                {
                    "gen_ai.system": gen_ai_system,
                    "gen_ai.request.model": chat_provider.model_name,
                    "gen_ai.model.family": _m.classify_model_family(chat_provider.model_name),
                    "session.id": self._runtime.session.id,
                    "gen_ai.operation.name": "chat",
                },
            ) as span:
                llm_t0 = time.monotonic()
                try:
                    profile_token = set_step_permission_profile(
                        permission_profile_for_runtime(self._runtime)
                    )
                    try:
                        with deliberation_scope(deliberation_context_id, deliberation_generation):
                            step_result = await pythinker_core.step(
                                chat_provider,
                                self._agent.system_prompt,
                                self._agent.toolset,
                                effective_history,
                                on_message_part=wire_send,
                                on_tool_result=_on_tool_result,
                            )
                    finally:
                        reset_step_permission_profile(profile_token)
                except Exception as exc:
                    llm_elapsed = time.monotonic() - llm_t0
                    error_type, status_code = classify_api_error(exc)
                    with contextlib.suppress(Exception):
                        span.set_attribute("error.type", error_type)
                        if status_code is not None:
                            span.set_attribute("http.response.status_code", status_code)
                    with contextlib.suppress(Exception):
                        _m.record_llm_call(
                            duration_seconds=llm_elapsed,
                            system=gen_ai_system,
                            model=chat_provider.model_name,
                            success=False,
                        )
                    with contextlib.suppress(Exception):
                        _m.record_error(kind="api_error", error_type=error_type)
                    raise
                llm_elapsed = time.monotonic() - llm_t0
                # Attach response details — usage may be None on partial / cached responses.
                if step_result.id:
                    span.set_attribute("gen_ai.response.id", step_result.id)
                u = step_result.usage
                if u is not None:
                    self._cumulative_usage = accumulate_usage(self._cumulative_usage, u)
                    self._session_cost_usd += estimate_cost_usd(u, self.model_name)

                def _opt_int(attr: str) -> int | None:
                    """Read an optional usage counter as int — None when usage or the
                    field is absent (usage may be None on partial / cached responses)."""
                    value = getattr(u, attr, None) if u is not None else None
                    return int(value) if value is not None else None

                input_tokens = _opt_int("input")
                output_tokens = _opt_int("output")
                # Prompt-cache token accounting. pythinker freezes the system prompt
                # to maximize cache hits, so surfacing these makes cache efficiency
                # (and any regression that silently breaks cache-keying) observable
                # from telemetry rather than only as an aggregate cost spike.
                cache_read_tokens = _opt_int("input_cache_read")
                cache_creation_tokens = _opt_int("input_cache_creation")
                if input_tokens is not None:
                    span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
                if output_tokens is not None:
                    span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
                if cache_read_tokens is not None:
                    span.set_attribute("gen_ai.usage.cache_read_input_tokens", cache_read_tokens)
                if cache_creation_tokens is not None:
                    span.set_attribute(
                        "gen_ai.usage.cache_creation_input_tokens", cache_creation_tokens
                    )
                # Per-call finish-reason proxy: pythinker_core's StepResult does not
                # expose the provider finish_reason, so derive it from whether the
                # step produced tool calls (tool_use) or stopped with text (stop).
                span.set_attribute(
                    "gen_ai.response.finish_reasons",
                    ["tool_use"] if step_result.tool_calls else ["stop"],
                )
                span.set_attribute("llm.tool_calls", len(step_result.tool_calls))
                _m.record_llm_call(
                    duration_seconds=llm_elapsed,
                    system=gen_ai_system,
                    model=chat_provider.model_name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_creation_tokens=cache_creation_tokens,
                    success=True,
                )
                return step_result

        max_attempts = self._loop_control.max_retries_per_step

        def _before_step_retry_sleep(retry_state: RetryCallState) -> None:
            self._retry_log("step", retry_state)
            self._emit_step_retry(retry_state, max_attempts=max_attempts)

        @tenacity.retry(
            retry=retry_if_exception(self._is_retryable_error),
            before_sleep=_before_step_retry_sleep,
            wait=wait_exponential_jitter(initial=0.3, max=5, jitter=0.5),
            stop=stop_after_attempt(max_attempts),
            reraise=True,
        )
        async def _pythinker_core_step_with_retry() -> StepResult:
            return await self._run_with_connection_recovery(
                "step",
                _run_step_once,
                chat_provider=chat_provider,
            )

        t0 = time.monotonic()
        result = await _pythinker_core_step_with_retry()
        llm_elapsed = time.monotonic() - t0
        usage = result.usage
        logger.info(
            "LLM step completed in {elapsed:.1f}s (input={input_tokens}, output={output_tokens})",
            elapsed=llm_elapsed,
            input_tokens=usage.input if usage else "?",
            output_tokens=usage.output if usage else "?",
        )
        _step_model_name: str | None = chat_provider.model_name
        _step_provider_key: str | None = None
        if self._runtime.llm.model_config is not None:
            _step_provider_key = self._runtime.llm.model_config.provider
        status_update = StatusUpdate(
            token_usage=usage,
            message_id=result.id,
            model_name=_step_model_name,
            provider_key=_step_provider_key,
            plan_mode=self._plan_mode,
        )
        if usage is not None:
            # mark the token count for the context before the step
            await self._context.update_token_count(usage.input)
            snap = self.status
            status_update.context_usage = snap.context_usage
            status_update.context_tokens = snap.context_tokens
            status_update.max_context_tokens = snap.max_context_tokens
        wire_send(status_update)

        # wait for all tool results (may be interrupted)
        plan_mode_before_tools = self._plan_mode
        # Scope the deliberation one-shot to this context + step. Tool futures normally
        # inherit this ContextVar when created during pythinker_core.step above; keeping
        # it bound here also covers any future implementation that starts work lazily in
        # tool_results().
        with deliberation_scope(deliberation_context_id, deliberation_generation):
            try:
                results = await result.tool_results()
            except asyncio.CancelledError:
                # Interrupted mid-tool: persist the assistant message plus a result
                # for every tool_call so the next turn does not see unanswered
                # tool_calls (which providers reject). Keep the real output of calls
                # that already completed (streamed via on_tool_result); only the
                # still-pending calls get a synthetic interruption marker. Shield the
                # write from the same cancellation so it completes, then re-raise.
                interrupted = [
                    completed_tool_results.get(tc.id)
                    or ToolResult(
                        tool_call_id=tc.id,
                        return_value=ToolRuntimeError(message="Tool call interrupted by user."),
                    )
                    for tc in result.tool_calls
                ]
                interrupted_grow_task = asyncio.create_task(self._grow_context(result, interrupted))
                try:
                    await asyncio.shield(interrupted_grow_task)
                except asyncio.CancelledError:
                    # A second interrupt landed mid-write: still wait the marker
                    # write out so the context never keeps unanswered tool_calls.
                    await _settle_shielded(interrupted_grow_task)
                raise
        logger.debug("Got tool results: {results}", results=results)

        # Update dedup tracking for the next step
        if isinstance(self._agent.toolset, PythinkerToolset):
            self._last_tool_calls = self._agent.toolset.end_step()

        # If a tool (EnterPlanMode/ExitPlanMode) changed plan mode during execution,
        # send a corrected StatusUpdate so the client sees the up-to-date state.
        if self._plan_mode != plan_mode_before_tools:
            wire_send(StatusUpdate(plan_mode=self._plan_mode))

        # Shield context manipulation from cancellation, but do not orphan the write task.
        grow_context_task = asyncio.create_task(self._grow_context(result, results))
        try:
            await asyncio.shield(grow_context_task)
        except asyncio.CancelledError:
            await _settle_shielded(grow_context_task)
            raise

        # Truncation recovery: a response cut off by the output-token limit that made no tool
        # call would otherwise end the turn as a half-finished answer. Nudge the model to
        # resume (bounded per turn) instead of treating the cut-off text as the final answer.
        if _should_nudge_truncation(
            result.truncated,
            bool(result.tool_calls),
            self._truncation_recoveries,
            self._loop_control.max_truncation_recoveries,
        ):
            self._truncation_recoveries += 1
            await self._context.append_message(
                Message(
                    role="user",
                    content=[
                        system_reminder(
                            "Your previous response was cut off by the output token limit. "
                            "Continue directly from where you stopped — no apology, no recap — "
                            "and break the remaining work into smaller pieces."
                        )
                    ],
                )
            )
            return None

        if invalid_summary := _malformed_empty_tool_call_summary(result.tool_calls, results):
            message = Message(
                role="assistant",
                content=[
                    TextPart(
                        text=(
                            "I couldn't continue because the model emitted malformed empty "
                            "tool calls with missing required arguments: "
                            f"{invalid_summary}. Please retry the request, or switch models "
                            "if this repeats."
                        )
                    )
                ],
            )
            await self._context.append_message(message)
            return StepOutcome(stop_reason="no_tool_calls", assistant_message=message)

        rejected_errors = [
            result.return_value
            for result in results
            if isinstance(result.return_value, ToolRejectedError)
        ]
        if (
            rejected_errors
            and not any(e.has_feedback for e in rejected_errors)
            and self._runtime.role != "subagent"
        ):
            # Pure rejection (no user feedback) — stop the turn.
            # Subagents skip this so the LLM can see the rejection and try
            # an alternative approach instead of terminating immediately.
            _ = self._denwa_renji.fetch_pending_dmail()
            return StepOutcome(stop_reason="tool_rejected", assistant_message=result.message)

        # handle pending D-Mail
        if dmail := self._denwa_renji.fetch_pending_dmail():
            assert dmail.checkpoint_id >= 0, "DenwaRenji guarantees checkpoint_id >= 0"
            assert dmail.checkpoint_id < self._context.n_checkpoints, (
                "DenwaRenji guarantees checkpoint_id < n_checkpoints"
            )
            # raise to let the main loop take us back to the future
            raise BackToTheFuture(
                dmail.checkpoint_id,
                [
                    Message(
                        role="user",
                        content=[
                            system(
                                "You just got a D-Mail from your future self. "
                                "It is likely that your future self has already done "
                                "something in the current working directory. Please read "
                                "the D-Mail and decide what to do next. You MUST NEVER "
                                "mention to the user about this information. "
                                f"D-Mail content:\n\n{dmail.message.strip()}"
                            )
                        ],
                    )
                ],
            )

        if result.tool_calls:
            # Degenerate-loop backstop: count consecutive steps where every tool
            # call failed; past the configured threshold, hand control back to the
            # user with a summary instead of burning steps until max_steps_per_turn.
            threshold = self._loop_control.max_consecutive_failures
            if _is_all_error_batch(results):
                self._consecutive_failures += 1
                if threshold and self._consecutive_failures >= threshold:
                    from pythinker_code.telemetry import track

                    summary = _stuck_summary_message(
                        self._consecutive_failures, result.tool_calls, results
                    )
                    await self._context.append_message(summary)
                    wire_send(TextPart(text=summary.extract_text(" ")))
                    track(
                        "agent_stuck",
                        consecutive_failures=self._consecutive_failures,
                        model=self._runtime.llm.model_name,
                    )
                    return StepOutcome(stop_reason="stuck", assistant_message=summary)
            else:
                self._consecutive_failures = 0
            return None

        # A tool-call-free message normally ends the turn. If it is only a
        # restatement of intent ("Let me synthesize the findings…") with no
        # result, nudge the model to actually deliver — but at most once per
        # turn so a stubborn model can still finish.
        if not getattr(self, "_intent_nudge_used", False) and _looks_like_unfinished_intent(
            result.message.extract_text(" ")
        ):
            self._intent_nudge_used = True
            await self._context.append_message(
                Message(
                    role="user",
                    content=[
                        system_reminder(
                            "Your previous message stated an intention to act (for example "
                            "to produce a report or run a tool) but included no tool call and "
                            "no actual result, which would normally end your turn. Either "
                            "produce the promised result now, in full, or make the necessary "
                            "tool call. Do not reply with only a restatement of intent."
                        )
                    ],
                )
            )
            return None

        return StepOutcome(stop_reason="no_tool_calls", assistant_message=result.message)

    async def _grow_context(self, result: StepResult, tool_results: list[ToolResult]):
        from pythinker_code.extensions import shared_event_bus

        logger.debug("Growing context with result: {result}", result=result)

        assert self._runtime.llm is not None
        tool_messages = [tool_result_to_message(tr) for tr in tool_results]
        for tm in tool_messages:
            if missing_caps := check_message(tm, self._runtime.llm.capabilities):
                logger.warning(
                    "Tool result message requires unsupported capabilities: {caps}",
                    caps=missing_caps,
                )
                raise LLMNotSupported(self._runtime.llm, list(missing_caps))

        bus = shared_event_bus()
        bus.emit(
            "assistant.message",
            {
                "session_id": self._runtime.session.id,
                "message": result.message,
                "usage": result.usage,
            },
        )
        for tr in tool_results:
            bus.emit(
                "tool.call.end",
                {
                    "session_id": self._runtime.session.id,
                    "tool_call_id": getattr(tr, "tool_call_id", None),
                    "is_error": getattr(tr, "is_error", False),
                },
            )

        await self._context.append_message(result.message)
        if result.usage is not None:
            await self._context.update_token_count(result.usage.total)

        logger.debug(
            "Appending tool messages to context: {tool_messages}", tool_messages=tool_messages
        )
        await self._context.append_message(tool_messages)
        # token count of tool results are not available yet

    async def _recover_from_context_overflow(self, step_no: int) -> bool:
        """Reactive shrink-and-retry after a provider context-length rejection.

        The proactive prune/compact thresholds run on heuristic token counts
        and can undercount (e.g. large pending tool output), so the provider
        may still reject a step. Prune (best-effort), force a full
        compaction, and let the loop retry the step once. Returns False when
        compaction itself fails — the original error then propagates.
        """
        from pythinker_code.telemetry import track

        logger.warning(
            "Provider rejected step {step_no} for context length; compacting and retrying once",
            step_no=step_no,
        )
        try:
            try:
                await self.prune_context()
            except Exception as prune_err:
                logger.debug(
                    "Best-effort prune during overflow recovery failed: {error}",
                    error=prune_err,
                )
            await self.compact_context()
        except Exception as compact_err:
            from pythinker_code.telemetry.errors import report_handled_error

            report_handled_error(compact_err, site="soul.context.overflow_recovery")
            logger.error(
                "Context-overflow recovery compaction failed: {error_type}: {error}",
                error_type=type(compact_err).__name__,
                error=compact_err,
            )
            track("context_overflow_recovery", outcome="failed")
            return False
        track("context_overflow_recovery", outcome="recovered")
        return True

    async def prune_context(self) -> bool:
        """Cheap, fidelity-preserving compaction tier: replace large stale
        tool-result bodies in deep history with placeholders, then rewrite the
        context. Returns True if anything was pruned.

        Unlike full compaction this makes no LLM call and preserves the
        conversational structure (roles, order, tool_call_id pairing), so it can
        run frequently to defer or avoid the lossy summary. No-op (returns False)
        when there is nothing worth pruning. Runs silently — no compaction wire
        events — since it may fire often and is not a user-visible summary.
        """
        capped, cap_freed = cap_stale_tool_result_bodies(
            self._context.history,
            protect_last=self._loop_control.prune_protect_last,
            max_chars=self._loop_control.prune_tool_result_max_chars,
        )
        pruned, prune_freed = prune_stale_tool_outputs(
            capped,
            protect_last=self._loop_control.prune_protect_last,
            min_chars=self._loop_control.prune_min_chars,
        )
        freed = cap_freed + prune_freed
        if freed <= 0:
            return False

        before_tokens = self._context.token_count
        # Snapshot history first: clear() rotates the backing file, so a mid-rebuild
        # failure would otherwise leave the context as just the system prompt. Reuse the
        # same clear+rebuild primitive compact_context uses (the supported way to mutate
        # the append-only JSONL context), but roll back to the snapshot if it throws.
        snapshot = list(self._context.history)
        # Reduce the AUTHORITATIVE pre-prune count by the estimated tokens freed, rather
        # than replacing it with a full heuristic re-estimate of the remaining history. A
        # full re-estimate can over-count the survivors (chars/4 overshoots code/markup),
        # leaving the context above the prune trigger and re-firing the whole rewrite every
        # step. The delta uses the same estimator on both sides so its bias cancels, and
        # pruning can only lower the count (pruned ⊆ snapshot ⇒ delta ≥ 0).
        freed_tokens = estimate_text_tokens(snapshot) - estimate_text_tokens(pruned)
        pruned_tokens = max(0, before_tokens - max(0, freed_tokens))
        await self._context.clear()
        try:
            await self._context.write_system_prompt(self._agent.system_prompt)
            await self._checkpoint()
            await self._context.append_message(pruned)
            await self._context.update_token_count(pruned_tokens)
        except Exception:
            await self._context.clear()
            await self._context.write_system_prompt(self._agent.system_prompt)
            await self._checkpoint()
            if snapshot:
                await self._context.append_message(snapshot)
            await self._context.update_token_count(before_tokens)
            raise
        # Unlike full compaction, pruning preserves every non-tool message verbatim
        # (only tool-result *bodies* are elided), so prior dynamic injections survive in
        # history. Do NOT re-arm injection providers here, or one-shot fragments (e.g. the
        # model-defense reminder) get re-emitted as duplicates on the next step.

        from pythinker_code.telemetry import track

        track(
            "context_pruned",
            before_tokens=before_tokens,
            after_tokens=self._context.token_count,
            freed_chars=freed,
        )
        logger.info("Pruned {freed} chars of stale tool output from context", freed=freed)
        return True

    async def compact_context(self, custom_instruction: str = "") -> None:
        """
        Compact the context.

        Raises:
            LLMNotSet: When the LLM is not set.
            ChatProviderError: When the chat provider returns an error.
        """

        chat_provider = self._runtime.llm.chat_provider if self._runtime.llm is not None else None

        async def _run_compaction_once() -> CompactionResult:
            if self._runtime.llm is None:
                raise LLMNotSet()
            return await self._compaction.compact(
                self._context.history, self._runtime.llm, custom_instruction=custom_instruction
            )

        @tenacity.retry(
            retry=retry_if_exception(self._is_retryable_error),
            before_sleep=partial(self._retry_log, "compaction"),
            wait=wait_exponential_jitter(initial=0.3, max=5, jitter=0.5),
            stop=stop_after_attempt(self._loop_control.max_retries_per_step),
            reraise=True,
        )
        async def _compact_with_retry() -> CompactionResult:
            return await self._run_with_connection_recovery(
                "compaction",
                _run_compaction_once,
                chat_provider=chat_provider,
            )

        trigger_reason = "manual" if custom_instruction else "auto"
        before_tokens = self._context.token_count
        history_before_compaction = tuple(self._context.history)
        from pythinker_code.hooks import events

        pre_compact_results = await self._hook_engine.trigger(
            "PreCompact",
            matcher_value=trigger_reason,
            input_data=events.pre_compact(
                session_id=self._runtime.session.id,
                cwd=_safe_cwd(str(self._runtime.work_dir)),
                trigger=trigger_reason,
                token_count=before_tokens,
                custom_instructions=custom_instruction,
            ),
        )
        for result in pre_compact_results:
            if result.action == "block":
                raise RuntimeError(result.reason or "Compaction blocked by PreCompact hook")

        restore_context = await build_compaction_restore_context(
            history_before_compaction,
            work_dir=self._runtime.builtin_args.PYTHINKER_WORK_DIR,
            additional_dirs=self._runtime.additional_dirs,
            active_skill_names=getattr(self._runtime.session.state, "active_skills", ()),
            skills_by_name=getattr(self._runtime, "skills", {}),
        )

        if getattr(self._runtime.config.memory, "harvest_enabled", False):
            await self._harvest_before_compaction(history_before_compaction, custom_instruction)

        wire_send(CompactionBegin())
        try:
            compaction_result = await _compact_with_retry()
            if not compaction_result.messages:
                raise RuntimeError("Compaction produced no messages; preserving existing history")
            # Compaction makes its own LLM call outside the step loop; fold its usage
            # into the cumulative total so a child's reported spend includes it.
            if compaction_result.usage is not None:
                self._cumulative_usage = accumulate_usage(
                    self._cumulative_usage, compaction_result.usage
                )
                self._session_cost_usd += estimate_cost_usd(
                    compaction_result.usage, self.model_name
                )
            await self._context.clear()
            try:
                await self._context.write_system_prompt(self._agent.system_prompt)
                await self._checkpoint()
                await self._context.append_message(compaction_result.messages)
                estimated_token_count = compaction_result.estimated_token_count
                summary_text = compact_summary_text(compaction_result.messages)

                if restore_context.messages:
                    await self._context.append_message(restore_context.messages)
                    estimated_token_count += estimate_text_tokens(restore_context.messages)

                if self._runtime.role == "root":
                    active_task_snapshot = build_active_task_snapshot(
                        self._runtime.background_tasks
                    )
                    if active_task_snapshot is not None:
                        active_task_message = Message(
                            role="user",
                            content=[
                                system(
                                    "The following background tasks are still active"
                                    " after compaction. Use TaskList if you need to"
                                    " re-enumerate them later."
                                ),
                                TextPart(text=active_task_snapshot),
                            ],
                        )
                        await self._context.append_message(active_task_message)
                        estimated_token_count += estimate_text_tokens([active_task_message])

                post_compact_results = await self._hook_engine.trigger(
                    "PostCompact",
                    matcher_value=trigger_reason,
                    input_data=events.post_compact(
                        session_id=self._runtime.session.id,
                        cwd=_safe_cwd(str(self._runtime.work_dir)),
                        trigger=trigger_reason,
                        estimated_token_count=estimated_token_count,
                        compact_summary=summary_text,
                    ),
                )
                session_start_results = await self._hook_engine.trigger(
                    "SessionStart",
                    matcher_value="compact",
                    input_data=events.session_start(
                        session_id=self._runtime.session.id,
                        cwd=_safe_cwd(str(self._runtime.work_dir)),
                        source="compact",
                    ),
                )
                hook_context_message = build_hook_context_message(
                    result.additional_context
                    for result in [*post_compact_results, *session_start_results]
                )
                if hook_context_message is not None:
                    await self._context.append_message(hook_context_message)
                    estimated_token_count += estimate_text_tokens([hook_context_message])

                # Estimate token count so context_usage is not reported as 0%
                await self._context.update_token_count(estimated_token_count)

                # Notify dynamic injection providers that history has been rebuilt so
                # they can reset any one-shot throttling state. Failures are isolated
                # per-provider so compaction completion (wire event + telemetry) is
                # not affected by a buggy provider.
                await self._notify_injection_providers_compacted()
            except Exception:
                # Rebuild faulted after clear() rotated the backing file. Restore
                # the pre-compaction history so an I/O fault cannot truncate the
                # live context to just the system prompt. Same primitive as
                # prune_context.
                await self._context.clear()
                await self._context.write_system_prompt(self._agent.system_prompt)
                await self._checkpoint()
                if history_before_compaction:
                    await self._context.append_message(list(history_before_compaction))
                await self._context.update_token_count(before_tokens)
                raise

        except Exception:
            from pythinker_code.telemetry import track

            track(
                "compaction_triggered",
                trigger_type=trigger_reason,
                before_tokens=before_tokens,
                success=False,
            )
            raise
        finally:
            # Always close the wire pair, even if the LLM call, context rewrite,
            # hooks, or injection-provider notifications fail.
            wire_send(CompactionEnd())

        wire_send(TextPart(text=restore_context.display_text()))

        from pythinker_code.telemetry import track

        track(
            "compaction_triggered",
            trigger_type=trigger_reason,
            before_tokens=before_tokens,
            after_tokens=estimated_token_count,
            success=True,
        )

    async def _harvest_before_compaction(
        self, history: Sequence[Message], custom_instruction: str = ""
    ) -> None:
        try:
            prepared = self._compaction.prepare(history, custom_instruction=custom_instruction)
        except Exception as exc:
            logger.warning("compaction prepare failed during harvest: {!r}", exc)
            return
        dropped_count = max(0, len(history) - len(prepared.to_preserve))
        dropped = list(history[:dropped_count])
        if not dropped:
            return
        try:
            from pythinker_code.memory.harvest import CompactionHarvester
            from pythinker_code.scratchpad import append_scratch_note

            notes = CompactionHarvester().harvest(dropped)
        except Exception as exc:
            logger.warning("compaction harvester crashed: {!r}", exc)
            return
        persisted = 0
        for note in notes:
            try:
                await append_scratch_note(
                    self._runtime.work_dir,
                    kind=note.kind,
                    content=note.content,
                    session_id=self._runtime.session.id,
                    session_title=self._runtime.session.title,
                    labels=["source:compaction"],
                )
                persisted += 1
            except Exception as exc:
                logger.warning("append_scratch_note failed for kind={!r}: {!r}", note.kind, exc)
        if persisted:
            try:
                self.rearm_injection("project_memory")
            except Exception as exc:
                logger.warning("rearm_injection(project_memory) failed: {!r}", exc)

    async def _harvest_on_stop(self, history_start_index: int) -> None:
        recent_history = list(self._context.history[history_start_index:])
        if not recent_history:
            return
        try:
            from pythinker_code.memory.harvest import CompactionHarvester
            from pythinker_code.scratchpad import append_scratch_note

            notes = CompactionHarvester().harvest(recent_history)
        except Exception as exc:
            logger.warning("stop-time memory harvester crashed: {!r}", exc)
            return
        persisted = 0
        for note in notes:
            try:
                append_result = await append_scratch_note(
                    self._runtime.work_dir,
                    kind=note.kind,
                    content=note.content,
                    session_id=self._runtime.session.id,
                    session_title=self._runtime.session.title,
                    labels=["source:stop"],
                )
                if append_result.appended:
                    persisted += 1
                else:
                    logger.debug(
                        "stop-time memory note was not appended: reason={reason}",
                        reason=append_result.reason,
                    )
            except Exception as exc:
                logger.warning(
                    "append_scratch_note failed during stop harvest for kind={!r}: {!r}",
                    note.kind,
                    exc,
                )
        if persisted:
            try:
                self.rearm_injection("project_memory")
            except Exception as exc:
                logger.warning("rearm_injection(project_memory) failed: {!r}", exc)

    @staticmethod
    def _is_retryable_error(exception: BaseException) -> bool:
        if isinstance(exception, (APIConnectionError, APITimeoutError)):
            return not bool(getattr(exception, "_pythinker_recovery_exhausted", False))
        if isinstance(exception, APIEmptyResponseError):
            return True
        if not isinstance(exception, APIStatusError):
            return False
        if exception.status_code == 429 and _is_hard_usage_limit(exception):
            # A subscription usage cap (e.g. ChatGPT `usage_limit_reached`)
            # resets in hours, not seconds — retrying with backoff only adds
            # latency before the inevitable failure. Surface it immediately.
            return False
        return exception.status_code in (
            429,  # Too Many Requests
            500,  # Internal Server Error
            502,  # Bad Gateway
            503,  # Service Unavailable
            504,  # Gateway Timeout
        )

    async def _run_with_connection_recovery(
        self,
        name: str,
        operation: Callable[[], Awaitable[Any]],
        *,
        chat_provider: object | None = None,
        _auth_retried: bool = False,
        _connection_retried: bool = False,
    ) -> Any:
        try:
            return await operation()
        except APIStatusError as error:
            if error.status_code != 401 or _auth_retried:
                raise
            # Only attempt refresh+retry when the active model's provider
            # uses OAuth.  For plain API-key providers there is nothing
            # to refresh and retrying would just add latency.
            active_provider = (
                self._runtime.config.providers.get(self._runtime.llm.model_config.provider)
                if self._runtime.llm and self._runtime.llm.model_config
                else None
            )
            if not (active_provider and active_provider.oauth):
                raise
            logger.warning(
                "Received 401 during {name}, attempting token refresh",
                name=name,
            )
            try:
                await self._runtime.oauth.ensure_fresh(self._runtime, force=True)
            except Exception as refresh_exc:
                logger.exception("Token refresh failed after 401.")
                raise error from refresh_exc
            # Re-enter full recovery so that transient connection errors
            # on the retry are still handled by on_retryable_error.
            return await self._run_with_connection_recovery(
                name,
                operation,
                chat_provider=chat_provider,
                _auth_retried=True,
                _connection_retried=_connection_retried,
            )
        except (APIConnectionError, APITimeoutError) as error:
            if _connection_retried:
                logger.warning(
                    "Chat provider recovery exhausted for {name}: {error_type}: {error}",
                    name=name,
                    error_type=type(error).__name__,
                    error=error,
                )
                error._pythinker_recovery_exhausted = True  # type: ignore[attr-defined]
                raise
            if not isinstance(chat_provider, RetryableChatProvider):
                raise
            try:
                recovered = chat_provider.on_retryable_error(error)
            except Exception as recover_exc:
                from pythinker_code.telemetry.errors import report_handled_error

                report_handled_error(recover_exc, site="soul.chat.recover")
                logger.exception(
                    "Failed to recover chat provider during {name} after {error_type}.",
                    name=name,
                    error_type=type(error).__name__,
                )
                raise
            if not recovered:
                logger.warning(
                    "Chat provider recovery not available for {name} after {error_type}.",
                    name=name,
                    error_type=type(error).__name__,
                )
                raise
            logger.info(
                "Recovered chat provider during {name} after {error_type}; retrying once.",
                name=name,
                error_type=type(error).__name__,
            )
            # Re-enter the full recovery path so a 401 on the retry can still
            # trigger OAuth refresh instead of bubbling straight to the user.
            return await self._run_with_connection_recovery(
                name,
                operation,
                chat_provider=chat_provider,
                _auth_retried=_auth_retried,
                _connection_retried=True,
            )

    @staticmethod
    def _retry_log(name: str, retry_state: RetryCallState):
        error = retry_state.outcome.exception() if retry_state.outcome else None
        logger.warning(
            "Retrying {name} for the {n} time (last error: {error_type}: {error}). "
            "Waiting {sleep} seconds.",
            name=name,
            n=retry_state.attempt_number,
            error_type=type(error).__name__ if error else "unknown",
            error=error or "unknown",
            sleep=retry_state.next_action.sleep
            if retry_state.next_action is not None
            else "unknown",
        )

    def _emit_step_retry(self, retry_state: RetryCallState, *, max_attempts: int) -> None:
        error = retry_state.outcome.exception() if retry_state.outcome else None
        next_action = retry_state.next_action
        wait_s = next_action.sleep if next_action is not None else 0.0
        wire_send(
            StepRetry(
                n=self._current_step_no,
                next_attempt=retry_state.attempt_number + 1,
                max_attempts=max_attempts,
                wait_s=wait_s,
                error_type=type(error).__name__ if error else "unknown",
                status_code=error.status_code if isinstance(error, APIStatusError) else None,
            )
        )


class BackToTheFuture(Exception):
    """
    Raise when we need to revert the context to a previous checkpoint.
    The main agent loop should catch this exception and handle it.
    """

    def __init__(self, checkpoint_id: int, messages: Sequence[Message]):
        self.checkpoint_id = checkpoint_id
        self.messages = messages
