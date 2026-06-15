import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, override

from pydantic import BaseModel, Field
from pythinker_core.tooling import CallableTool2, ToolError, ToolReturnValue

from pythinker_code.execution_profiles import resolve_execution_policy
from pythinker_code.soul.agent import Runtime
from pythinker_code.soul.toolset import get_current_tool_call_or_none
from pythinker_code.subagents.codenames import generate_codename, is_generic_agent_name
from pythinker_code.subagents.models import AgentLaunchSpec, AgentTypeDefinition
from pythinker_code.subagents.runner import (
    ForegroundRunRequest,
    ForegroundSubagentRunner,
    busy_resume_message,
)
from pythinker_code.subagents.usage import aggregate_findings, summarize_batch
from pythinker_code.tools.utils import ToolResultStatus, load_desc, tool_status_line
from pythinker_code.utils.logging import logger
from pythinker_code.wire.types import MCPStatusSnapshot, SubagentToolFallback


def _missing_required_mcp_servers(
    required: Sequence[str], snapshot: MCPStatusSnapshot | None
) -> list[str]:
    """Required MCP servers that are settled-and-not-connected.

    Returns ``[]`` when nothing is required or MCP is still loading (a required server may
    yet connect — do not reject prematurely). Once loading has settled, a required server
    that is not connected (absent or failed) is reported as missing.
    """
    if not required:
        return []
    if snapshot is not None and snapshot.loading:
        return []
    connected: set[str] = (
        {s.name for s in snapshot.servers if s.status == "connected"} if snapshot else set()
    )
    return [name for name in required if name not in connected]


def _emit_subagent_tool_fallback(
    *,
    reason: Literal[
        "unavailable_agent_type",
        "mcp_unavailable",
        "policy_denied",
        "timeout",
        "exception",
    ],
    requested_type: str,
    runtime: Runtime,
) -> None:
    try:
        from pythinker_code.soul import get_wire_or_none

        if wire := get_wire_or_none():
            wire.soul_side.send(
                SubagentToolFallback(
                    reason=reason,
                    requested_type=requested_type,
                    available_types=tuple(sorted(runtime.labor_market.builtin_types)),
                )
            )
    except Exception as exc:  # noqa: BLE001 - observability must not break Agent tool errors
        logger.debug(
            "Failed to emit subagent fallback event: {type} ({reason}): {error}",
            type=requested_type,
            reason=reason,
            error=exc,
        )


NAME = "Agent"

MAX_FOREGROUND_TIMEOUT = 60 * 60  # 1 hour
MAX_BACKGROUND_TIMEOUT = 60 * 60  # 1 hour


class Params(BaseModel):
    description: str = Field(description="A short (3-5 word) description of the task")
    prompt: str = Field(
        description=(
            "The task for the agent to perform. Include a single goal, relevant context/evidence, "
            "scope boundaries, constraints, expected output format, and verification criteria."
        )
    )
    subagent_type: str = Field(
        default="coder",
        description="The built-in agent type to use. Defaults to `coder`.",
    )
    model: str | None = Field(
        default=None,
        description=(
            "Optional model override. Selection priority is: this parameter, then the built-in "
            "type default model, then the parent agent's current model."
        ),
    )
    resume: str | None = Field(
        default=None,
        description="Optional agent ID to resume instead of creating a new instance.",
    )
    fork_context: bool = Field(
        default=False,
        description=(
            "Seed the new agent with a filtered transcript of this conversation (user "
            "requests and assistant replies; tool traffic and thinking are dropped). Use "
            "when the child needs the discussion so far without a hand-written context "
            "packet. New foreground instances only — invalid with resume or "
            "run_in_background."
        ),
    )
    run_in_background: bool = Field(
        default=False,
        description=(
            "Whether to run the agent in the background. Prefer false unless the task can "
            "continue independently and there is a clear benefit to returning control before "
            "the result is needed."
        ),
    )
    timeout: int | None = Field(
        default=None,
        description=(
            "Timeout in seconds for the agent task. "
            "Foreground: no default timeout (runs until completion), max 3600s (1hr). "
            "Background: default from config (1hr), max 3600s (1hr). "
            "For thorough large-codebase exploration, pass an explicit longer timeout near "
            "the max and scope the prompt narrowly. The agent is stopped if it exceeds "
            "this limit."
        ),
        ge=30,
        le=MAX_BACKGROUND_TIMEOUT,
    )
    dependencies: list[str] = Field(
        default_factory=list,
        description=(
            "Optional background task IDs this task depends on. Metadata only; the parent "
            "agent should launch dependent tasks after prerequisites are ready."
        ),
    )
    budget_seconds: int | None = Field(
        default=None,
        description="Optional budget in seconds for planning/synthesis metadata.",
        ge=1,
        le=MAX_BACKGROUND_TIMEOUT,
    )
    isolation: Literal["none", "worktree"] = Field(
        default="none",
        description=(
            "Optional isolation for background agents. `worktree` runs a write-profile child "
            "in its own git worktree of HEAD; its final report names the worktree path and a "
            "diff summary so changes are merged deliberately (clean worktrees are removed). "
            "Requires a git repository; ignored for read-profile child types."
        ),
    )

    @property
    def effective_timeout(self) -> int | None:
        """Return the user-specified timeout, or None to use the system default."""
        return self.timeout


