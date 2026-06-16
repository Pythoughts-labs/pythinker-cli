from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import sys
import time
import warnings
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pythinker_host
from pydantic import SecretStr
from pythinker_core.chat_provider import ThinkingEffort
from pythinker_host.path import HostPath

from pythinker_code.agentspec import DEFAULT_AGENT_FILE
from pythinker_code.auth.oauth import OAuthManager, get_device_id
from pythinker_code.background.models import is_terminal_status
from pythinker_code.cli import InputFormat, OutputFormat
from pythinker_code.config import Config, LLMModel, LLMProvider, load_config
from pythinker_code.constant import VERSION
from pythinker_code.llm import augment_provider_with_env_vars, create_llm, model_display_name
from pythinker_code.session import Session
from pythinker_code.share import get_share_dir
from pythinker_code.soul import RunCancelled, run_soul
from pythinker_code.soul.agent import Runtime, load_agent
from pythinker_code.soul.context import Context
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.thinking import bool_to_thinking_effort, effective_config_thinking_effort
from pythinker_code.utils.aioqueue import QueueShutDown
from pythinker_code.utils.logging import logger, open_original_stderr, redirect_stderr_to_logger
from pythinker_code.utils.path import shorten_home
from pythinker_code.wire import Wire, WireUISide
from pythinker_code.wire.types import ApprovalRequest, ApprovalResponse, ContentPart, WireMessage

if TYPE_CHECKING:
    from fastmcp.mcp_config import MCPConfig


_CWD_LOCK = asyncio.Lock()


