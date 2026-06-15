from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import importlib
import inspect
import json
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast, overload

from pythinker_core.tooling import (
    CallableTool,
    CallableTool2,
    HandleResult,
    Tool,
    ToolError,
    ToolOk,
    Toolset,
)
from pythinker_core.tooling.error import (
    ToolNotFoundError,
    ToolParseError,
    ToolRuntimeError,
)
from pythinker_core.tooling.mcp import convert_mcp_content
from pythinker_core.utils.typing import JsonType
from pythinker_host.path import HostPath

from pythinker_code.exception import InvalidToolError, MCPRuntimeError
from pythinker_code.hooks.engine import HookEngine
from pythinker_code.tools import SkipThisTool
from pythinker_code.utils.logging import logger
from pythinker_code.wire.types import (
    AudioURLPart,
    ContentPart,
    ImageURLPart,
    MCPServerSnapshot,
    MCPStatusSnapshot,
    TextPart,
    ToolCall,
    ToolCallRequest,
    ToolExecutionStarted,
    ToolResult,
    ToolReturnValue,
    ToolUseSkipped,
    VideoURLPart,
)

if TYPE_CHECKING:
    import fastmcp
    import mcp
    from fastmcp.client.client import CallToolResult
    from fastmcp.client.transports import ClientTransport
    from fastmcp.mcp_config import MCPConfig

    from pythinker_code.soul.agent import Runtime

current_tool_call = ContextVar[ToolCall | None]("current_tool_call", default=None)
_current_tool_execution_started_ids: ContextVar[set[str] | None] = ContextVar(
    "current_tool_execution_started_ids", default=None
)

# Per-server timeout for closing MCP clients during teardown, so one hung client
# cannot block cleanup of the rest (mcpext-3).
_MCP_CLOSE_TIMEOUT_S = 5.0

_current_session_id: ContextVar[str] = ContextVar("_current_session_id", default="")
_MCP_LOG_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def set_session_id(sid: str) -> None:
    _current_session_id.set(sid)


def get_session_id() -> str:
    return _current_session_id.get()


def _get_session_id() -> str:
    return _current_session_id.get()


def get_current_tool_call_or_none() -> ToolCall | None:
    """
    Get the current tool call or None.
    Expect to be not None when called from a `__call__` method of a tool.
    """
    return current_tool_call.get()


def emit_current_tool_execution_started() -> None:
    """Emit ToolExecutionStarted once for the current tool call, if wire is active."""
    tool_call = get_current_tool_call_or_none()
    if tool_call is None:
        return

    started_ids = _current_tool_execution_started_ids.get()
    if started_ids is None:
        started_ids = set[str]()
        _current_tool_execution_started_ids.set(started_ids)
    if tool_call.id in started_ids:
        return
    started_ids.add(tool_call.id)

    try:
        from pythinker_code.soul import get_wire_or_none

        if wire := get_wire_or_none():
            wire.soul_side.send(ToolExecutionStarted(tool_call_id=tool_call.id))
    except Exception as exc:  # noqa: BLE001 - lifecycle events must not break tool execution
        logger.debug(
            "Failed to emit tool execution start: {tool_name} (call_id={call_id}): {error}",
            tool_name=tool_call.function.name,
            call_id=tool_call.id,
            error=exc,
        )


def _emit_tool_use_skipped(
    *,
    tool_call_id: str,
    tool_name: str,
    reason: Literal["dedup", "policy", "interrupt", "concurrent_inflight"],
    resumed: bool = False,
) -> None:
    try:
        from pythinker_code.soul import get_wire_or_none

        if wire := get_wire_or_none():
            wire.soul_side.send(
                ToolUseSkipped(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    reason=reason,
                    resumed=resumed,
                )
            )
    except Exception as exc:  # noqa: BLE001 - observability must not break tool execution
        logger.debug(
            "Failed to emit tool skipped event: {tool_name} (call_id={call_id}): {error}",
            tool_name=tool_name,
            call_id=tool_call_id,
            error=exc,
        )


def _tool_defers_execution_started(tool: ToolType) -> bool:
    return bool(getattr(tool, "emits_tool_execution_started_after_approval", False))


def _is_external_side_effect_tool(tool: ToolType) -> bool:
    """Return True for tool adapters whose side effects are not statically classified.

    Reads the declarative ``external_side_effect_tool`` class flag (declared on
    ``MCPTool``, ``WireExternalTool``, and ``PluginTool``) instead of matching
    module/qualname strings, so a moved or renamed adapter cannot silently fall
    out of the side-effect classification.
    """
    return bool(getattr(tool, "external_side_effect_tool", False))


def _mcp_stderr_log_path(runtime: Runtime, server_name: str) -> Path:
    safe_name = _MCP_LOG_NAME_RE.sub("_", server_name).strip("._-") or "server"
    log_dir = runtime.session.dir / "mcp"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{safe_name}.stderr.log"


async def _discover_optional_capability[T](
    server_name: str,
    capability: str,
    list_fn: Callable[[], Awaitable[Iterable[T]]],
) -> list[T]:
    """List an optional MCP capability (resources/prompts), separating a server that
    genuinely lacks it from a transient/transport failure (mcpext-1).

    Per MCP, a server that does not implement the capability replies with a
    ``METHOD_NOT_FOUND`` (-32601) error — expected, recorded as empty, logged at debug.
    Any other failure (e.g. a transient transport error) is NOT treated as silently
    identical to "no capability": it is logged at WARNING so an operator can tell a
    momentary blip from a permanent absence. Either way an empty list is returned so the
    server (and its already-listed tools) still connect rather than failing the whole
    handshake on an optional capability.
    """
    from mcp.shared.exceptions import McpError
    from mcp.types import METHOD_NOT_FOUND

    try:
        return list(await list_fn())
    except McpError as exc:
        if exc.error.code == METHOD_NOT_FOUND:
            logger.debug(
                "MCP server {name} does not support {cap}", name=server_name, cap=capability
            )
            return []
        logger.warning(
            "MCP server {name} errored listing {cap} (code {code}); treating as empty: {error}",
            name=server_name,
            cap=capability,
            code=exc.error.code,
            error=exc,
        )
        return []
    except Exception as exc:
        logger.warning(
            "MCP server {name} failed listing {cap} (transient?); treating as empty: {error}",
            name=server_name,
            cap=capability,
            error=exc,
        )
        return []


def _configure_mcp_client_stderr_log(client: Any, runtime: Runtime, server_name: str) -> None:
    """Route stdio MCP child stderr to a session log file instead of the TUI."""
    log_path = _mcp_stderr_log_path(runtime, server_name)

    def _set_transport_log_file(transport: Any) -> None:
        if hasattr(transport, "log_file"):
            transport.log_file = log_path
        nested = getattr(transport, "transport", None)
        if nested is not None:
            _set_transport_log_file(nested)
        for child in getattr(transport, "_transports", ()) or ():
            _set_transport_log_file(child)

    _set_transport_log_file(getattr(client, "transport", None))