class AgentRunConfig(BaseModel):
    name: str = Field(description="Stable short name for this child agent")
    prompt: str = Field(
        description=(
            "Agent-specific task prompt. Keep it to one objective and include the child's "
            "scope, evidence to use, expected output contract, and verification criteria."
        )
    )
    title: str | None = Field(
        default=None,
        description="Optional 3-5 word display title. Defaults to name.",
    )
    subagent_type: str = Field(
        default="coder",
        description="Built-in agent type for this child agent.",
    )


class RunAgentsParams(BaseModel):
    summary: str = Field(description="Short summary of the multi-agent run")
    base_prompt: str = Field(
        default="",
        description=(
            "Shared context prepended to every child prompt. Include the overall goal, "
            "repo constraints, known evidence, excluded scope, and shared output requirements."
        ),
    )
    agents: list[AgentRunConfig] = Field(
        description=(
            "Child agents to launch with shared base_prompt plus their own prompt. Each child "
            "should have exactly one objective; split unrelated work into separate children. "
            "For background runs, oversized batches launch only the fitting prefix and "
            "report remaining children as deferred."
        ),
        min_length=1,
        max_length=8,
    )
    model: str | None = Field(
        default=None,
        description="Optional model override applied to every child agent.",
    )
    run_in_background: bool = Field(
        default=True,
        description=(
            "Foreground (false) runs children concurrently and returns all results inline — "
            "prefer it when your only next step is to synthesize the results. Background "
            "(true) returns immediately with task ids; use it only when you have other work "
            "to do while children run."
        ),
    )
    timeout: int | None = Field(
        default=None,
        description="Optional per-agent timeout in seconds.",
        ge=30,
        le=MAX_BACKGROUND_TIMEOUT,
    )
    isolation: Literal["none", "worktree"] = Field(
        default="none",
        description=(
            "Optional isolation for background child agents. `worktree` gives each "
            "write-profile child its own git worktree of HEAD so parallel children "
            "cannot clobber each other; each child's report names its worktree and "
            "diff summary for deliberate merging."
        ),
    )


