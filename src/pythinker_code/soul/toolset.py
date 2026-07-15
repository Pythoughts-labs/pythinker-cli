from __future__ import annotations

import asyncio
import contextlib
import difflib
import importlib
import inspect
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast, overload

from pythinker_core.tooling import (
    CallableTool,
    HandleResult,
    Tool,
    ToolBatchContext,
    ToolBatchHandle,
    ToolError,
    ToolOk,
    Toolset,
)
from pythinker_core.tooling.mcp import convert_mcp_content
from pythinker_core.utils.typing import JsonType
from pythinker_host.path import HostPath

from pythinker_code.exception import InvalidToolError, MCPRuntimeError
from pythinker_code.hooks.engine import HookEngine
from pythinker_code.soul import tool_execution as _tool_execution
from pythinker_code.soul.tool_execution import (
    ReadWriteGate,
    ToolCallKey,
    ToolExecutionEngine,
    ToolType,
    get_current_tool_call_or_none,
    tool_defers_execution_started,
)
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
    ToolResult,
    ToolReturnValue,
    VideoURLPart,
)

if TYPE_CHECKING:
    import fastmcp
    import mcp
    from fastmcp.client.client import CallToolResult
    from fastmcp.client.transports import ClientTransport
    from fastmcp.mcp_config import MCPConfig

    from pythinker_code.soul.agent import Runtime

current_tool_call = _tool_execution.current_tool_call
emit_current_tool_execution_started = _tool_execution.emit_current_tool_execution_started
get_session_id = _tool_execution.get_session_id
set_session_id = _tool_execution.set_session_id
_ReadWriteGate = ReadWriteGate
_tool_defers_execution_started = tool_defers_execution_started

# Per-server timeout for closing MCP clients during teardown, so one hung client
# cannot block cleanup of the rest (mcpext-3).
_MCP_CLOSE_TIMEOUT_S = 5.0

_MCP_LOG_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


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


def _make_mcp_live_refresh_handler(
    client: Any,
    toolset: PythinkerToolset,
    runtime: Runtime,
    server_name: str,
) -> Any:
    """Message handler that refreshes inventory on MCP list_changed notifications."""
    from fastmcp.client.tasks import TaskNotificationHandler

    class _McpLiveRefreshHandler(TaskNotificationHandler):
        async def on_tool_list_changed(
            self, message: mcp.types.ToolListChangedNotification
        ) -> None:
            await self._refresh_inventory("tools")

        async def on_resource_list_changed(
            self, message: mcp.types.ResourceListChangedNotification
        ) -> None:
            await self._refresh_inventory("resources")

        async def on_prompt_list_changed(
            self, message: mcp.types.PromptListChangedNotification
        ) -> None:
            await self._refresh_inventory("prompts")

        async def _refresh_inventory(self, capability: str) -> None:
            info = toolset.mcp_servers.get(server_name)
            if info is None or info.status != "connected":
                return
            try:
                await toolset.refresh_mcp_server(server_name, runtime)
            except Exception as exc:
                logger.warning(
                    "MCP server {server_name} live {capability} refresh failed: {error}",
                    server_name=server_name,
                    capability=capability,
                    error=exc,
                )

    return _McpLiveRefreshHandler(client)


def _configure_mcp_client_handlers(
    client: Any,
    toolset: PythinkerToolset,
    runtime: Runtime,
    server_name: str,
) -> None:
    _configure_mcp_client_stderr_log(client, runtime, server_name)
    client._session_kwargs["message_handler"] = _make_mcp_live_refresh_handler(
        client, toolset, runtime, server_name
    )


async def _hold_mcp_session(server_name: str, info: MCPServerInfo) -> None:
    """Keep one MCP client session open so list_changed notifications can arrive."""
    stop = asyncio.Event()
    info.session_stop = stop
    try:
        async with info.client:
            await stop.wait()
    except Exception as exc:
        logger.debug(
            "MCP session holder exited for {server_name}: {error}",
            server_name=server_name,
            error=exc,
        )
    finally:
        info.session_stop = None
        info.session_holder_task = None


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