def _classify_mcp_connect_error(error: BaseException, server_name: str) -> str:
    """One short actionable line for /mcp explaining a connect failure.

    A bare 'failed' tells the user nothing; each common failure shape names
    its fix (config knob, auth command, command path) so recovery does not
    require reading logs.
    """
    if isinstance(error, TimeoutError):
        return (
            "startup timed out — raise mcp.client.startup_timeout_ms if the server is slow to start"
        )
    if isinstance(error, FileNotFoundError):
        missing = error.filename or str(error)
        return f"command not found: {missing} — check the server command/path"
    if isinstance(error, ConnectionError):
        return "connection failed — is the server running and the URL reachable?"
    text = str(error) or type(error).__name__
    lowered = text.lower()
    if "401" in text or "unauthorized" in lowered or "authentication" in lowered:
        return f"authentication failed — run: pythinker mcp auth {server_name}"
    return text.splitlines()[0][:200]


@dataclass(frozen=True, slots=True)
class McpToolFilter:
    """Optional per-server tool scoping from mcp.json.

    ``enabledTools`` (exclusive allowlist) and ``disabledTools`` (denylist,
    wins on conflict) keep noisy servers from flooding the model tool list
    and double as a safety scoping knob. No filter fields → permissive.
    """

    enabled: frozenset[str] | None = None
    deny: frozenset[str] = frozenset()

    @classmethod
    def from_server_config(cls, server_config: Any) -> McpToolFilter:
        enabled: object = getattr(server_config, "enabledTools", None)
        disabled: object = getattr(server_config, "disabledTools", None) or ()
        enabled_names: frozenset[str] | None = None
        if isinstance(enabled, list):
            enabled_names = frozenset(
                name for name in cast(list[Any], enabled) if isinstance(name, str)
            )
        deny_names: frozenset[str] = frozenset()
        if isinstance(disabled, list):
            deny_names = frozenset(
                name for name in cast(list[Any], disabled) if isinstance(name, str)
            )
        return cls(enabled=enabled_names, deny=deny_names)

    def allows(self, tool_name: str) -> bool:
        if tool_name in self.deny:
            return False
        return self.enabled is None or tool_name in self.enabled


type ToolType = CallableTool | CallableTool2[Any]
type ToolCallKey = tuple[str, str]


if TYPE_CHECKING:

    def type_check(pythinker_toolset: PythinkerToolset):
        _: Toolset = pythinker_toolset


_REMINDER_TEXT_1 = (
    "\n\n<system-reminder>\n"
    "You are repeating the exact same tool call with identical parameters."
    " Please carefully analyze the previous result. If the task is not yet complete,"
    " try a different method or parameters instead of repeating the same call."
    "\n</system-reminder>"
)

TOOL_USE_SKIPPED_REASONS = frozenset({"dedup", "policy", "interrupt", "concurrent_inflight"})


def _make_reminder_text_2(tool_name: str, repeat_count: int, canonical_args: str) -> str:
    # Echo only a bounded preview of the arguments: large-payload tools
    # (WriteFile, MultiEdit) would otherwise re-inject the whole body into
    # context on every repeat — defeating the reminder by inflating tokens.
    # Exact identity is preserved by the args_hash in the dedup telemetry.
    args_limit = 256
    if len(canonical_args) > args_limit:
        dropped = len(canonical_args) - args_limit
        args_preview = f"{canonical_args[:args_limit]}... [truncated {dropped} chars]"
    else:
        args_preview = canonical_args
    return (
        "\n\n<system-reminder>\n"
        "You have repeatedly called the same tool with identical parameters many times.\n"
        "Repeated tool call detected:\n"
        f"- tool: {tool_name}\n"
        f"- repeated_times: {repeat_count}\n"
        f"- arguments: {args_preview}\n"
        "The previous repeated calls did not make progress. Do not call this exact same tool "
        "with the exact same arguments again.\n"
        "Carefully inspect the latest tool result and choose a different next action, "
        "different parameters, or finish the task if enough evidence has been gathered."
        "\n</system-reminder>"
    )


def _sort_json_value(value: object) -> object:
    if isinstance(value, list):
        return [_sort_json_value(item) for item in cast("list[object]", value)]
    if isinstance(value, dict):
        value_dict = cast("dict[str, object]", value)
        return {key: _sort_json_value(value_dict[key]) for key in sorted(value_dict)}
    return value