def _safe_git_branch(cwd: str | Path | HostPath) -> str | None:
    """Return the current git branch name, or ``None`` outside a repo / on error."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "branch", "--show-current"],
            capture_output=True,
            encoding="utf-8",
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def _resumed_unsupervised_notice(*, resumed: bool, yolo: bool, auto: bool) -> str | None:
    """Welcome-banner warning when a resumed session is running unsupervised.

    Resuming restores ``yolo``/``auto`` from persisted state, so a session can come back
    unattended and, under YOLO, auto-approving actions with no prompt. Surface that
    prominently at startup (it also fires when the modes were passed explicitly on the
    resume command — acceptable over-notification). ``None`` when not a resume or no
    unsupervised mode is active.
    """
    if not resumed or not (yolo or auto):
        return None
    modes = " + ".join(name for name, active in (("YOLO", yolo), ("auto", auto)) if active)
    if yolo:
        return f"{modes} active — actions auto-approved; toggle with /yolo /auto"
    return "auto active — interactive approvals still required; toggle with /auto"


def _patch_session_id(record: dict[str, Any]) -> None:
    """Inject the current session ID (from ContextVar) into log records."""
    try:
        from pythinker_code.soul.toolset import get_session_id

        sid = get_session_id()
        record["extra"]["sid"] = sid if sid else ""
    except Exception:
        record["extra"].setdefault("sid", "")


def enable_logging(debug: bool = False, *, redirect_stderr: bool = True) -> None:
    # NOTE: stderr redirection is implemented by swapping the process-level fd=2 (dup2).
    # That can hide Click/Typer error output during CLI startup, so some entrypoints delay
    # installing it until after critical initialization succeeds.
    logger.remove()  # Remove default stderr handler
    logger.enable("pythinker_code")
    if debug:
        logger.enable("pythinker_core")
    logger.add(
        get_share_dir() / "logs" / "pythinker.log",
        # FIXME: configure level for different modules
        level="TRACE" if debug else "INFO",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{name}:{function}:{line} | {extra[sid]} - {message}"
        ),
        rotation="06:00",
        retention="10 days",
    )
    logger.configure(extra={"sid": ""}, patcher=_patch_session_id)
    if redirect_stderr:
        redirect_stderr_to_logger()


def _write_original_stderr(text: str) -> None:
    """Write a user-facing notice to the terminal even if ``fd=2`` has been
    redirected into the logger by ``redirect_stderr_to_logger``.

    Falls back to ``sys.stderr`` when no redirector is installed (tests,
    early-startup code paths), matching the semantics of ``_emit_fatal_error``
    in ``cli/__init__.py``.
    """
    with open_original_stderr() as stream:
        if stream is not None:
            stream.write(text.encode("utf-8", errors="replace"))
            stream.flush()
            return
    sys.stderr.write(text)


async def _refresh_managed_models_silent(config: Config) -> None:
    from pythinker_code.auth.platforms import refresh_managed_models

    try:
        await refresh_managed_models(config)
    except Exception as exc:
        logger.warning("Background managed-model refresh failed: {error}", error=exc)


def _cleanup_stale_foreground_subagents(runtime: Runtime) -> None:
    subagent_store = getattr(runtime, "subagent_store", None)
    if subagent_store is None:
        return

    stale_agent_ids = [
        record.agent_id
        for record in subagent_store.list_instances()
        if record.status == "running_foreground"
    ]
    for agent_id in stale_agent_ids:
        logger.warning(
            "Marking stale foreground subagent instance as failed during startup: {agent_id}",
            agent_id=agent_id,
        )
        subagent_store.update_instance(agent_id, status="failed")


class PythinkerCLI:
    @staticmethod
    async def create(
        session: Session,
        *,
        # Basic configuration
        config: Config | Path | None = None,
        model_name: str | None = None,
        thinking: bool | None = None,
        thinking_effort: ThinkingEffort | None = None,
        # Run mode
        yolo: bool = False,
        no_yolo: bool = False,
        auto: bool = False,
        runtime_auto: bool = False,
        plan_mode: bool = False,
        resumed: bool = False,
        ui_mode: str = "shell",
        # Extensions
        agent_file: Path | None = None,
        mcp_configs: list[MCPConfig] | list[dict[str, Any]] | None = None,
        skills_dirs: list[HostPath] | None = None,
        # Loop control
        max_steps_per_turn: int | None = None,
        max_retries_per_step: int | None = None,
        max_ralph_iterations: int | None = None,
        startup_progress: Callable[[str], None] | None = None,
        defer_mcp_loading: bool = False,
        scratchpad_section: str | None = None,
    ) -> PythinkerCLI:
        """
        Create a PythinkerCLI instance.

        Args:
            session (Session): A session created by `Session.create` or `Session.continue_`.
            config (Config | Path | None, optional): Configuration to use, or path to config file.
                Defaults to None.
            model_name (str | None, optional): Name of the model to use. Defaults to None.
            thinking (bool | None, optional): Whether to enable thinking mode. Defaults to None.
            thinking_effort (ThinkingEffort | None, optional): Reasoning effort override.
                Defaults to None.
            yolo (bool, optional): Dangerously skip permission approvals. The user is still
                reachable via ``AskUserQuestion``. Defaults to False.
            no_yolo (bool, optional): Force yolo OFF for this run, overriding the ``yolo``
                flag, config ``default_yolo``, and persisted session state. Defaults to False.
            auto (bool, optional): Invocation-level auto mode (no user is present to answer
                questions or approve actions). Implies auto-approve. Defaults to False.
            runtime_auto (bool, optional): Internal invocation-only auto-mode overlay, used by
                print mode so it stays non-interactive without changing persisted session auto
                state. Defaults to False.
            agent_file (Path | None, optional): Path to the agent file. Defaults to None.
            mcp_configs (list[MCPConfig | dict[str, Any]] | None, optional): MCP configs to load
                MCP tools from. Defaults to None.
            skills_dirs (list[HostPath] | None, optional): Custom skills directories that
                override default user/project discovery. Defaults to None.
            max_steps_per_turn (int | None, optional): Maximum number of steps in one turn.
                Defaults to None.
            max_retries_per_step (int | None, optional): Maximum number of retries in one step.
                Defaults to None.
            max_ralph_iterations (int | None, optional): Extra iterations after the first turn in
                Ralph mode. Defaults to None.
            startup_progress (Callable[[str], None] | None, optional): Progress callback used by
                interactive startup UI. Defaults to None.
            defer_mcp_loading (bool, optional): Defer MCP startup until the interactive shell is
                ready. Defaults to False.

        Raises:
            FileNotFoundError: When the agent file is not found.
            ConfigError(PythinkerCLIException, ValueError): When the configuration is invalid.
            AgentSpecError(PythinkerCLIException, ValueError): When the agent specification is
                invalid.
            SystemPromptTemplateError(PythinkerCLIException, ValueError): When the system prompt
                template is invalid.
            InvalidToolError(PythinkerCLIException, ValueError): When any tool cannot be loaded.
            MCPConfigError(PythinkerCLIException, ValueError): When any MCP configuration is
                invalid.
            MCPRuntimeError(PythinkerCLIException, RuntimeError): When any MCP server cannot be
                connected.
        """
        _create_t0 = time.monotonic()
        _phase_timings_ms: dict[str, int] = {}

        if startup_progress is not None:
            startup_progress("Loading configuration...")

        _phase_t = time.monotonic()
        config = config if isinstance(config, Config) else load_config(config)
        _phase_timings_ms["config_ms"] = int((time.monotonic() - _phase_t) * 1000)
        if max_steps_per_turn is not None:
            config.loop_control.max_steps_per_turn = max_steps_per_turn
        if max_retries_per_step is not None:
            config.loop_control.max_retries_per_step = max_retries_per_step
        if max_ralph_iterations is not None:
            config.loop_control.max_ralph_iterations = max_ralph_iterations
        logger.info("Loaded config: {config}", config=config)

        # Install the plugin activation policy for this session so artifact
        # collectors (skills/agents/commands/hooks/mcp) honor config-configured
        # enable-state and the external (Claude/Codex) opt-in.
        from pythinker_code.plugin.policy import policy_from_config, set_plugin_policy

        set_plugin_policy(
            policy_from_config(
                config.plugins.discover_external,
                config.plugins.external_exec,
                config.plugins.enabled,
                config.plugins.disabled,
                config.plugins.options,
            )
        )

        _phase_t = time.monotonic()
        oauth = OAuthManager(config)

        bg_refresh_task = asyncio.create_task(_refresh_managed_models_silent(config))

        model: LLMModel | None = None
        provider: LLMProvider | None = None

        # try to use config file
        if not model_name and config.default_model:
            # no --model specified && default model is set in config
            model = config.models[config.default_model]
            provider = config.providers[model.provider]
        if model_name and model_name in config.models:
            # --model specified && model is set in config
            model = config.models[model_name]
            provider = config.providers[model.provider]

        if not model:
            model = LLMModel(provider="", model="", max_context_size=100_000)
            provider = LLMProvider(type="pythinker", base_url="", api_key=SecretStr(""))

        # try overwrite with environment variables
        assert provider is not None
        assert model is not None
        env_overrides = augment_provider_with_env_vars(provider, model, provider_key=model.provider)

        # determine thinking mode / effort. The bool flag is kept for CLI and
        # config compatibility; the effort string is the source of truth for new
        # sessions when present.
        if thinking_effort is None:
            thinking_effort = (
                bool_to_thinking_effort(thinking)
                if thinking is not None
                else effective_config_thinking_effort(
                    config.default_thinking,
                    config.default_thinking_effort,
                )
            )
        thinking = thinking_effort != "off"

        # determine yolo mode
        yolo = yolo if yolo else config.default_yolo

        # determine plan mode (only for new sessions, not restored)
        if not resumed:
            plan_mode = plan_mode if plan_mode else config.default_plan_mode

        llm = create_llm(
            provider,
            model,
            thinking=thinking,
            thinking_effort=thinking_effort,
            session_id=session.id,
            oauth=oauth,
        )
        if llm is not None:
            logger.info("Using LLM provider: {provider}", provider=provider)
            logger.info("Using LLM model: {model}", model=model)
            logger.info("Thinking mode: {thinking}", thinking=thinking)
            logger.info("Thinking effort: {thinking_effort}", thinking_effort=thinking_effort)

        if startup_progress is not None:
            startup_progress("Scanning workspace...")

        runtime = await Runtime.create(
            config,
            oauth,
            llm,
            session,
            yolo,
            auto=auto,
            runtime_auto=runtime_auto,
            no_yolo=no_yolo,
            skills_dirs=skills_dirs,
            scratchpad_section=scratchpad_section,
        )
        runtime.ui_mode = ui_mode
        runtime.resumed = resumed
        runtime.notifications.recover()
        runtime.background_tasks.reconcile()
        _cleanup_stale_foreground_subagents(runtime)
        _phase_timings_ms["init_ms"] = int((time.monotonic() - _phase_t) * 1000)

        if agent_file is None:
            agent_file = DEFAULT_AGENT_FILE
        if startup_progress is not None:
            startup_progress("Loading agent...")

        _phase_t = time.monotonic()
        agent = await load_agent(
            agent_file,
            runtime,
            mcp_configs=mcp_configs or [],
            start_mcp_loading=not defer_mcp_loading,
        )
        _phase_timings_ms["mcp_ms"] = int((time.monotonic() - _phase_t) * 1000)

        if runtime.config.best_practices_always:
            from pythinker_code.prompts import apply_always_on_best_practices

            agent = dataclasses.replace(
                agent,
                system_prompt=apply_always_on_best_practices(agent.system_prompt, enabled=True),
            )

        if startup_progress is not None:
            startup_progress("Restoring conversation...")
        context = Context(session.context_file)
        await context.restore()

        if context.system_prompt is not None:
            from pythinker_code.scratchpad import refresh_system_prompt_scratchpad_section

            refreshed_system_prompt = refresh_system_prompt_scratchpad_section(
                context.system_prompt,
                runtime.builtin_args.PYTHINKER_SCRATCHPAD_SECTION,
            )
            agent = dataclasses.replace(agent, system_prompt=refreshed_system_prompt)
        else:
            await context.write_system_prompt(agent.system_prompt)

        soul = PythinkerSoul(agent, context=context)

        from pythinker_code.memory.recall import RecallInjectionProvider
        from pythinker_code.project_memory import ProjectMemoryStore

        soul.add_injection_provider(
            RecallInjectionProvider(ProjectMemoryStore(session.work_dir), session)
        )

        # Activate plan mode if requested (for new sessions or --plan flag)
        if plan_mode and not soul.plan_mode:
            await soul.set_plan_mode_from_manual(True)
        elif plan_mode and soul.plan_mode:
            # Already in plan mode from restored session, trigger activation reminder
            soul.schedule_plan_activation_reminder()

        # Create and inject hook engine. Enabled plugins contribute lifecycle
        # hooks; config hooks come first so a project hook is never shadowed.
        from pythinker_code.hooks.engine import HookEngine
        from pythinker_code.plugin.integration import plugin_hook_defs

        hook_engine = HookEngine([*config.hooks, *plugin_hook_defs()], cwd=str(session.work_dir))
        if config.disabled_project_hooks:
            # The load-time logger.warning only reaches shell users; publish a
            # notification so web/ACP frontends also learn why their project
            # hooks did not run and how to enable them.
            from pythinker_code.notifications.models import NotificationEvent

            runtime.notifications.publish(
                NotificationEvent(
                    id=f"project-hooks-disabled:{session.id}",
                    category="system",
                    type="project_hooks_disabled",
                    source_kind="config",
                    source_id="project_trust",
                    title="Project hooks disabled (untrusted project)",
                    body=(
                        "Hooks defined in "
                        + ", ".join(config.disabled_project_hooks)
                        + " are disabled until you trust this project. Run /trust to "
                        "enable them (takes effect on /reload or next start)."
                    ),
                    severity="warning",
                    dedupe_key=f"project-hooks-disabled:{session.id}",
                )
            )
        soul.set_hook_engine(hook_engine)
        runtime.hook_engine = hook_engine

        # --- Initialize telemetry ---
        from pythinker_code.telemetry import attach_sink, set_context
        from pythinker_code.telemetry import disable as disable_telemetry
        from pythinker_code.telemetry.config import is_enabled as telemetry_is_enabled

        telemetry_disabled = not telemetry_is_enabled(config_telemetry=config.telemetry)
        if telemetry_disabled:
            disable_telemetry()
        else:
            device_id = get_device_id()
            set_context(device_id=device_id, session_id=session.id)

            # Initialize Sentry/Bugsink (errors) and OTel SDK (traces + logs)
            # before the in-process EventSink picks up its first event.
            from pythinker_code.telemetry import otel as _otel
            from pythinker_code.telemetry import sentry as _sentry

            _sentry.init(
                version=VERSION,
                device_id=device_id,
                extra_tags={
                    "ui_mode": ui_mode,
                    "model": model.model if model else "",
                },
            )
            _otel.init(version=VERSION, ui_mode=ui_mode, device_id=device_id)

            from pythinker_code.telemetry.sink import EventSink

            sink = EventSink(
                version=VERSION,
                model=model.model if model else "",
                ui_mode=ui_mode,
            )
            attach_sink(sink)

        from pythinker_code.telemetry import track, track_session_started_once
        from pythinker_code.telemetry.crash import install_asyncio_handler, set_phase

        # App init finished — enter runtime phase and hook asyncio crashes.
        install_asyncio_handler()
        set_phase("runtime")

        if ui_mode != "wire":
            track_session_started_once(ui_mode=ui_mode, resumed=resumed)
        track(
            "started",
            resumed=resumed,
            yolo=runtime.approval.is_yolo(),
            auto=runtime.approval.is_auto(),
        )
        track(
            "startup_perf",
            duration_ms=int((time.monotonic() - _create_t0) * 1000),
            config_ms=_phase_timings_ms.get("config_ms", 0),
            init_ms=_phase_timings_ms.get("init_ms", 0),
            mcp_ms=_phase_timings_ms.get("mcp_ms", 0),
        )

        return PythinkerCLI(soul, runtime, env_overrides, bg_refresh_task)

    def __init__(
        self,
        _soul: PythinkerSoul,
        _runtime: Runtime,
        _env_overrides: dict[str, str],
        _bg_refresh_task: asyncio.Task[None] | None = None,
    ) -> None:
        self._soul = _soul
        self._runtime = _runtime
        self._env_overrides = _env_overrides
        self._bg_refresh_task = _bg_refresh_task

    @property
    def soul(self) -> PythinkerSoul:
        """Get the PythinkerSoul instance."""
        return self._soul

    @property
    def session(self) -> Session:
        """Get the Session instance."""
        return self._runtime.session

    async def cleanup_runtime_resources(self) -> None:
        """Cleanup per-CLI-instance resources without stopping persisted background tasks."""
        # Cancel the startup managed-model refresh task if it is still running
        # so it does not outlive this CLI instance across reloads.
        if self._bg_refresh_task is not None and not self._bg_refresh_task.done():
            self._bg_refresh_task.cancel()

        # Cleanup MCP connections held by this instance's toolset. Background
        # task workers are persisted elsewhere and intentionally left alone.
        from pythinker_code.soul.toolset import PythinkerToolset

        toolset = self._soul.agent.toolset
        if isinstance(toolset, PythinkerToolset):
            try:
                await toolset.cleanup()
            except Exception:
                logger.exception("Failed to cleanup MCP toolset during reload")

        if self._runtime.lsp is not None:
            try:
                await self._runtime.lsp.shutdown()
            except Exception:
                logger.exception("Failed to shutdown LSP service during reload")

    async def shutdown_background_tasks(self) -> None:
        """Kill active background tasks on exit, unless keep_alive_on_exit is configured.

        Prints a stderr notice naming each task so the user knows what is being
        terminated, waits out the configured kill grace period so SIGTERM can
        take effect, then reconciles and reports any workers that ignored the
        signal.

        This runs on the CLI's hard-shutdown path, so every failure mode must
        be contained: disk IO errors from ``list_tasks`` / ``reconcile`` or
        store corruption must not propagate and replace the real exit code
        with a traceback.
        """
        await self.cleanup_runtime_resources()

        bg_config = self._runtime.config.background
        if bg_config.keep_alive_on_exit:
            return

        try:
            manager = self._runtime.background_tasks
            active_views = [
                v
                for v in manager.list_tasks(status=None, limit=None)
                if not is_terminal_status(v.runtime.status)
            ]
            if not active_views:
                return

            # Split by whether the task has already been kill-requested (e.g.
            # by the ``--print`` timeout path which ran immediately before
            # this shutdown).  For those:
            #   - don't re-announce on stderr (user saw the timeout notice)
            #   - don't re-kill with a generic reason, which would overwrite
            #     the more specific ``kill_reason`` on disk
            # We still reconcile + grace-wait for them so they reach terminal
            # status before the process exits.
            fresh_targets = [v for v in active_views if v.control.kill_requested_at is None]

            if fresh_targets:
                # Build and emit the kill notice via ``open_original_stderr``
                # — ``sys.stderr.write`` alone would silently land in
                # ``pythinker.log`` because ``redirect_stderr_to_logger`` has
                # replaced fd=2 with a pipe into the logger by this point.
                lines = [f"\u26a0  Killing {len(fresh_targets)} background tasks:\n"]
                for view in fresh_targets:
                    description = view.spec.description or ""
                    if len(description) > 60:
                        description = description[:57] + "..."
                    lines.append(f"  {view.spec.id}  {description}\n")
                _write_original_stderr("".join(lines))

                killed: list[str] = []
                for view in fresh_targets:
                    try:
                        manager.kill(view.spec.id, reason="CLI session ended")
                        killed.append(view.spec.id)
                    except Exception:
                        logger.exception(
                            "Failed to kill task {task_id} during shutdown",
                            task_id=view.spec.id,
                        )
                if killed:
                    logger.info(
                        "Stopped {n} background task(s) on exit: {ids}",
                        n=len(killed),
                        ids=killed,
                    )

            await asyncio.sleep(bg_config.kill_grace_period_ms / 1000)
            manager.reconcile()
            survivors = [
                v
                for v in manager.list_tasks(status=None, limit=None)
                if not is_terminal_status(v.runtime.status)
            ]
            if survivors:
                # Distinguish "worker is mid-shutdown" (kill request on record,
                # SIGTERM delivered, worker just hasn't written terminal state
                # yet) from a genuine leak (never got kill-requested, i.e.
                # ``manager.kill`` raised).  Without this split, users saw
                # ``killed N`` from the --print timeout path immediately
                # followed by ``(N tasks still alive)`` here — a direct
                # semantic contradiction.
                terminating = [s for s in survivors if s.control.kill_requested_at is not None]
                leaking = [s for s in survivors if s.control.kill_requested_at is None]
                # Report leaks first — ``stop request failed`` is strictly
                # more severe than ``still terminating`` (the latter will
                # resolve on its own once the worker writes terminal state).
                if leaking:
                    _write_original_stderr(
                        f"  ({len(leaking)} tasks still running; stop request failed)\n"
                    )
                if terminating:
                    _write_original_stderr(f"  ({len(terminating)} tasks still terminating)\n")
        except Exception:
            logger.warning("Error during background task shutdown; continuing exit", exc_info=True)

    async def await_bg_tasks_shutdown(self, timeout: float = 2.0) -> None:
        """Await completion of the model-refresh background task after cancellation."""
        task = self._bg_refresh_task
        if task is None or task.done():
            return
        # Best-effort cleanup — errors inside the task are already logged.
        with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    @contextlib.asynccontextmanager
    async def _env(self) -> AsyncGenerator[None]:
        async with _CWD_LOCK:
            original_cwd = HostPath.cwd()
            await pythinker_host.chdir(self._runtime.work_dir)
            try:
                # to ignore possible warnings from dateparser
                warnings.filterwarnings("ignore", category=DeprecationWarning)
                async with self._runtime.oauth.refreshing(self._runtime):
                    yield
            finally:
                await pythinker_host.chdir(original_cwd)

    async def run(
        self,
        user_input: str | list[ContentPart],
        cancel_event: asyncio.Event,
        merge_wire_messages: bool = False,
    ) -> AsyncGenerator[WireMessage]:
        """
        Run the Pythinker CLI instance without any UI and yield Wire messages directly.

        Args:
            user_input (str | list[ContentPart]): The user input to the agent.
            cancel_event (asyncio.Event): An event to cancel the run.
            merge_wire_messages (bool): Whether to merge Wire messages as much as possible.

        Yields:
            WireMessage: The Wire messages from the `PythinkerSoul`.

        Raises:
            LLMNotSet: When the LLM is not set.
            LLMNotSupported: When the LLM does not have required capabilities.
            ChatProviderError: When the LLM provider returns an error.
            MaxStepsReached: When the maximum number of steps is reached.
            RunCancelled: When the run is cancelled by the cancel event.
        """
        async with self._env():
            wire_future = asyncio.Future[WireUISide]()
            stop_ui_loop = asyncio.Event()
            approval_bridge_tasks: dict[str, asyncio.Task[None]] = {}
            forwarded_approval_requests: dict[str, ApprovalRequest] = {}

            async def _bridge_approval_request(request: ApprovalRequest) -> None:
                try:
                    response = await request.wait()
                    assert self._runtime.approval_runtime is not None
                    self._runtime.approval_runtime.resolve(
                        request.id, response, feedback=request.feedback
                    )
                finally:
                    approval_bridge_tasks.pop(request.id, None)
                    forwarded_approval_requests.pop(request.id, None)

            def _forward_approval_request(wire: Wire, request: ApprovalRequest) -> None:
                if request.id in forwarded_approval_requests:
                    return
                forwarded_approval_requests[request.id] = request
                if request.id not in approval_bridge_tasks:
                    approval_bridge_tasks[request.id] = asyncio.create_task(
                        _bridge_approval_request(request)
                    )
                wire.soul_side.send(request)

            async def _ui_loop_fn(wire: Wire) -> None:
                wire_future.set_result(wire.ui_side(merge=merge_wire_messages))
                assert self._runtime.root_wire_hub is not None
                assert self._runtime.approval_runtime is not None
                root_hub_queue = self._runtime.root_wire_hub.subscribe()
                stop_task = asyncio.create_task(stop_ui_loop.wait())
                queue_task = asyncio.create_task(root_hub_queue.get())
                try:
                    for pending in self._runtime.approval_runtime.list_pending():
                        _forward_approval_request(
                            wire,
                            ApprovalRequest(
                                id=pending.id,
                                tool_call_id=pending.tool_call_id,
                                sender=pending.sender,
                                action=pending.action,
                                description=pending.description,
                                display=pending.display,
                                source_kind=pending.source.kind,
                                source_id=pending.source.id,
                                agent_id=pending.source.agent_id,
                                subagent_type=pending.source.subagent_type,
                            ),
                        )
                    while True:
                        done, _ = await asyncio.wait(
                            [stop_task, queue_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if stop_task in done:
                            break
                        try:
                            msg = queue_task.result()
                        except QueueShutDown:
                            break
                        match msg:
                            case ApprovalRequest() as request:
                                _forward_approval_request(wire, request)
                                queue_task = asyncio.create_task(root_hub_queue.get())
                                continue
                            case ApprovalResponse() as response:
                                if (
                                    request := forwarded_approval_requests.get(response.request_id)
                                ) and not request.resolved:
                                    request.resolve(response.response, response.feedback)
                            case _:
                                pass
                        wire.soul_side.send(msg)
                        queue_task = asyncio.create_task(root_hub_queue.get())
                finally:
                    stop_task.cancel()
                    queue_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stop_task
                    with contextlib.suppress(asyncio.CancelledError):
                        await queue_task
                    for task in list(approval_bridge_tasks.values()):
                        task.cancel()
                    for task in list(approval_bridge_tasks.values()):
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                    approval_bridge_tasks.clear()
                    forwarded_approval_requests.clear()
                    assert self._runtime.root_wire_hub is not None
                    self._runtime.root_wire_hub.unsubscribe(root_hub_queue)

            run_cancel_event = asyncio.Event()

            async def _mirror_external_cancel() -> None:
                await cancel_event.wait()
                run_cancel_event.set()

            external_cancel_task = asyncio.create_task(
                _mirror_external_cancel(),
                name="cancel-event-mirror",
            )
            soul_task = asyncio.create_task(
                run_soul(
                    self.soul,
                    user_input,
                    _ui_loop_fn,
                    run_cancel_event,
                    runtime=self._runtime,
                )
            )

            wire_shut_down = False
            try:
                wire_ui = await wire_future
                while True:
                    msg = await wire_ui.receive()
                    yield msg
            except QueueShutDown:
                wire_shut_down = True
                pass
            finally:
                # stop consuming Wire messages
                stop_ui_loop.set()
                cleanup_cancelled_run = False
                if not wire_shut_down and not soul_task.done() and not cancel_event.is_set():
                    cleanup_cancelled_run = True
                    run_cancel_event.set()
                # wait for the soul task to finish, or raise
                try:
                    await soul_task
                except RunCancelled:
                    if not cleanup_cancelled_run:
                        raise
                finally:
                    external_cancel_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await external_cancel_task

    async def run_shell(
        self, command: str | None = None, *, prefill_text: str | None = None
    ) -> bool:
        """Run the Pythinker CLI instance with shell UI."""
        from pythinker_code.ui.shell import Shell, WelcomeInfoItem

        work_dir = self._runtime.work_dir
        welcome_info = [
            WelcomeInfoItem(name="Directory", value=str(shorten_home(work_dir))),
        ]
        branch_name = _safe_git_branch(work_dir)
        if branch_name:
            welcome_info.append(WelcomeInfoItem(name="Branch", value=branch_name))
        if notice := _resumed_unsupervised_notice(
            resumed=self._runtime.resumed,
            yolo=self._runtime.approval.is_yolo(),
            auto=self._runtime.approval.is_auto(),
        ):
            welcome_info.append(
                WelcomeInfoItem(name="Mode", value=notice, level=WelcomeInfoItem.Level.WARN)
            )
        if base_url := self._env_overrides.get("PYTHINKER_BASE_URL"):
            welcome_info.append(
                WelcomeInfoItem(
                    name="API URL",
                    value=f"{base_url} (from PYTHINKER_BASE_URL)",
                    level=WelcomeInfoItem.Level.WARN,
                )
            )
        if self._env_overrides.get("PYTHINKER_API_KEY"):
            welcome_info.append(
                WelcomeInfoItem(
                    name="API Key",
                    value="****** (from PYTHINKER_API_KEY)",
                    level=WelcomeInfoItem.Level.WARN,
                )
            )
        if not self._runtime.llm:
            welcome_info.append(
                WelcomeInfoItem(
                    name="Model",
                    value="not set — type /login to sign in",
                    level=WelcomeInfoItem.Level.WARN,
                )
            )
        elif "PYTHINKER_MODEL_NAME" in self._env_overrides:
            welcome_info.append(
                WelcomeInfoItem(
                    name="Model",
                    value=f"{self._soul.model_name} (from PYTHINKER_MODEL_NAME)",
                    level=WelcomeInfoItem.Level.WARN,
                )
            )
        else:
            welcome_info.append(
                WelcomeInfoItem(
                    name="Model",
                    value=model_display_name(
                        self._soul.model_name,
                        self._runtime.llm.model_config if self._runtime.llm else None,
                    ),
                    level=WelcomeInfoItem.Level.INFO,
                )
            )
            model_name = self._soul.model_name
            if model_name not in (
                "pythinker-for-coding",
                "pythinker-code",
            ) and not model_name.startswith("pythinker-ai"):
                welcome_info.append(
                    WelcomeInfoItem(
                        name="Tip",
                        value="Type /login to use Pythinker for Coding.",
                        level=WelcomeInfoItem.Level.WARN,
                    )
                )
        # Session persistence details come last — workspace and model identity
        # read first, storage internals stay at the bottom of the facts block.
        welcome_info.append(WelcomeInfoItem(name="Session", value=self._runtime.session.id))
        try:
            auto_save_path = str(
                shorten_home(HostPath.unsafe_from_local_path(self._runtime.session.context_file))
            )
        except Exception:
            logger.debug("Failed to compute auto-save display path", exc_info=True)
            auto_save_path = ""
        if auto_save_path:
            welcome_info.append(WelcomeInfoItem(name="Auto-save", value=auto_save_path))
        welcome_info.append(
            WelcomeInfoItem(
                name="Tip",
                value=(
                    'Review first: try "review this diff", "scan for vulnerabilities",'
                    ' or "find the root cause" before editing.'
                ),
                level=WelcomeInfoItem.Level.INFO,
            )
        )
        # Repos without agent guidance benefit most from /init — surface the
        # tip emphasized (WARN renders bold) only when neither file exists.
        try:
            has_agent_docs = (
                await (work_dir / "AGENTS.md").exists() or await (work_dir / "CLAUDE.md").exists()
            )
        except OSError:
            has_agent_docs = True
        if not has_agent_docs:
            welcome_info.append(
                WelcomeInfoItem(
                    name="Tip",
                    value="No AGENTS.md/CLAUDE.md found — run /init to generate one.",
                    level=WelcomeInfoItem.Level.WARN,
                )
            )
        welcome_info.append(
            WelcomeInfoItem(
                name="Tip",
                value="Feedback or bug? Type /feedback in this session.",
                level=WelcomeInfoItem.Level.INFO,
            )
        )
        async with self._env():
            shell = Shell(self._soul, welcome_info=welcome_info, prefill_text=prefill_text)
            return await shell.run(command)

    async def run_print(
        self,
        input_format: InputFormat,
        output_format: OutputFormat,
        command: str | None = None,
        *,
        final_only: bool = False,
    ) -> int:
        """Run the Pythinker CLI instance with print UI."""
        from pythinker_code.ui.print import Print

        async with self._env():
            print_ = Print(
                self._soul,
                input_format,
                output_format,
                self._runtime.session.context_file,
                final_only=final_only,
            )
            return await print_.run(command)

    async def run_acp(self) -> None:
        """Run the Pythinker CLI instance as ACP server."""
        from pythinker_code.ui.acp import ACP

        async with self._env():
            acp = ACP(self._soul)
            await acp.run()

    async def run_wire_stdio(self) -> None:
        """Run the Pythinker CLI instance as Wire server over stdio."""
        from pythinker_code.wire.server import WireServer

        async with self._env():
            server = WireServer(self._soul)
            await server.serve()