class AgentTool(CallableTool2[Params]):
    name: str = NAME
    params: type[Params] = Params

    def __init__(self, runtime: Runtime):
        super().__init__(
            description=load_desc(
                Path(__file__).parent / "description.md",
                {
                    "BUILTIN_AGENT_TYPES_MD": self._builtin_type_lines(runtime),
                },
            )
        )
        self._runtime = runtime

    @staticmethod
    def _builtin_type_lines(runtime: Runtime) -> str:
        lines: list[str] = []
        for name, type_def in runtime.labor_market.builtin_types.items():
            tool_names = AgentTool._tool_summary(type_def)
            model = type_def.default_model or "inherit"
            suffix = (
                f" When to use: {AgentTool._normalize_summary(type_def.when_to_use)}"
                if type_def.when_to_use
                else ""
            )
            background = "yes" if type_def.supports_background else "no"
            lines.append(
                f"- `{name}`: {type_def.description} "
                f"(Tools: {tool_names}, Model: {model}, Background: {background}).{suffix}"
            )
        return "\n".join(lines)

    @staticmethod
    def _normalize_summary(text: str) -> str:
        return " ".join(text.split())

    @staticmethod
    def _tool_summary(type_def: AgentTypeDefinition) -> str:
        if type_def.tool_policy.mode != "allowlist":
            return "*"
        if not type_def.tool_policy.tools:
            return "(none)"
        return ", ".join(AgentTool._unique_tool_names(type_def.tool_policy.tools))

    @staticmethod
    def _unique_tool_names(tool_paths: tuple[str, ...]) -> list[str]:
        names: list[str] = []
        for path in tool_paths:
            name = path.split(":")[-1]
            if name not in names:
                names.append(name)
        return names

    async def _journal_foreground_agent_start(self, params: Params, actual_type: str) -> None:
        from pythinker_code.scratchpad import append_scratch_event

        details = [
            "mode: foreground",
            f"type: {actual_type}",
            f"description: {params.description.strip()}",
        ]
        if params.resume:
            details.append(f"resume: {params.resume}")
        await append_scratch_event(
            self._runtime.work_dir,
            session_id=self._runtime.session.id,
            session_title=self._runtime.session.title or self._runtime.session.state.custom_title,
            labels=["kind:agent", f"agent-type:{actual_type}"],
            title="agent started",
            details=details,
        )

    def check_required_mcp_servers(self, requested_type: str) -> ToolError | None:
        """Reject a fresh spawn when the agent type's required MCP servers are absent.

        Returns ``None`` (allow) when nothing is required, the type is unknown (the normal
        type-validation path reports that), or MCP is still loading. Unconfigured/failed
        required servers once loading settles are surfaced so the model can self-correct.
        """
        type_def = self._runtime.labor_market.get_builtin_type(requested_type)
        if type_def is None or not type_def.required_mcp_servers:
            return None
        snapshot = self._runtime.mcp_status() if self._runtime.mcp_status is not None else None
        missing = _missing_required_mcp_servers(type_def.required_mcp_servers, snapshot)
        if not missing:
            return None
        return ToolError(
            message=(
                f"Agent type '{requested_type}' requires MCP server(s) not available: "
                f"{', '.join(missing)}. Add a missing server with `pythinker mcp add` "
                f"(or `pythinker mcp auth <server>` if it is configured but unauthorized), "
                f"or choose a different agent type."
            ),
            brief="Required MCP server unavailable",
        )

    def check_execution_policy(self, subagent_type: str) -> ToolError | None:
        policy = resolve_execution_policy(
            self._runtime.config.agent_execution_profile,
            yolo=self._runtime.approval.is_yolo_flag(),
        )
        if policy.subagents == "deny":
            return ToolError(
                message="Subagents are denied by the active execution profile.",
                brief="Execution profile restriction",
            )
        if (
            policy.allowed_subagent_types is not None
            and subagent_type not in policy.allowed_subagent_types
        ):
            return ToolError(
                message=(
                    f"Subagent type `{subagent_type}` is not allowed by the active execution "
                    f"profile `{self._runtime.config.agent_execution_profile}`."
                ),
                brief="Execution profile restriction",
            )
        return None

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        if self._runtime.role != "root":
            return ToolError(
                message="Subagents cannot launch other subagents.",
                brief="Agent unavailable",
            )
        if params.model is not None and params.model not in self._runtime.config.models:
            return ToolError(
                message=f"Unknown model alias: {params.model}",
                brief="Invalid model alias",
            )
        requested_type = params.subagent_type or "coder"
        if err := self.check_execution_policy(requested_type):
            _emit_subagent_tool_fallback(
                reason="policy_denied",
                requested_type=requested_type,
                runtime=self._runtime,
            )
            return err
        # Gate a FRESH spawn on the agent type's required MCP servers (resume is not a
        # fresh spawn — the instance already exists, so it is not re-gated).
        if params.resume is None and (err := self.check_required_mcp_servers(requested_type)):
            _emit_subagent_tool_fallback(
                reason="mcp_unavailable",
                requested_type=requested_type,
                runtime=self._runtime,
            )
            return err
        if params.fork_context and (params.resume is not None or params.run_in_background):
            return ToolError(
                message=(
                    "fork_context seeds a NEW foreground agent; it cannot be combined "
                    "with resume or run_in_background."
                ),
                brief="Invalid fork_context",
            )
        if not params.run_in_background and params.isolation != "none":
            # Proceeding unisolated after an isolation request would present
            # degraded behavior as authoritative; fail fast instead.
            return ToolError(
                message=(
                    "isolation='worktree' is only supported for background agents; "
                    "set run_in_background=true or drop isolation."
                ),
                brief="Invalid isolation",
            )
        if params.run_in_background:
            return await self._run_in_background(params)
        await self._journal_foreground_agent_start(params, requested_type)
        timeout = params.effective_timeout
        try:
            runner = ForegroundSubagentRunner(self._runtime)
            req = ForegroundRunRequest(
                description=params.description,
                prompt=params.prompt,
                requested_type=params.subagent_type or "coder",
                model=params.model,
                resume=params.resume,
                fork_context=params.fork_context,
            )
            if timeout is not None:
                return await asyncio.wait_for(runner.run(req), timeout=timeout)
            return await runner.run(req)
        except TimeoutError as exc:
            _emit_subagent_tool_fallback(
                reason="timeout",
                requested_type=requested_type,
                runtime=self._runtime,
            )
            # Note: TimeoutError from run_soul internals (e.g. aiohttp) is now caught
            # by run_soul_checked and converted to SoulRunFailure. This handler mainly
            # covers wait_for's task-level timeout and pre-run_soul TimeoutErrors.
            if isinstance(exc.__cause__, asyncio.CancelledError):
                logger.warning("Foreground agent timed out after {t}s", t=timeout)
                return ToolError(
                    message=f"Agent timed out after {timeout}s.",
                    brief=f"Agent timed out ({timeout}s)",
                )
            # Internal timeout (e.g. aiohttp request) — treat as generic failure
            logger.exception("Foreground agent run failed")
            return ToolError(message=f"Failed to run agent: {exc}", brief="Agent failed")
        except FileNotFoundError as exc:
            logger.warning("Foreground agent resume target was not found: {err}", err=exc)
            return ToolError(message=str(exc), brief="Agent not found")
        except ValueError as exc:
            # Malformed resume id (store.instance_dir validates [A-Za-z0-9_-]{1,64}).
            logger.warning("Foreground agent resume id was malformed: {err}", err=exc)
            return ToolError(message=str(exc), brief="Agent not found")
        except RuntimeError as exc:
            if "cannot be resumed concurrently" in str(exc):
                logger.warning("Foreground agent resume rejected: {err}", err=exc)
                return ToolError(message=str(exc), brief="Agent already running")
            from pythinker_code.telemetry.errors import report_handled_error

            report_handled_error(exc, site="tool.agent.foreground", tool="Agent")
            logger.exception("Foreground agent run failed")
            return ToolError(message=f"Failed to run agent: {exc}", brief="Agent failed")
        except KeyError as exc:
            # Hallucinated subagent type: routine model error, not a crash —
            # name the valid types so the model can self-correct.
            _emit_subagent_tool_fallback(
                reason="unavailable_agent_type",
                requested_type=requested_type,
                runtime=self._runtime,
            )
            return ToolError(
                message=(
                    f"{exc.args[0] if exc.args else exc}. Available types: "
                    f"{', '.join(sorted(self._runtime.labor_market.builtin_types))}."
                ),
                brief="Invalid subagent type",
            )
        except Exception as exc:
            from pythinker_code.telemetry.errors import report_handled_error

            report_handled_error(exc, site="tool.agent.foreground", tool="Agent")
            logger.exception("Foreground agent run failed")
            _emit_subagent_tool_fallback(
                reason="exception",
                requested_type=requested_type,
                runtime=self._runtime,
            )
            return ToolError(message=f"Failed to run agent: {exc}", brief="Agent failed")

    async def _run_in_background(self, params: Params) -> ToolReturnValue:
        assert self._runtime.subagent_store is not None
        try:
            tool_call = get_current_tool_call_or_none()
            if tool_call is None:
                return ToolError(
                    message="Background agent requires a tool call context.",
                    brief="No tool call context",
                )

            requested_type = params.subagent_type or "coder"
            if params.resume:
                record = self._runtime.subagent_store.require_instance(params.resume)
                task_view = None
                if record.status == "running_background":
                    task_view = self._runtime.background_tasks.reconcile_stale_agent_record(
                        record.agent_id
                    )
                    record = self._runtime.subagent_store.require_instance(params.resume)
                if record.status in {"running_foreground", "running_background"}:
                    return ToolError(
                        message=busy_resume_message(record, task_view),
                        brief="Agent already running",
                    )
                actual_type = record.subagent_type
                agent_id = record.agent_id
                # Validate the effective model for resumed instances — the model
                # stored in the launch spec may have been removed from config since
                # the instance was created.  params.model is already validated in
                # __call__, so only check the stored effective_model fallback here.
                if params.model is None:
                    type_def = self._runtime.labor_market.require_builtin_type(actual_type)
                    effective = record.launch_spec.effective_model or type_def.default_model
                    if effective is not None and effective not in self._runtime.config.models:
                        return ToolError(
                            message=f"Unknown model alias: {effective}",
                            brief="Invalid model alias",
                        )
            else:
                actual_type = requested_type
                import uuid

                agent_id = f"a{uuid.uuid4().hex[:8]}"
                record = None

            created_instance = False
            if not params.resume:
                type_def = self._runtime.labor_market.require_builtin_type(actual_type)
                self._runtime.subagent_store.create_instance(
                    agent_id=agent_id,
                    description=params.description.strip(),
                    launch_spec=AgentLaunchSpec(
                        agent_id=agent_id,
                        subagent_type=actual_type,
                        model_override=params.model,
                        effective_model=params.model or type_def.default_model,
                        thinking=self._runtime.llm.thinking
                        if self._runtime.llm is not None
                        else None,
                        thinking_effort=(
                            self._runtime.llm.thinking_effort
                            if self._runtime.llm is not None
                            else None
                        ),
                        parent_agent_id=self._runtime.subagent_id,
                    ),
                )
                created_instance = True

            # Mark running_background synchronously before dispatching the
            # async task so that concurrent resume attempts see the guard
            # immediately (asyncio.create_task only queues the coroutine).
            self._runtime.subagent_store.update_instance(
                agent_id,
                status="running_background",
            )
            try:
                view = self._runtime.background_tasks.create_agent_task(
                    agent_id=agent_id,
                    subagent_type=actual_type,
                    prompt=params.prompt,
                    description=params.description.strip(),
                    tool_call_id=tool_call.id,
                    model_override=params.model,
                    timeout_s=params.effective_timeout,
                    resumed=params.resume is not None,
                    dependencies=params.dependencies,
                    budget_seconds=params.budget_seconds,
                    isolation=params.isolation,
                )
            except Exception:
                self._runtime.subagent_store.update_instance(
                    agent_id,
                    status="idle",
                )
                if created_instance:
                    self._runtime.subagent_store.delete_instance(agent_id)
                raise
            dependency_text = ", ".join(params.dependencies) if params.dependencies else "(none)"
            budget_text = (
                str(params.budget_seconds) if params.budget_seconds is not None else "(none)"
            )
            lines = [
                tool_status_line(ToolResultStatus.launched),
                f"task_id: {view.spec.id}",
                f"kind: {view.spec.kind}",
                f"status: {view.runtime.status}",
                f"description: {view.spec.description}",
                f"agent_id: {agent_id}",
                f"actual_subagent_type: {actual_type}",
                f"dependencies: {dependency_text}",
                f"budget_seconds: {budget_text}",
                f"isolation: {params.isolation}",
                f"synthesis_state: {getattr(view.spec, 'synthesis_state', None) or 'pending'}",
                "automatic_notification: true",
                "next_step: You will be automatically notified when it completes.",
                (
                    "next_step: Use TaskOutput with this task_id for a non-blocking status/output "
                    "snapshot. Only set block=true when you intentionally want to wait."
                ),
                (
                    "next_step: If you launched several agents, do not block=true on any single "
                    "one — blocking waits only for that task and freezes the turn until the "
                    "slowest finishes. Return control and rely on the completion notifications."
                ),
                f"resume_hint: After this task reaches a terminal state, use "
                f'Agent(resume="{agent_id}", prompt="...") for follow-up work. Its final report '
                "arrives via the completion notification and TaskOutput — do not resume while it "
                "is still running.",
            ]
            return ToolReturnValue(
                is_error=False,
                output="\n".join(lines),
                message="Background task started.",
                display=[],
                extras={"status": ToolResultStatus.launched.value},
            )
        except FileNotFoundError as exc:
            return ToolError(message=str(exc), brief="Agent not found")
        except ValueError as exc:
            # Malformed resume id (store.instance_dir validates [A-Za-z0-9_-]{1,64}).
            return ToolError(message=str(exc), brief="Agent not found")
        except KeyError as exc:
            _emit_subagent_tool_fallback(
                reason="unavailable_agent_type",
                requested_type=params.subagent_type or "coder",
                runtime=self._runtime,
            )
            return ToolError(
                message=(
                    f"{exc.args[0] if exc.args else exc}. Available types: "
                    f"{', '.join(sorted(self._runtime.labor_market.builtin_types))}."
                ),
                brief="Invalid subagent type",
            )
        except RuntimeError as exc:
            logger.exception("Background agent launch failed")
            return ToolError(message=str(exc), brief="Background start failed")