def _canonical_tool_arguments(arguments: Any) -> str:
    try:
        return json.dumps(
            _sort_json_value(arguments),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return str(arguments)


def _canonical_tool_arguments_text(arguments: str) -> str:
    try:
        return _canonical_tool_arguments(json.loads(arguments, strict=False))
    except json.JSONDecodeError:
        return arguments


def _normalize_call_key(tool_name: str, arguments: str) -> ToolCallKey:
    return (tool_name, _canonical_tool_arguments_text(arguments))


def _append_reminder_to_return_value(return_value: Any, reminder_text: str) -> Any:
    """Append dedup reminder text to a ToolReturnValue output."""
    if not isinstance(return_value, ToolReturnValue):
        return return_value

    output = return_value.output

    if isinstance(output, str):
        new_output: str | list[ContentPart] = output + reminder_text
    else:
        new_output = list(output)
        if new_output and isinstance(new_output[-1], TextPart):
            new_output[-1] = TextPart(text=new_output[-1].text + reminder_text)
        else:
            new_output.append(TextPart(text=reminder_text))

    return return_value.model_copy(update={"output": new_output})


def _emit_tool_use_skipped_if_opted_in(
    tool: ToolType,
    *,
    tool_call_id: str,
    tool_name: str,
    reason: Literal["dedup", "policy", "interrupt", "concurrent_inflight"],
    resumed: bool = False,
) -> None:
    if not getattr(tool, "emits_tool_use_skipped", False):
        return
    _emit_tool_use_skipped(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        reason=reason,
        resumed=resumed,
    )


_DEFAULT_MAX_CONCURRENT_READERS = 10
"""Cap on concurrent parallel-safe tool calls. A turn that fans out many readers
(e.g. dozens of FetchURL) overlaps freely up to this bound rather than opening an
unbounded number of sockets/file handles at once."""


class _ReadWriteGate:
    """Async reader-writer gate for same-step parallel tool calls.

    Parallel-safe tools (readers) overlap freely up to ``max_concurrent_readers``;
    a mutating tool (writer) waits for in-flight readers to drain and excludes
    everything while it runs. Writers hold the lock while draining, which also
    blocks new readers behind a queued writer — dispatch order stays deterministic
    and writers cannot starve. Unflagged/plugin-style tools default to the
    exclusive writer path unless they explicitly declare ``supports_parallel=True``.
    """

    def __init__(self, max_concurrent_readers: int = _DEFAULT_MAX_CONCURRENT_READERS) -> None:
        self._writer_lock = asyncio.Lock()
        self._active_readers = 0
        self._readers_drained = asyncio.Event()
        self._readers_drained.set()
        self._reader_slots = asyncio.Semaphore(max_concurrent_readers)

    @contextlib.asynccontextmanager
    async def shared(self) -> AsyncGenerator[None]:
        # Only tools that opted into ``supports_parallel=True`` should enter this
        # shared path; unflagged/plugin adapters stay exclusive by default.
        # Cap concurrent readers. Acquire the slot BEFORE the writer lock / counter
        # bump: a reader still queued here has not incremented _active_readers, so it
        # never holds _readers_drained open, and writers (which never touch the
        # semaphore) cannot be starved — keeping the cap deadlock-safe.
        await self._reader_slots.acquire()
        try:
            async with self._writer_lock:
                self._active_readers += 1
                self._readers_drained.clear()
            try:
                yield
            finally:
                self._active_readers -= 1
                if self._active_readers == 0:
                    self._readers_drained.set()
        finally:
            self._reader_slots.release()

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncGenerator[None]:
        async with self._writer_lock:
            await self._readers_drained.wait()
            yield


class PythinkerToolset:
    def __init__(self, runtime: Runtime | None = None) -> None:
        self._runtime = runtime
        self._tool_dict: dict[str, ToolType] = {}
        self._hidden_tools: set[str] = set()
        self._mcp_servers: dict[str, MCPServerInfo] = {}
        self._mcp_loading_task: asyncio.Task[None] | None = None
        self._deferred_mcp_load: tuple[list[MCPConfig], Runtime] | None = None
        self._hook_engine: HookEngine = HookEngine()
        self._concurrency_gate = _ReadWriteGate()

        # Deduplication state
        self._previous_step_calls: list[ToolCallKey] = []
        self._current_step_calls: list[ToolCallKey] = []
        self._current_step_tasks: dict[ToolCallKey, asyncio.Task[ToolResult]] = {}
        self._seen_call_keys: set[ToolCallKey] = set()
        self._consecutive_key: ToolCallKey | None = None
        self._consecutive_count: int = 0
        self._step_closed: bool = False
        self._dedup_triggered: bool = False
        self._step_no: int = 0
        self._turn_id: str = ""

    def set_hook_engine(self, engine: HookEngine) -> None:
        self._hook_engine = engine

    def add(self, tool: ToolType) -> None:
        self._tool_dict[tool.name] = tool

    def add_shared_tools(self, names: list[str], shared: dict[str, ToolType]) -> None:
        """Attach already-instantiated tools (e.g. the parent session's MCP tools) by registry name.

        Names with no live registry entry are skipped: such a tool attaches only
        when the runtime actually provides it (e.g. the MCP server is connected).
        """
        for name in names:
            tool = shared.get(name)
            if tool is None:
                logger.info("Shared tool not available from runtime: {name}", name=name)
                continue
            existing = self.find(tool.name)
            if existing is not None and existing is not tool:
                logger.warning(
                    "Shared tool '{name}' conflicts with an existing tool, skipping",
                    name=tool.name,
                )
                continue
            self.add(tool)

    def _register_mcp_tools(self, server_name: str, tools: list[MCPTool[Any]]) -> None:
        """Register MCP tools, skipping any whose name conflicts with a non-MCP tool."""
        for tool in tools:
            existing = self.find(tool.name)
            if existing is not None and not isinstance(existing, MCPTool):
                logger.warning(
                    "MCP tool '{name}' from server '{server}' conflicts with an existing"
                    " tool, skipping",
                    name=tool.name,
                    server=server_name,
                )
                continue
            if isinstance(existing, MCPTool) and existing.mcp_server_name != server_name:
                # Servers connect concurrently, so which one wins is nondeterministic;
                # keep last-wins semantics but make the shadowing visible.
                logger.warning(
                    "MCP tool '{name}' from server '{server}' overrides the same-named"
                    " tool from MCP server '{prev}'",
                    name=tool.name,
                    server=server_name,
                    prev=existing.mcp_server_name,
                )
            self.add(tool)

    def hide(self, tool_name: str) -> bool:
        """Hide a tool from the LLM tool list. Returns True if the tool exists."""
        if tool_name in self._tool_dict:
            self._hidden_tools.add(tool_name)
            return True
        return False

    def unhide(self, tool_name: str) -> None:
        """Restore a hidden tool to the LLM tool list."""
        self._hidden_tools.discard(tool_name)

    @overload
    def find(self, tool_name_or_type: str) -> ToolType | None: ...
    @overload
    def find[T: ToolType](self, tool_name_or_type: type[T]) -> T | None: ...
    def find(self, tool_name_or_type: str | type[ToolType]) -> ToolType | None:
        if isinstance(tool_name_or_type, str):
            return self._tool_dict.get(tool_name_or_type)
        else:
            for tool in self._tool_dict.values():
                if isinstance(tool, tool_name_or_type):
                    return tool
        return None

    @property
    def tools(self) -> list[Tool]:
        return [tool.base for tool in self._tool_dict.values() if self._is_tool_visible(tool)]

    def set_work_dir_override(self, work_dir: HostPath | None) -> HostPath | None:
        """Apply a process-local operational cwd override to this toolset's runtime and tools."""
        if self._runtime is None:
            return None
        self._runtime.work_dir_override = work_dir
        effective_work_dir = self._runtime.work_dir
        self._runtime.builtin_args = dataclass_replace(
            self._runtime.builtin_args,
            PYTHINKER_WORK_DIR=effective_work_dir,
        )
        for tool in self._tool_dict.values():
            if hasattr(tool, "_work_dir"):
                cast(Any, tool)._work_dir = effective_work_dir
        return effective_work_dir

    def _is_tool_visible(self, tool: ToolType) -> bool:
        """Return whether *tool* should be advertised to the model for this step.

        Tool-specific execution guards remain authoritative.  This model-facing filter is a
        defense-in-depth layer that prevents agents from repeatedly selecting tools that the
        active runtime profile, execution policy, or session mode will reject anyway.
        """
        if tool.name in self._hidden_tools:
            return False
        if self._runtime is None:
            return True

        runtime = self._runtime
        from pythinker_code.execution_profiles import resolve_execution_policy
        from pythinker_code.soul.permission import active_permission_profile

        profile = active_permission_profile(runtime)
        policy = resolve_execution_policy(
            runtime.config.agent_execution_profile,
            yolo=runtime.approval.is_yolo_flag(),
        )

        if tool.name in {"WriteFile", "StrReplaceFile"}:
            if policy.write == "deny":
                return False
            return profile.allow_file_mutation or profile.allow_plan_file_mutation

        if tool.name == "Shell" and policy.shell == "deny":
            return False

        if tool.name in {"SearchWeb", "FetchURL"} and (
            policy.network == "deny" or not profile.allow_network
        ):
            return False

        if tool.name in {"Agent", "RunAgents"} and (
            runtime.role != "root" or policy.subagents == "deny"
        ):
            return False

        if tool.name == "EnterPlanMode" and runtime.session.state.plan_mode:
            return False
        if tool.name == "ExitPlanMode" and not runtime.session.state.plan_mode:
            return False

        if _is_external_side_effect_tool(tool):
            return profile.allow_file_mutation and profile.allow_shell_mutation

        return True

    async def _gated_call(self, tool: ToolType, arguments: JsonType) -> ToolReturnValue:
        """Execute under the same-step concurrency policy.

        Tools declaring ``supports_parallel`` share the gate; everything
        else (including unflagged plugin/MCP tools — the safe default)
        runs exclusively so same-step mutations stay ordered.
        """
        if getattr(tool, "supports_parallel", False):
            async with self._concurrency_gate.shared():
                return await tool.call(arguments)
        async with self._concurrency_gate.exclusive():
            return await tool.call(arguments)

    def begin_step(
        self,
        previous_calls: list[ToolCallKey],
        *,
        step_no: int = 0,
        turn_id: str = "",
    ) -> None:
        """Called before each step to set up deduplication state."""
        self._previous_step_calls = [
            _normalize_call_key(tool_name, arguments) for tool_name, arguments in previous_calls
        ]
        self._current_step_calls = []
        self._current_step_tasks = {}
        self._step_closed = False
        self._dedup_triggered = False
        self._step_no = step_no
        self._turn_id = turn_id
        if not self._previous_step_calls:
            self._seen_call_keys = set()
            self._consecutive_key = None
            self._consecutive_count = 0
        else:
            self._seen_call_keys.update(self._previous_step_calls)
            if self._consecutive_key is None and self._consecutive_count == 0:
                self._advance_consecutive_streak(self._previous_step_calls)

    def end_step(self) -> list[ToolCallKey]:
        """Called after each step to capture the calls made in this step."""
        if not self._step_closed:
            self._advance_consecutive_streak(self._current_step_calls)
            self._seen_call_keys.update(self._current_step_calls)
            self._step_closed = True
        return list(self._current_step_calls)

    def _advance_consecutive_streak(self, calls: list[ToolCallKey]) -> None:
        for call_key in calls:
            if call_key == self._consecutive_key:
                self._consecutive_count += 1
            else:
                self._consecutive_key = call_key
                self._consecutive_count = 1

    def _projected_streak_for_call(self, call_index: int) -> int:
        consecutive_key = self._consecutive_key
        consecutive_count = self._consecutive_count
        for call_key in self._current_step_calls[: call_index + 1]:
            if call_key == consecutive_key:
                consecutive_count += 1
            else:
                consecutive_key = call_key
                consecutive_count = 1
        return consecutive_count

    @property
    def dedup_triggered(self) -> bool:
        """Whether a cross-step duplicate was blocked in the current step."""
        return self._dedup_triggered

    def handle(self, tool_call: ToolCall) -> HandleResult:
        token = current_tool_call.set(tool_call)
        try:
            if tool_call.function.name not in self._tool_dict:
                available = list(self._tool_dict.keys())
                matches = difflib.get_close_matches(
                    tool_call.function.name, available, n=1, cutoff=0.6
                )
                return ToolResult(
                    tool_call_id=tool_call.id,
                    return_value=ToolNotFoundError(
                        tool_call.function.name,
                        suggestion=matches[0] if matches else None,
                    ),
                )

            tool = self._tool_dict[tool_call.function.name]

            try:
                arguments: JsonType = json.loads(tool_call.function.arguments or "{}", strict=False)
            except json.JSONDecodeError as e:
                logger.warning(
                    "Tool call JSON parse error: {tool_name} (call_id={call_id}): {error}",
                    tool_name=tool_call.function.name,
                    call_id=tool_call.id,
                    error=e,
                )
                return ToolResult(tool_call_id=tool_call.id, return_value=ToolParseError(str(e)))

            canonical_args = _canonical_tool_arguments(arguments)
            call_key = (tool_call.function.name, canonical_args)
            call_index = len(self._current_step_calls)
            self._current_step_calls.append(call_key)

            # Same-step dedup: wait for the original task and copy its result.
            if call_key in self._current_step_tasks:
                from pythinker_code.telemetry import track

                _emit_tool_use_skipped_if_opted_in(
                    tool,
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.function.name,
                    reason="dedup",
                    resumed=True,
                )
                track(
                    "tool_call_dedup_detected",
                    turn_id=self._turn_id,
                    step_no=self._step_no,
                    tool_name=tool_call.function.name,
                    dup_type="same_step",
                    args_hash=hashlib.sha256(canonical_args.encode("utf-8")).hexdigest()[:8],
                )
                original_task = self._current_step_tasks[call_key]

                async def _await_dup() -> ToolResult:
                    original_result = await original_task
                    return ToolResult(
                        tool_call_id=tool_call.id,
                        return_value=original_result.return_value,
                    )

                return asyncio.create_task(_await_dup())

            is_cross_step_dup = call_key in self._seen_call_keys
            reminder_text: str | None = None
            if is_cross_step_dup:
                from pythinker_code.telemetry import track

                track(
                    "tool_call_dedup_detected",
                    turn_id=self._turn_id,
                    step_no=self._step_no,
                    tool_name=tool_call.function.name,
                    dup_type="cross_step",
                    args_hash=hashlib.sha256(canonical_args.encode("utf-8")).hexdigest()[:8],
                )
                self._dedup_triggered = True
                repeat_count = self._projected_streak_for_call(call_index)
                if repeat_count == 3:
                    reminder_text = _REMINDER_TEXT_1
                elif repeat_count in (5, 8):
                    reminder_text = _make_reminder_text_2(
                        tool_call.function.name, repeat_count, canonical_args
                    )
                if reminder_text is not None:
                    _emit_tool_use_skipped_if_opted_in(
                        tool,
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.function.name,
                        reason="dedup",
                        resumed=False,
                    )

            async def _call():
                started_ids_token = _current_tool_execution_started_ids.set(set[str]())
                try:
                    return await _call_with_lifecycle()
                finally:
                    _current_tool_execution_started_ids.reset(started_ids_token)

            async def _call_with_lifecycle():
                tool_input_dict = arguments if isinstance(arguments, dict) else {}

                if self._runtime is not None:
                    from pythinker_code.soul.permission import check_tool_call_allowed

                    if err := check_tool_call_allowed(
                        self._runtime,
                        tool_call.function.name,
                        tool_input_dict,
                        tool=tool,
                    ):
                        _emit_tool_use_skipped_if_opted_in(
                            tool,
                            tool_call_id=tool_call.id,
                            tool_name=tool_call.function.name,
                            reason="policy",
                        )
                        return ToolResult(tool_call_id=tool_call.id, return_value=err)

                # --- PreToolUse ---
                from pythinker_code.hooks import events

                results = await self._hook_engine.trigger(
                    "PreToolUse",
                    matcher_value=tool_call.function.name,
                    input_data=events.pre_tool_use(
                        session_id=_get_session_id(),
                        cwd=str(Path.cwd()),
                        tool_name=tool_call.function.name,
                        tool_input=tool_input_dict,
                        tool_call_id=tool_call.id,
                    ),
                )
                for result in results:
                    if result.action == "block":
                        _emit_tool_use_skipped_if_opted_in(
                            tool,
                            tool_call_id=tool_call.id,
                            tool_name=tool_call.function.name,
                            reason="policy",
                        )
                        return ToolResult(
                            tool_call_id=tool_call.id,
                            return_value=ToolError(
                                message=result.reason or "Blocked by PreToolUse hook",
                                brief="Hook blocked",
                            ),
                        )

                # --- Execute tool ---
                from pythinker_code.telemetry import metrics as _m
                from pythinker_code.telemetry import otel as _otel

                if not _tool_defers_execution_started(tool):
                    emit_current_tool_execution_started()

                t0 = time.monotonic()
                _tool_span_cm = _otel.start_span(
                    "pythinker.tool",
                    {
                        "tool.name": tool_call.function.name,
                        "tool.call_id": tool_call.id,
                        # GenAI semconv so GenAI-aware backends recognize the tool layer.
                        "gen_ai.operation.name": "execute_tool",
                        "gen_ai.tool.name": tool_call.function.name,
                    },
                )
                _tool_span = _tool_span_cm.__enter__()
                try:
                    ret = await self._gated_call(tool, arguments)
                except Exception as e:
                    tool_elapsed = time.monotonic() - t0
                    _tool_span.set_attribute("tool.success", False)
                    _tool_span.set_attribute("tool.error_type", type(e).__name__)
                    _tool_span.set_attribute("tool.duration_ms", int(tool_elapsed * 1000))
                    _tool_span_cm.__exit__(type(e), e, e.__traceback__)
                    _m.record_tool_call(
                        tool_name=tool_call.function.name,
                        duration_seconds=tool_elapsed,
                        success=False,
                        error_type=type(e).__name__,
                    )
                    _m.record_error(kind="tool_error", error_type=type(e).__name__)
                    logger.exception(
                        "Tool execution failed: {tool_name} (call_id={call_id})",
                        tool_name=tool_call.function.name,
                        call_id=tool_call.id,
                    )
                    # --- PostToolUseFailure (fire-and-forget) ---
                    self._hook_engine.fire_and_forget_trigger(
                        "PostToolUseFailure",
                        matcher_value=tool_call.function.name,
                        input_data=events.post_tool_use_failure(
                            session_id=_get_session_id(),
                            cwd=str(Path.cwd()),
                            tool_name=tool_call.function.name,
                            tool_input=tool_input_dict,
                            error=str(e),
                            tool_call_id=tool_call.id,
                        ),
                    )
                    from pythinker_code.telemetry import track

                    _error_type = type(e).__name__
                    track(
                        "tool_error",
                        tool_name=tool_call.function.name,
                        error_type=_error_type,
                    )
                    track(
                        "tool_call",
                        tool_name=tool_call.function.name,
                        success=False,
                        duration_ms=int(tool_elapsed * 1000),
                        error_type=_error_type,
                        dup_type="cross_step" if is_cross_step_dup else "normal",
                    )
                    return ToolResult(
                        tool_call_id=tool_call.id,
                        return_value=ToolRuntimeError(str(e)),
                    )
                except BaseException as e:
                    # CancelledError/KeyboardInterrupt during the tool call: close the
                    # span in this task so its OTel context token detaches now, not
                    # later under GC in a different asyncio context.
                    _tool_span_cm.__exit__(type(e), e, e.__traceback__)
                    raise

                tool_elapsed = time.monotonic() - t0
                _tool_succeeded = not isinstance(ret, ToolError)
                _tool_span.set_attribute("tool.success", _tool_succeeded)
                if isinstance(ret, ToolError):
                    _tool_span.set_attribute("tool.error_brief", ret.brief or "")
                _tool_span.set_attribute("tool.duration_ms", int(tool_elapsed * 1000))
                _tool_span_cm.__exit__(None, None, None)
                _m.record_tool_call(
                    tool_name=tool_call.function.name,
                    duration_seconds=tool_elapsed,
                    success=_tool_succeeded,
                )
                logger.info(
                    "Tool {tool_name} completed in {elapsed:.1f}s (call_id={call_id})",
                    tool_name=tool_call.function.name,
                    elapsed=tool_elapsed,
                    call_id=tool_call.id,
                )
                from pythinker_code.telemetry import track as _track_tool_call

                _track_tool_call(
                    "tool_call",
                    tool_name=tool_call.function.name,
                    success=not isinstance(ret, ToolError),
                    duration_ms=int(tool_elapsed * 1000),
                    dup_type="cross_step" if is_cross_step_dup else "normal",
                )

                # --- PostToolUse (fire-and-forget) ---
                self._hook_engine.fire_and_forget_trigger(
                    "PostToolUse",
                    matcher_value=tool_call.function.name,
                    input_data=events.post_tool_use(
                        session_id=_get_session_id(),
                        cwd=str(Path.cwd()),
                        tool_name=tool_call.function.name,
                        tool_input=tool_input_dict,
                        tool_output=str(ret)[:2000],
                        tool_call_id=tool_call.id,
                    ),
                )

                # Append the dedup reminder inline (no-op on errors) so the
                # returned task is the tool task itself: cancelling it cancels
                # the tool, rather than orphaning it behind a wrapper task.
                if reminder_text is not None:
                    return ToolResult(
                        tool_call_id=tool_call.id,
                        return_value=_append_reminder_to_return_value(ret, reminder_text),
                    )
                return ToolResult(tool_call_id=tool_call.id, return_value=ret)

            task = asyncio.create_task(_call())
            self._current_step_tasks[call_key] = task
            return task
        finally:
            current_tool_call.reset(token)

    def register_external_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
    ) -> tuple[bool, str | None]:
        if name in self._tool_dict:
            existing = self._tool_dict[name]
            if not isinstance(existing, WireExternalTool):
                return False, "tool name conflicts with existing tool"
        try:
            tool = WireExternalTool(
                name=name,
                description=description,
                parameters=parameters,
            )
        except Exception as e:
            from pythinker_code.telemetry.errors import report_handled_error

            report_handled_error(e, site="soul.toolset.register_external")
            return False, str(e)
        self.add(tool)
        return True, None

    @property
    def mcp_servers(self) -> dict[str, MCPServerInfo]:
        """Get MCP servers info."""
        return self._mcp_servers

    def mcp_status_snapshot(self) -> MCPStatusSnapshot | None:
        """Return a read-only snapshot of current MCP startup state.

        Returns ``None`` only when no MCP is configured (the settled, nothing-to-load state).
        While a deferred startup is queued but has not populated ``_mcp_servers`` yet, a
        ``loading=True`` snapshot is returned instead — otherwise the required-MCP spawn gate
        could not distinguish "still starting" from "not configured" and would reject a
        first-turn subagent spawn during the startup window.
        """
        if not self._mcp_servers:
            if self.has_deferred_mcp_tools():
                return MCPStatusSnapshot(loading=True, connected=0, total=0, tools=0, servers=())
            return None

        servers = tuple(
            MCPServerSnapshot(
                name=name,
                status=info.status,
                tools=tuple(tool.name for tool in info.tools),
                error=info.error,
            )
            for name, info in self._mcp_servers.items()
        )
        return MCPStatusSnapshot(
            loading=self.has_pending_mcp_tools(),
            connected=sum(1 for server in servers if server.status == "connected"),
            total=len(servers),
            tools=sum(len(server.tools) for server in servers),
            servers=servers,
        )

    def defer_mcp_tool_loading(self, mcp_configs: list[MCPConfig], runtime: Runtime) -> None:
        """Store MCP configs for a later background startup."""
        self._deferred_mcp_load = (list(mcp_configs), runtime)

    def has_deferred_mcp_tools(self) -> bool:
        """Return True when MCP loading is configured but has not started yet."""
        return self._deferred_mcp_load is not None

    async def start_deferred_mcp_tool_loading(self) -> bool:
        """Start any deferred MCP loading in the background."""
        if self._deferred_mcp_load is None:
            return False
        if self._mcp_loading_task is not None or self._mcp_servers:
            self._deferred_mcp_load = None
            return False

        mcp_configs, runtime = self._deferred_mcp_load
        self._deferred_mcp_load = None
        await self.load_mcp_tools(mcp_configs, runtime, in_background=True)
        return True

    def load_tools(self, tool_paths: list[str], dependencies: dict[type[Any], Any]) -> None:
        """
        Load tools from paths like `pythinker_code.tools.shell:Shell`.

        Raises:
            InvalidToolError(PythinkerCLIException, ValueError): When any tool cannot be loaded.
        """

        good_tools: list[str] = []
        bad_tools: list[str] = []

        for tool_path in tool_paths:
            if ":" not in tool_path:
                # Named dynamic tools (e.g. MCP tools like `mcp__server__tool`) are not
                # importable module paths; they only take effect when the runtime
                # provides a matching tool, so they are not loaded here.
                logger.info("Skipping non-module tool entry: {tool_path}", tool_path=tool_path)
                continue
            try:
                tool = self._load_tool(tool_path, dependencies)
            except SkipThisTool:
                logger.info("Skipping tool: {tool_path}", tool_path=tool_path)
                continue
            if tool:
                self.add(tool)
                good_tools.append(tool_path)
            else:
                bad_tools.append(tool_path)
        logger.info("Loaded tools: {good_tools}", good_tools=good_tools)
        if bad_tools:
            raise InvalidToolError(f"Invalid tools: {bad_tools}")

    @staticmethod
    def _load_tool(tool_path: str, dependencies: dict[type[Any], Any]) -> ToolType | None:
        logger.debug("Loading tool: {tool_path}", tool_path=tool_path)
        module_name, class_name = tool_path.rsplit(":", 1)
        try:
            module = importlib.import_module(module_name)
        except ImportError as e:
            logger.warning(
                "Tool module import failed: {module_name}: {error}",
                module_name=module_name,
                error=e,
            )
            return None
        tool_cls = getattr(module, class_name, None)
        if tool_cls is None:
            logger.warning(
                "Tool class not found: {class_name} in {module_name}",
                class_name=class_name,
                module_name=module_name,
            )
            return None
        args: list[Any] = []
        if "__init__" in tool_cls.__dict__:
            # the tool class overrides the `__init__` of base class
            for param in inspect.signature(tool_cls).parameters.values():
                if param.kind == inspect.Parameter.KEYWORD_ONLY:
                    # once we encounter a keyword-only parameter, we stop injecting dependencies
                    break
                # all positional parameters should be dependencies to be injected
                if param.annotation not in dependencies:
                    raise ValueError(f"Tool dependency not found: {param.annotation}")
                args.append(dependencies[param.annotation])
        return tool_cls(*args)

    # TODO(rc): remove `in_background` parameter and always load in background
    async def load_mcp_tools(
        self, mcp_configs: list[MCPConfig], runtime: Runtime, in_background: bool = True
    ) -> None:
        """
        Load MCP tools from specified MCP configs.

        Raises:
            MCPRuntimeError(PythinkerCLIException, RuntimeError): When any MCP server cannot be
                connected.
        """
        import fastmcp
        from fastmcp.mcp_config import MCPConfig, RemoteMCPServer

        async def _check_oauth_tokens(server_url: str) -> bool:
            """Check if OAuth tokens exist for the server."""
            try:
                from fastmcp.client.auth import oauth as fastmcp_oauth

                file_token_storage = getattr(fastmcp_oauth, "FileTokenStorage", None)
                if file_token_storage is not None:
                    storage: Any = file_token_storage(server_url=server_url)
                else:
                    provider: Any = fastmcp_oauth.OAuth(mcp_url=server_url)
                    storage = provider.token_storage_adapter
                tokens = await storage.get_tokens()
                return tokens is not None
            except Exception:
                return False

        def _toast_mcp(message: str) -> None:
            if in_background:
                from pythinker_code.ui.shell.prompt import toast

                toast(
                    message,
                    duration=10.0,
                    topic="mcp",
                    immediate=True,
                    position="right",
                )

        oauth_servers: dict[str, str] = {}

        async def _connect_server(
            server_name: str, server_info: MCPServerInfo
        ) -> tuple[str, Exception | None]:
            if server_info.status != "pending":
                return server_name, None

            server_info.status = "connecting"

            async def _open_and_inventory() -> None:
                async with server_info.client as client:
                    skipped: list[str] = []
                    local_tools: list[MCPTool[Any]] = []
                    for tool in await client.list_tools():
                        if server_info.tool_filter and not server_info.tool_filter.allows(
                            tool.name
                        ):
                            skipped.append(tool.name)
                            continue
                        local_tools.append(
                            MCPTool(
                                server_name,
                                tool,
                                client,
                                runtime=runtime,
                                tool_filter=server_info.tool_filter,
                            )
                        )
                    if skipped:
                        logger.info(
                            "MCP server {server_name}: {n} tools filtered out by "
                            "mcp.json enabledTools/disabledTools: {names}",
                            server_name=server_name,
                            n=len(skipped),
                            names=", ".join(sorted(skipped)),
                        )
                    # Resources/prompts are optional MCP capabilities; a server
                    # that exposes none (or does not support the request) must
                    # still connect, so capture them best-effort (mcpext-1). A
                    # METHOD_NOT_FOUND means the capability is genuinely absent; any
                    # other error is surfaced (WARNING) rather than masked as "none".
                    local_resources = await _discover_optional_capability(
                        server_name, "resources", client.list_resources
                    )
                    local_prompts = await _discover_optional_capability(
                        server_name, "prompts", client.list_prompts
                    )
                    server_info.tools = local_tools
                    server_info.resources = local_resources
                    server_info.prompts = local_prompts

            try:
                # Bound connect+inventory: a hung server would otherwise block
                # every agent turn (the loop awaits MCP loading).
                await asyncio.wait_for(
                    _open_and_inventory(),
                    timeout=runtime.config.mcp.client.startup_timeout_ms / 1000,
                )

                self._register_mcp_tools(server_name, server_info.tools)
                for tool in server_info.tools:
                    runtime.mcp_tools[f"mcp__{server_name}__{tool.name}"] = tool

                server_info.status = "connected"
                logger.info("Connected MCP server: {server_name}", server_name=server_name)
                return server_name, None
            except Exception as e:
                from pythinker_code.telemetry.errors import report_handled_error

                report_handled_error(e, site="soul.toolset.mcp.connect")
                logger.error(
                    "Failed to connect MCP server: {server_name}, error: {error}",
                    server_name=server_name,
                    error=e,
                )
                server_info.status = "failed"
                server_info.error = _classify_mcp_connect_error(e, server_name)
                return server_name, e

        async def _connect():
            _toast_mcp("connecting to mcp servers...")
            unauthorized_servers: dict[str, str] = {}
            for server_name, server_info in self._mcp_servers.items():
                server_url = oauth_servers.get(server_name)
                if not server_url:
                    continue
                if not await _check_oauth_tokens(server_url):
                    logger.warning(
                        "Skipping OAuth MCP server '{server_name}': not authorized. "
                        "Run 'pythinker mcp auth {server_name}' first.",
                        server_name=server_name,
                    )
                    server_info.status = "unauthorized"
                    unauthorized_servers[server_name] = server_url

            tasks = [
                asyncio.create_task(_connect_server(server_name, server_info))
                for server_name, server_info in self._mcp_servers.items()
                if server_info.status == "pending"
            ]
            results = await asyncio.gather(*tasks) if tasks else []
            failed_servers = {name: error for name, error in results if error is not None}

            if failed_servers:
                _toast_mcp("mcp connection failed")
                raise MCPRuntimeError(f"Failed to connect MCP servers: {failed_servers}")
            if unauthorized_servers:
                _toast_mcp("mcp authorization needed")
            else:
                _toast_mcp("mcp servers connected")

        for mcp_config in mcp_configs:
            if not mcp_config.mcpServers:
                logger.debug("Skipping empty MCP config: {mcp_config}", mcp_config=mcp_config)
                continue

            for server_name, server_config in mcp_config.mcpServers.items():
                if isinstance(server_config, RemoteMCPServer) and server_config.auth == "oauth":
                    oauth_servers[server_name] = server_config.url

                client = fastmcp.Client(MCPConfig(mcpServers={server_name: server_config}))
                _configure_mcp_client_stderr_log(client, runtime, server_name)
                self._mcp_servers[server_name] = MCPServerInfo(
                    status="pending",
                    client=client,
                    tools=[],
                    resources=[],
                    prompts=[],
                    tool_filter=McpToolFilter.from_server_config(server_config),
                )

        if not any(server_info.status == "pending" for server_info in self._mcp_servers.values()):
            return

        if in_background:
            self._mcp_loading_task = asyncio.create_task(_connect())
        else:
            await _connect()

    def has_pending_mcp_tools(self) -> bool:
        """Return True if the background MCP tool-loading task is still running."""
        return self._mcp_loading_task is not None and not self._mcp_loading_task.done()

    async def wait_for_mcp_tools(self) -> None:
        """Wait for background MCP tool loading to finish."""
        task = self._mcp_loading_task
        if not task:
            return
        try:
            await task
        finally:
            if self._mcp_loading_task is task and task.done():
                self._mcp_loading_task = None

    async def cleanup(self) -> None:
        """Cleanup any resources held by the toolset."""
        self._deferred_mcp_load = None
        if self._mcp_loading_task:
            self._mcp_loading_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._mcp_loading_task

        # Close every MCP client concurrently with a per-server timeout, so one
        # hung or slow client cannot block teardown of the rest (mcpext-3).
        async def _close(info: MCPServerInfo) -> None:
            try:
                await asyncio.wait_for(info.client.close(), timeout=_MCP_CLOSE_TIMEOUT_S)
            except Exception as exc:
                logger.debug("MCP client close failed/timed out: {error}", error=exc)

        await asyncio.gather(*(_close(info) for info in self._mcp_servers.values()))