if TYPE_CHECKING:

    def type_check(pythinker_toolset: PythinkerToolset):
        _: Toolset = pythinker_toolset


class PythinkerToolset:
    def __init__(self, runtime: Runtime | None = None) -> None:
        self._runtime = runtime
        self._tool_dict: dict[str, ToolType] = {}
        self._hidden_tools: set[str] = set()
        self._mcp_servers: dict[str, MCPServerInfo] = {}
        self._mcp_loading_task: asyncio.Task[None] | None = None
        self._deferred_mcp_load: tuple[list[MCPConfig], Runtime] | None = None
        self._hook_engine: HookEngine = HookEngine()

        self._execution = ToolExecutionEngine(
            runtime,
            lambda name: self._tool_dict.get(name),
            lambda: list(self._tool_dict),
            lambda: self._hook_engine,
            lambda tool, arguments: self._gated_call(tool, arguments),
        )

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

    def _publish_connected_mcp_tools(self, runtime: Runtime) -> None:
        """Publish connected MCP tools in configured server order.

        Servers connect concurrently, so registering inside each connection task
        makes duplicate tool-name resolution depend on task completion order.
        Publishing after the gather keeps the collision policy deterministic.
        """
        for server_name, server_info in self._mcp_servers.items():
            if server_info.status != "connected":
                continue
            self._register_mcp_tools(server_name, server_info.tools)
            for tool in server_info.tools:
                from pythinker_code.utils.mcp_names import mcp_tool_runtime_key

                runtime.mcp_tools[mcp_tool_runtime_key(server_name, tool.name)] = tool

    def _rebuild_published_mcp_tools(self, runtime: Runtime) -> None:
        """Atomically rebuild the published MCP tool registry from connected servers.

        Drop every currently-published MCP tool (non-MCP tools are preserved), then
        republish all *connected* servers in configured order via
        :meth:`_publish_connected_mcp_tools`. This keeps last-wins collision order
        deterministic and lets a disconnect/refresh re-claim a tool name another
        still-connected server provides, instead of orphaning it. The method runs
        synchronously (no ``await`` between the drop and the republish), so the two
        registries are never observed half-rebuilt.
        """
        prior_tools = dict(self._tool_dict)
        prior_runtime_tools = dict(runtime.mcp_tools)
        try:
            stale = [name for name, tool in self._tool_dict.items() if isinstance(tool, MCPTool)]
            for name in stale:
                del self._tool_dict[name]
            runtime.mcp_tools.clear()
            self._publish_connected_mcp_tools(runtime)
        except Exception:
            self._tool_dict.clear()
            self._tool_dict.update(prior_tools)
            runtime.mcp_tools.clear()
            runtime.mcp_tools.update(prior_runtime_tools)
            raise

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

        # Hide ToolSearch unless the active model genuinely supports the deferred
        # tool-search workflow. Compat proxies that declare type="anthropic"
        # (z.ai/GLM, Kimi, MiniMax, opencode) and non-Anthropic providers do not
        # forward the tool_reference/defer_loading beta, so ToolSearch is noise
        # there and weaker tool-callers (e.g. GLM-5.2) loop on it forever instead
        # of calling tools directly. See llm.supports_deferred_tool_search for the
        # full rationale — this is deliberate, do not drop it.
        if tool.name == "ToolSearch":
            from pythinker_code.llm import supports_deferred_tool_search

            if not supports_deferred_tool_search(runtime.llm):
                return False

        if tool.name == "EnterPlanMode" and runtime.session.state.plan_mode:
            return False
        if tool.name == "ExitPlanMode" and not runtime.session.state.plan_mode:
            return False

        if _is_external_side_effect_tool(tool):
            return profile.allow_file_mutation and profile.allow_shell_mutation

        return True

    @property
    def _concurrency_gate(self) -> _ReadWriteGate:
        """Compatibility seam for local characterization probes."""
        return self._execution._concurrency_gate  # pyright: ignore[reportPrivateUsage]

    async def _gated_call(self, tool: ToolType, arguments: JsonType) -> ToolReturnValue:
        return await self._execution.gated_call(tool, arguments)

    def begin_step(
        self,
        previous_calls: list[ToolCallKey],
        *,
        step_no: int = 0,
        turn_id: str = "",
    ) -> None:
        """Prepare execution state for one legacy caller step."""
        self._execution.begin_step(previous_calls, step_no=step_no, turn_id=turn_id)

    def end_step(self) -> list[ToolCallKey]:
        """Finalize and return the current step's normalized call fingerprints."""
        return self._execution.end_step()

    @property
    def dedup_triggered(self) -> bool:
        return self._execution.dedup_triggered

    @property
    def consecutive_repeat_count(self) -> int:
        return self._execution.consecutive_repeat_count

    def handle(self, tool_call: ToolCall) -> HandleResult:
        """Compatibility path for third-party per-call core dispatch."""
        return self._execution.handle(tool_call)

    def handle_batch(
        self,
        tool_calls: Sequence[ToolCall],
        context: ToolBatchContext,
        *,
        on_tool_result: Callable[[ToolResult], None] | None = None,
    ) -> ToolBatchHandle:
        """Create one supervised execution batch after terminal response assembly."""
        calls = tuple(tool_calls)
        return self._execution.handle_batch(calls, context, on_tool_result=on_tool_result)

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
                The message lists each bad tool with the actual failure reason
                (module missing, class missing, or constructor exception) so a
                stale-binary or typo case is diagnosable from the traceback
                without grepping the log file.
        """

        good_tools: list[str] = []
        bad_tools: list[tuple[str, str]] = []

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
            except Exception as exc:  # noqa: BLE001 - aggregate per-tool failures, re-raise below
                # A constructor error (missing dep, type mismatch, binary built
                # before the tool was added) is a per-tool configuration bug.
                # Catch it here so the aggregated error names which tool failed
                # and why, instead of bubbling a bare traceback out of agent
                # load. The whole load still aborts on any failure — agent.yaml
                # tool references are hard requirements — but the user now gets
                # a one-line pointer to the offending tool.
                reason = f"{type(exc).__name__}: {exc}"
                # logger.exception keeps the full traceback in the log next to
                # the aggregated error, so a stale-binary / import-time
                # constructor failure stays diagnosable (the aggregated
                # InvalidToolError carries only the one-line reason).
                logger.exception(
                    "Tool load failed: {tool_path}: {reason}",
                    tool_path=tool_path,
                    reason=reason,
                )
                bad_tools.append((tool_path, reason))
                continue
            if tool:
                self.add(tool)
                good_tools.append(tool_path)
            else:
                # _load_tool returns None only for the known import / class
                # miss paths, both already logged with a reason. Surface a
                # generic placeholder so the aggregated error still names
                # the tool.
                bad_tools.append((tool_path, "class or module not found"))
        logger.info("Loaded tools: {good_tools}", good_tools=good_tools)
        if bad_tools:
            lines = ["Invalid tools:"]
            for path, reason in bad_tools:
                lines.append(f"  - {path}: {reason}")
            raise InvalidToolError("\n".join(lines))

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
            # Best-effort "did you mean" — points users at the actual class
            # name when they typo'd it. Only attach to the warning, not the
            # aggregated error, so the log search stays one line per failure.
            suggestion = ""
            try:
                available = [n for n in dir(module) if not n.startswith("_")]
                matches = difflib.get_close_matches(class_name, available, n=1, cutoff=0.6)
                if matches:
                    suggestion = f" Did you mean {matches[0]!r}?"
            except Exception:  # noqa: BLE001 - dir() / difflib can't realistically fail, fail open
                suggestion = ""
            logger.warning(
                "Tool class not found: {class_name} in {module_name}{suggestion}",
                class_name=class_name,
                module_name=module_name,
                suggestion=suggestion,
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
            return await self._connect_mcp_server(server_name, server_info, runtime)

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

            # Publish before raising so servers that DID connect become callable in
            # this session even when another server fails the aggregate connect.
            self._publish_connected_mcp_tools(runtime)
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
                _configure_mcp_client_handlers(client, self, runtime, server_name)
                self._mcp_servers[server_name] = MCPServerInfo(
                    status="pending",
                    client=client,
                    tools=[],
                    resources=[],
                    prompts=[],
                    tool_filter=McpToolFilter.from_server_config(server_config),
                    server_config=server_config,
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

    async def _inventory_mcp_server(
        self, server_name: str, server_info: MCPServerInfo, runtime: Runtime
    ) -> tuple[list[MCPTool[Any]], list[mcp.Resource], list[mcp.types.Prompt]]:
        """Discover a server's tools/resources/prompts without mutating it.

        Returns the freshly discovered inventory; the caller assigns it onto
        ``server_info`` only after the awaited call (and the client context exit)
        fully succeeds, so a timeout or ``__aexit__`` failure never leaves the
        published registry inconsistent with the exposed callable tools.
        """
        async with server_info.client as client:
            skipped: list[str] = []
            local_tools: list[MCPTool[Any]] = []
            for tool in await client.list_tools():
                if server_info.tool_filter and not server_info.tool_filter.allows(tool.name):
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
            resources = await _discover_optional_capability(
                server_name, "resources", client.list_resources
            )
            prompts = await _discover_optional_capability(
                server_name, "prompts", client.list_prompts
            )
            return local_tools, resources, prompts

    async def _connect_mcp_server(
        self, server_name: str, server_info: MCPServerInfo, runtime: Runtime
    ) -> tuple[str, Exception | None]:
        try:
            tools, resources, prompts = await asyncio.wait_for(
                self._inventory_mcp_server(server_name, server_info, runtime),
                timeout=runtime.config.mcp.client.startup_timeout_ms / 1000,
            )
            # Assign only after the awaited inventory (and client context exit)
            # succeeded, so a failure never leaves a half-applied inventory.
            server_info.tools = tools
            server_info.resources = resources
            server_info.prompts = prompts
            server_info.status = "connected"
            server_info.error = None
            self._start_mcp_session_holder(server_name, server_info)
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

    def _ensure_mcp_idle(self) -> None:
        if self._mcp_loading_task is not None and not self._mcp_loading_task.done():
            raise MCPRuntimeError("MCP servers are still loading")

    def _start_mcp_session_holder(self, server_name: str, info: MCPServerInfo) -> None:
        task = info.session_holder_task
        if task is not None and not task.done():
            return
        info.session_holder_task = asyncio.create_task(_hold_mcp_session(server_name, info))

    async def _stop_mcp_session_holder(self, info: MCPServerInfo) -> None:
        if info.session_stop is not None:
            info.session_stop.set()
        task = info.session_holder_task
        if task is None:
            return
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def disconnect_mcp_server(self, server_name: str, runtime: Runtime) -> None:
        """Disconnect one MCP server and unregister its tools."""
        self._ensure_mcp_idle()
        info = self._mcp_servers.get(server_name)
        if info is None:
            raise MCPRuntimeError(f"Unknown MCP server: {server_name}")
        await self._stop_mcp_session_holder(info)
        # Drop this server's inventory, then rebuild the published registry so any
        # tool name it was shadowing falls back to another still-connected server.
        info.tools = []
        self._rebuild_published_mcp_tools(runtime)
        prior_error = info.error
        close_error: str | None = None
        try:
            await asyncio.wait_for(info.client.close(), timeout=_MCP_CLOSE_TIMEOUT_S)
        except TimeoutError as exc:
            logger.warning(
                "MCP disconnect close timed out for {server_name}: {error}",
                server_name=server_name,
                error=exc,
            )
            close_error = "disconnect timed out while closing the MCP session"
        except Exception as exc:
            logger.warning(
                "MCP disconnect close failed for {server_name}: {error}",
                server_name=server_name,
                error=exc,
            )
            close_error = f"disconnect failed while closing the MCP session: {exc}"
        info.status = "failed"
        if close_error is not None:
            if prior_error is None:
                info.error = close_error
        else:
            info.error = "disconnected"
        info.resources = []
        info.prompts = []

    def connected_mcp_server_names(self) -> tuple[str, ...]:
        return tuple(name for name, info in self._mcp_servers.items() if info.status == "connected")

    async def refresh_mcp_server(self, server_name: str, runtime: Runtime) -> None:
        """Re-list tools/resources/prompts for a connected MCP server."""
        self._ensure_mcp_idle()
        info = self._mcp_servers.get(server_name)
        if info is None:
            raise MCPRuntimeError(f"Unknown MCP server: {server_name}")
        if info.status != "connected":
            raise MCPRuntimeError(
                f"MCP server '{server_name}' is not connected (status={info.status})"
            )
        # Inventory first: on failure ``info.tools`` keeps its last-known-good value
        # and the live registry is untouched, so a failed refresh never drops tools.
        # Convert raw timeout/inventory errors to MCPRuntimeError so callers (e.g. the
        # /mcp slash handler) receive a single typed boundary error.
        try:
            tools, resources, prompts = await asyncio.wait_for(
                self._inventory_mcp_server(server_name, info, runtime),
                timeout=runtime.config.mcp.client.startup_timeout_ms / 1000,
            )
        except TimeoutError as exc:
            raise MCPRuntimeError(f"Refresh of MCP server '{server_name}' timed out") from exc
        except Exception as exc:
            raise MCPRuntimeError(f"Failed to refresh MCP server '{server_name}': {exc}") from exc
        # Inventory succeeded; swap the old inventory for the new one atomically,
        # then rebuild the published registry from it.
        info.tools = tools
        info.resources = resources
        info.prompts = prompts
        self._rebuild_published_mcp_tools(runtime)

    async def reconnect_mcp_server(self, server_name: str, runtime: Runtime) -> None:
        """Close and reconnect one MCP server from its stored config."""
        import fastmcp
        from fastmcp.mcp_config import MCPConfig

        self._ensure_mcp_idle()
        info = self._mcp_servers.get(server_name)
        if info is None:
            raise MCPRuntimeError(f"Unknown MCP server: {server_name}")
        if info.server_config is None:
            raise MCPRuntimeError(f"MCP server '{server_name}' has no stored config to reconnect")
        await self.disconnect_mcp_server(server_name, runtime)
        info.client = fastmcp.Client(MCPConfig(mcpServers={server_name: info.server_config}))
        _configure_mcp_client_handlers(info.client, self, runtime, server_name)
        info.status = "pending"
        info.error = None
        _server_name, error = await self._connect_mcp_server(server_name, info, runtime)
        if error is not None:
            raise MCPRuntimeError(
                info.error or f"Failed to reconnect MCP server '{server_name}': {error}"
            )
        self._rebuild_published_mcp_tools(runtime)

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
            await self._stop_mcp_session_holder(info)
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
    # Original server config for per-server reconnect (mcpext-2).
    server_config: Any = None
    # Background task holding the client session open for list_changed notifications.
    session_stop: asyncio.Event | None = None
    session_holder_task: asyncio.Task[None] | None = None


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
        from pythinker_code.telemetry.names import sanitize_telemetry_tool_name

        telemetry_server = sanitize_telemetry_tool_name(self._mcp_server_name)
        telemetry_tool = sanitize_telemetry_tool_name(self._mcp_tool.name)
        # `start_span` returns a sync context manager (the OTel SDK uses
        # `_AgnosticContextManager`, which intentionally has no __aenter__).
        # Keep it as a sync `with` and use `async with` only on the fastmcp
        # client.
        try:
            with _otel.start_span(
                "pythinker.mcp.call",
                {
                    "mcp.server": telemetry_server,
                    "mcp.tool": telemetry_tool,
                    "mcp.timeout_ms": int(self._timeout.total_seconds() * 1000),
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": telemetry_tool,
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