def _run_agents_fingerprint(params: RunAgentsParams) -> str:
    payload = {
        "summary": params.summary,
        "base_prompt": params.base_prompt,
        "agent_count": len(params.agents),
        "agent_names": [agent.name for agent in params.agents],
        "agent_prompts": [agent.prompt for agent in params.agents],
        "agent_titles": [agent.title for agent in params.agents],
        "subagent_types": [agent.subagent_type or "coder" for agent in params.agents],
        "model": params.model,
        "run_in_background": params.run_in_background,
        "isolation": params.isolation,
        "timeout": params.timeout,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _BackgroundCapacity:
    requested: int
    max_running: int
    active: int
    available: int
    launch_count: int

    @property
    def deferred_count(self) -> int:
        return max(0, self.requested - self.launch_count)

    @property
    def is_limited(self) -> bool:
        return self.deferred_count > 0


def _with_distinct_codenames(agents: list[AgentRunConfig]) -> list[AgentRunConfig]:
    """Replace generic/duplicate child names with distinctive instance codenames.

    Models routinely echo the subagent type as the name, producing identical
    ``code-reviewer:code-reviewer`` rows for every parallel child. A generated
    codename makes each instance distinguishable across the result tree,
    TaskList, and notifications; caller-chosen distinct names are kept as-is.
    When the caller gave no title either, the codename plus type becomes the
    task description so notifications stay self-explanatory.
    """
    renamed: list[AgentRunConfig] = []
    seen: set[str] = set()
    for child in agents:
        child_type = child.subagent_type or "coder"
        name = child.name.strip()
        if is_generic_agent_name(name, child_type) or name.lower() in seen:
            codename = generate_codename(seen | {a.name.strip().lower() for a in agents})
            child = child.model_copy(
                update={
                    "name": codename,
                    "title": child.title or f"{codename} ({child_type})",
                }
            )
        seen.add(child.name.strip().lower())
        renamed.append(child)
    return renamed


class RunAgentsTool(CallableTool2[RunAgentsParams]):
    name: str = "RunAgents"
    params: type[RunAgentsParams] = RunAgentsParams
    emits_tool_execution_started_after_approval = True

    def __init__(self, runtime: Runtime):
        max_background = runtime.config.background.max_running_tasks
        super().__init__(
            description=(
                "Launch a bounded group of focused child agents that share context. "
                "Use this for scout/plan/implement/review/verify workflows when multiple "
                "independent subtasks can be delegated together. Put the shared context "
                "packet in base_prompt: goal, repo constraints, known evidence, excluded "
                "scope, and output requirements. Each child receives base_prompt, then "
                "its own single-objective prompt with scope and verification criteria. "
                "Background mode returns task IDs immediately; foreground mode runs children "
                "concurrently (bounded by the session background-task limit) and returns their "
                "summaries. Background batches share the session "
                f"background-task limit ({max_background} total slots, including running "
                "shell/background tasks); oversized background batches launch what fits now "
                "and report the deferred children."
            )
        )
        self._runtime = runtime
        self._agent_tool = AgentTool(runtime)

    def _child_concurrency_limit(self) -> int:
        """How many children may execute at once (execution-capacity guard).

        Deliberately reuses ``background.max_running_tasks`` so one config knob
        bounds total child execution; setting it to 1 also serializes
        foreground fan-out.
        """
        return max(1, self._runtime.config.background.max_running_tasks)

    def _background_capacity(self, params: RunAgentsParams) -> _BackgroundCapacity | None:
        if not params.run_in_background:
            return None
        requested = len(params.agents)
        max_running = self._runtime.config.background.max_running_tasks
        active = self._runtime.background_tasks.active_task_count()
        available = max(0, max_running - active)
        return _BackgroundCapacity(
            requested=requested,
            max_running=max_running,
            active=active,
            available=available,
            launch_count=min(requested, available),
        )

    def _background_capacity_error(self, capacity: _BackgroundCapacity | None) -> ToolError | None:
        if capacity is None or capacity.launch_count > 0:
            return None

        message = (
            f"RunAgents requested {capacity.requested} background agent(s), but no background "
            f"task slots are available (active={capacity.active}, max={capacity.max_running}). "
            "Wait for existing tasks to finish, or set run_in_background=false for "
            "foreground execution."
        )
        output = "\n".join(
            [
                tool_status_line(ToolResultStatus.failure),
                "reason: background_task_limit",
                f"requested_agents: {capacity.requested}",
                f"active_background_tasks: {capacity.active}",
                f"max_background_tasks: {capacity.max_running}",
                f"available_background_slots: {capacity.available}",
                (
                    "next_step: Wait for active tasks to finish, or use run_in_background=false "
                    "to run children in the foreground."
                ),
            ]
        )
        return ToolError(message=message, brief="Background task limit", output=output)

    @override
    async def __call__(self, params: RunAgentsParams) -> ToolReturnValue:
        if self._runtime.role != "root":
            return ToolError(
                message="Subagents cannot launch other subagents.",
                brief="RunAgents unavailable",
            )
        if not params.run_in_background and params.isolation != "none":
            # Foreground children would share one tree despite the isolation
            # request; fail fast rather than proceed unisolated.
            return ToolError(
                message=(
                    "isolation='worktree' is only supported for background child "
                    "agents; set run_in_background=true or drop isolation."
                ),
                brief="Invalid isolation",
            )
        if params.model is not None and params.model not in self._runtime.config.models:
            return ToolError(
                message=f"Unknown model alias: {params.model}",
                brief="Invalid model alias",
            )
        for child in params.agents:
            requested_type = child.subagent_type or "coder"
            # Fail fast on a hallucinated type BEFORE any child launches:
            # discovering it mid-loop leaves earlier children running, and a
            # corrected retry then double-launches them (the orchestration
            # fingerprint is already approved by that point).
            if self._runtime.labor_market.get_builtin_type(requested_type) is None:
                return ToolError(
                    message=(
                        f"Unknown subagent type {requested_type!r} for agent "
                        f"{child.name!r}. Available types: "
                        f"{', '.join(sorted(self._runtime.labor_market.builtin_types))}."
                    ),
                    brief="Invalid subagent type",
                )
            if err := self._agent_tool.check_execution_policy(requested_type):
                return err
            if err := self._agent_tool.check_required_mcp_servers(requested_type):
                return err
        capacity = self._background_capacity(params)
        if err := self._background_capacity_error(capacity):
            return err

        fingerprint = _run_agents_fingerprint(params)
        if self._runtime.approval.is_orchestration_approved(fingerprint):
            from pythinker_code.soul.toolset import emit_current_tool_execution_started

            emit_current_tool_execution_started()
            orchestration_approval = "reused"
        else:
            approved_count = capacity.launch_count if capacity is not None else len(params.agents)
            deferred_count = len(params.agents) - approved_count
            if deferred_count:
                approval_summary = (
                    f"Launch up to {len(params.agents)} child agent(s) for `{params.summary}` "
                    f"with isolation={params.isolation}, background={params.run_in_background}. "
                    f"Currently {approved_count} slot(s) are available; any children "
                    "beyond the capacity available at launch time will be reported "
                    "as deferred."
                )
            else:
                approval_summary = (
                    f"Launch {len(params.agents)} child agent(s) for `{params.summary}` "
                    f"with isolation={params.isolation}, background={params.run_in_background}"
                )
            approval = await self._runtime.approval.request(
                self.name,
                "run agents orchestration",
                approval_summary,
            )
            if not approval:
                return approval.rejection_error()
            self._runtime.approval.approve_orchestration(fingerprint)
            orchestration_approval = "requested"

        # Capacity can change while the human is considering the orchestration approval.
        # Re-check immediately before launching children. If at least one slot is free,
        # launch the fitting prefix and report the overflow as deferred instead of burning
        # a model turn on a hard failure like "requested 5, available 4".
        capacity = self._background_capacity(params)
        if err := self._background_capacity_error(capacity):
            return err
        agents_to_launch = params.agents
        deferred_agents: list[AgentRunConfig] = []
        if capacity is not None and capacity.is_limited:
            agents_to_launch = params.agents[: capacity.launch_count]
            deferred_agents = params.agents[capacity.launch_count :]
        # Applied after fingerprinting/approval (which use the caller's params
        # verbatim, keeping re-approval stable) and only to launched children.
        agents_to_launch = _with_distinct_codenames(agents_to_launch)

        from pythinker_code.scratchpad import append_scratch_event

        scratchpad_result = await append_scratch_event(
            self._runtime.work_dir,
            session_id=self._runtime.session.id,
            session_title=self._runtime.session.title or self._runtime.session.state.custom_title,
            labels=["kind:agent-batch"],
            title="agent batch started",
            details=[
                f"summary: {params.summary}",
                f"requested_agents: {len(params.agents)}",
                f"mode: {'background' if params.run_in_background else 'foreground'}",
            ],
        )

        # Children run concurrently (background launches are quick; foreground
        # children genuinely overlap), bounded so a large batch cannot fork-bomb
        # the session. Results keep the request order, and one failing child
        # surfaces as its own error entry instead of aborting its siblings.
        concurrency = asyncio.Semaphore(self._child_concurrency_limit())

        async def run_child(child: AgentRunConfig) -> ToolReturnValue:
            child_params = Params(
                description=(child.title or child.name).strip(),
                prompt=self._child_prompt(params.base_prompt, child.prompt),
                subagent_type=child.subagent_type or "coder",
                model=params.model,
                run_in_background=params.run_in_background,
                timeout=params.timeout,
                isolation=params.isolation,
            )
            async with concurrency:
                try:
                    return await self._agent_tool(child_params)
                except Exception as exc:  # noqa: BLE001 — isolate child crash from siblings
                    logger.exception("RunAgents child {} failed", child.name)
                    return ToolError(message=f"Failed to run agent: {exc}", brief="Agent failed")

        child_results = await asyncio.gather(*(run_child(child) for child in agents_to_launch))
        results = list(zip(agents_to_launch, child_results, strict=True))

        any_error = any(result.is_error for _, result in results)
        tool_status = (
            ToolResultStatus.failure
            if any_error
            else ToolResultStatus.launched
            if params.run_in_background
            else ToolResultStatus.success
        )
        mode = "background" if params.run_in_background else "foreground"
        lines = [
            tool_status_line(tool_status),
            f"orchestration_approval: {orchestration_approval}",
            f"orchestration_fingerprint: {fingerprint[:12]}",
            f"summary: {params.summary}",
            f"mode: {mode}",
            f"requested_agent_count: {len(params.agents)}",
            f"agent_count: {len(results)}",
            f"deferred_agent_count: {len(deferred_agents)}",
            f"scratchpad: {scratchpad_result.reason}",
        ]
        # Aggregate child spend so an N-child fan-out reports total tokens/cost in
        # one place (foreground completions only; background children report later).
        lines.extend(summarize_batch([result for _, result in results]))
        # Roll up RISKS/BLOCKERS from completed child reports so the orchestrator
        # sees cross-child findings without re-parsing every result body.
        if not params.run_in_background:
            lines.extend(
                aggregate_findings(
                    (child.name, result.output if isinstance(result.output, str) else "")
                    for child, result in results
                    if not result.is_error
                )
            )
        if capacity is not None:
            lines.extend(
                [
                    f"active_background_tasks: {capacity.active}",
                    f"max_background_tasks: {capacity.max_running}",
                    f"available_background_slots: {capacity.available}",
                ]
            )
        if deferred_agents:
            lines.append("capacity_limited: true")
            lines.append("deferred_agents:")
            for child in deferred_agents:
                lines.append(f"- name: {child.name}")
                lines.append(f"  subagent_type: {child.subagent_type or 'coder'}")
                lines.append("  status: deferred")
            lines.append(
                "next_step: Launch deferred agents after active background tasks complete."
            )
        lines.append("agents:")
        for child, result in results:
            status = self._child_result_status(result, run_in_background=params.run_in_background)
            lines.append(f"- name: {child.name}")
            lines.append(f"  subagent_type: {child.subagent_type or 'coder'}")
            lines.append(f"  status: {status}")
            if result.is_error:
                lines.append(f"  brief: {result.brief}")
                lines.append(f"  message: {result.message}")
            else:
                output = result.output if isinstance(result.output, str) else str(result.output)
                indented = "\n".join(f"    {line}" for line in output.splitlines())
                lines.append("  result: |")
                lines.append(indented)
        if any_error:
            message = "One or more agents failed."
        elif deferred_agents:
            message = f"Agents launched; {len(deferred_agents)} deferred by background capacity."
        elif params.run_in_background:
            message = "Agents launched."
        else:
            message = "Agents completed."
        return ToolReturnValue(
            is_error=any_error,
            output="\n".join(lines),
            message=message,
            display=[],
            extras={"status": tool_status.value},
        )

    @staticmethod
    def _child_result_status(result: ToolReturnValue, *, run_in_background: bool) -> str:
        if result.is_error:
            return "error"
        output = result.output if isinstance(result.output, str) else str(result.output)
        for line in output.splitlines():
            if line.startswith("status:"):
                status = line.removeprefix("status:").strip()
                if status:
                    return status
        return "launched" if run_in_background else "completed"

    @staticmethod
    def _child_prompt(base_prompt: str, prompt: str) -> str:
        base = base_prompt.strip()
        child = prompt.strip()
        if base and child:
            return f"{base}\n\n{child}"
        return base or child


Agent = AgentTool
RunAgents = RunAgentsTool