@dataclass(slots=True)
class MCPServerInfo:
    status: Literal["pending", "connecting", "connected", "failed", "unauthorized"]
    client: fastmcp.Client[Any]
    tools: list[MCPTool[Any]]
    # Resources and prompts published by the server, captured at connect time
    # (mcpext-1). Empty for servers that expose none or do not support them.
    resources: list[mcp.Resource]
    prompts: list[mcp.types.Prompt]
    # One short actionable line explaining a failed connect, surfaced by /mcp.
    error: str | None = None
    # Optional mcp.json enabledTools/disabledTools scoping for this server.
    tool_filter: McpToolFilter | None = None


class MCPTool[T: ClientTransport](CallableTool):
    external_side_effect_tool: ClassVar[bool] = True
    """Marks tool adapters whose side effects cannot be statically classified.

    Consumed by the permission guard in ``permission.check_tool_call_allowed``
    — which routes flagged tools through ``check_external_tool_allowed`` — and
    by profile-gated tool visibility filtering
    (``PythinkerToolset._is_tool_visible``). Removing or failing to set this
    flag on an external adapter disables its permission gating.
    """

    emits_tool_execution_started_after_approval: ClassVar[bool] = True
    """Defer ToolExecutionStarted until ``approval.request`` resolves.

    ``__call__`` requests approval as its first step, and ``Approval.request``
    emits the started event after resolution (idempotent per call id), so the
    UI shows the approval prompt before the tool reads as "running" — the same
    ordering as Shell/WriteFile. The old ``_approval`` duck-typing missed this
    class because it requests via ``runtime.approval`` instead.
    """

    def __init__(
        self,
        server_name: str,
        mcp_tool: mcp.Tool,
        client: fastmcp.Client[T],
        *,
        runtime: Runtime,
        tool_filter: McpToolFilter | None = None,
        **kwargs: Any,
    ):
        super().__init__(
            name=mcp_tool.name,
            description=(
                f"This is an MCP tool from the already-connected MCP server `{server_name}`. "
                "Call it directly like any built-in tool — do NOT pip install it, import it as a "
                "Python module, or search the repo for its configuration; the server is already "
                "wired into your toolset.\n\n"
                f"{mcp_tool.description or 'No description provided.'}"
            ),
            parameters=mcp_tool.inputSchema,
            **kwargs,
        )
        self._mcp_tool = mcp_tool
        self._mcp_server_name = server_name
        self._client = client
        self._runtime = runtime
        self._timeout = timedelta(milliseconds=runtime.config.mcp.client.tool_call_timeout_ms)
        self._action_name = f"mcp:{mcp_tool.name}"
        self._tool_filter = tool_filter

    @property
    def mcp_server_name(self) -> str:
        """Name of the MCP server this tool belongs to."""
        return self._mcp_server_name

    @property
    def supports_parallel(self) -> bool:
        """MCP tools stay exclusive unless locally proven safe.

        Server-supplied annotations are untrusted remote metadata, so they must
        not relax local write serialization.
        """
        return False

    async def __call__(self, *args: Any, **kwargs: Any) -> ToolReturnValue:
        # Call-time re-check of the list-time filter: defense in depth for
        # tool maps shared across agents (e.g. runtime.mcp_tools handed to
        # subagent specs) and future live tools/list_changed updates.
        if self._tool_filter is not None and not self._tool_filter.allows(self._mcp_tool.name):
            return ToolError(
                message=(
                    f"MCP tool '{self._mcp_tool.name}' is disabled for server "
                    f"'{self._mcp_server_name}' by mcp.json tool filtering."
                ),
                brief="Tool disabled",
            )
        description = f"Call MCP tool `{self._mcp_tool.name}`."
        result = await self._runtime.approval.request(self.name, self._action_name, description)
        if not result:
            return result.rejection_error()

        from pythinker_code.telemetry import otel as _otel

        # `start_span` returns a sync context manager (the OTel SDK uses
        # `_AgnosticContextManager`, which intentionally has no __aenter__).
        # Keep it as a sync `with` and use `async with` only on the fastmcp
        # client.
        try:
            with _otel.start_span(
                "pythinker.mcp.call",
                {
                    "mcp.server": self._mcp_server_name,
                    "mcp.tool": self._mcp_tool.name,
                    "mcp.timeout_ms": int(self._timeout.total_seconds() * 1000),
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": self._mcp_tool.name,
                },
            ) as span:
                async with self._client as client:
                    result = await client.call_tool(
                        self._mcp_tool.name,
                        kwargs,
                        timeout=self._timeout,
                        raise_on_error=False,
                    )
                    span.set_attribute("mcp.is_error", bool(result.is_error))
                    if result.is_error:
                        logger.warning(
                            "MCP tool returned error: {tool_name}: {content}",
                            tool_name=self._mcp_tool.name,
                            content=[str(p) for p in result.content][:3],
                        )
                    return convert_mcp_tool_result(result)
        except Exception as e:
            from pythinker_code.telemetry.errors import report_handled_error

            # fastmcp raises `RuntimeError` on timeout and we cannot tell it from other errors
            exc_msg = str(e).lower()
            if "timeout" in exc_msg or "timed out" in exc_msg:
                report_handled_error(e, site="soul.toolset.mcp.call.timeout", tool="MCP")
                logger.warning(
                    "MCP tool call timed out: {tool_name}: {error}",
                    tool_name=self._mcp_tool.name,
                    error=e,
                )
                return ToolError(
                    message=(
                        f"Timeout while calling MCP tool `{self._mcp_tool.name}`. "
                        "You may explain to the user that the timeout config is set too low."
                    ),
                    brief="Timeout",
                )
            report_handled_error(e, site="soul.toolset.mcp.call", tool="MCP")
            logger.error(
                "MCP tool call failed: {tool_name}: {error}",
                tool_name=self._mcp_tool.name,
                error=e,
            )
            raise


class WireExternalTool(CallableTool):
    external_side_effect_tool: ClassVar[bool] = True
    """Marks tool adapters whose side effects cannot be statically classified.

    Consumed by the permission guard in ``permission.check_tool_call_allowed``
    — which routes flagged tools through ``check_external_tool_allowed`` — and
    by profile-gated tool visibility filtering
    (``PythinkerToolset._is_tool_visible``). Removing or failing to set this
    flag on an external adapter disables its permission gating.
    """

    def __init__(self, *, name: str, description: str, parameters: dict[str, Any]) -> None:
        super().__init__(
            name=name,
            description=description or "No description provided.",
            parameters=parameters,
        )

    async def __call__(self, *args: Any, **kwargs: Any) -> ToolReturnValue:
        tool_call = get_current_tool_call_or_none()
        if tool_call is None:
            return ToolError(
                message="External tool calls must be invoked from a tool call context.",
                brief="Invalid tool call",
            )

        from pythinker_code.soul import get_wire_or_none

        wire = get_wire_or_none()
        if wire is None:
            logger.error(
                "Wire is not available for external tool call: {tool_name}", tool_name=self.name
            )
            return ToolError(
                message="Wire is not available for external tool calls.",
                brief="Wire unavailable",
            )

        external_tool_call = ToolCallRequest.from_tool_call(tool_call)
        wire.soul_side.send(external_tool_call)
        try:
            return await external_tool_call.wait()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            from pythinker_code.telemetry.errors import report_handled_error

            report_handled_error(e, site="soul.toolset.external_tool", tool="External")
            logger.exception("External tool call failed: {tool_name}:", tool_name=self.name)
            return ToolError(
                message=f"External tool call failed: {e}",
                brief="External tool error",
            )


# Maximum characters allowed in MCP tool output before truncation.
# Built-in tools use 50K via ToolResultBuilder; MCP gets a wider budget because
# multi-part results (e.g. text + image) are common, but still needs a cap to
# prevent context overflow from tools like Playwright that return full DOMs.
MCP_MAX_OUTPUT_CHARS = 100_000


def _media_part_size(part: ContentPart) -> int | None:
    """Return the payload size of a media part, or ``None`` for non-media parts."""
    if isinstance(part, ImageURLPart):
        return len(part.image_url.url)
    if isinstance(part, AudioURLPart):
        return len(part.audio_url.url)
    if isinstance(part, VideoURLPart):
        return len(part.video_url.url)
    return None


def convert_mcp_tool_result(result: CallToolResult) -> ToolReturnValue:
    """Convert MCP tool result to Pythinker Core tool return value.

    All content — text *and* inline media (``data:`` URLs) — is subject to
    a shared *MCP_MAX_OUTPUT_CHARS* character budget.  Text parts are
    truncated in-place; media parts that exceed the remaining budget are
    dropped and replaced with a descriptive placeholder.

    Unsupported content types are caught and replaced with a ``TextPart``
    placeholder instead of crashing the turn.
    """
    content: list[ContentPart] = []
    char_budget = MCP_MAX_OUTPUT_CHARS
    truncated = False

    for part in result.content:
        try:
            converted = convert_mcp_content(part)
        except ValueError as exc:
            logger.warning(
                "Skipping unsupported MCP content part: {error}",
                error=exc,
            )
            converted = TextPart(text=f"[Unsupported content: {exc}]")

        # --- budget enforcement (text) ---
        if isinstance(converted, TextPart):
            if char_budget <= 0:
                truncated = True
                continue
            if len(converted.text) > char_budget:
                converted = TextPart(text=converted.text[:char_budget])
                truncated = True
            char_budget -= len(converted.text)
            content.append(converted)
            continue

        # --- budget enforcement (media: image / audio / video) ---
        media_size = _media_part_size(converted)
        if media_size is not None:
            if media_size > char_budget:
                truncated = True
                continue  # drop the oversized media part silently
            char_budget -= media_size
            content.append(converted)
            continue

        # Unknown ContentPart subclass — pass through without budget impact
        content.append(converted)

    if truncated:
        content.append(
            TextPart(
                text=(
                    f"\n\n[Output truncated: exceeded {MCP_MAX_OUTPUT_CHARS} character limit. "
                    "Use pagination or more specific queries to get remaining content.]"
                )
            )
        )

    if result.is_error:
        return ToolError(
            output=content,
            message="Tool returned an error. The output may be error message or incomplete output",
            brief="",
        )
    else:
        return ToolOk(output=content)
