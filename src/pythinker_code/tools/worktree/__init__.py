import re
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, override

from pydantic import BaseModel, Field
from pythinker_core.tooling import CallableTool2, ToolReturnValue
from pythinker_host.path import HostPath

from pythinker_code.soul.agent import Runtime
from pythinker_code.soul.toolset import PythinkerToolset
from pythinker_code.subagents.worktree import WorktreeError, create_agent_worktree
from pythinker_code.tools.utils import ToolResultStatus, load_desc, tool_error


@dataclass(frozen=True, slots=True)
class _SessionWorktreeState:
    original_work_dir: HostPath
    worktree_path: HostPath


class EnterWorktreeParams(BaseModel):
    name: str | None = Field(
        default=None,
        description=(
            "Optional short suffix for the worktree directory. Ignored when `path` is provided."
        ),
    )
    path: str | None = Field(
        default=None,
        description=(
            "Optional absolute destination path for the worktree. Defaults to a session worktrees "
            "directory."
        ),
    )


class ExitWorktreeParams(BaseModel):
    pass


_ACTIVE_WORKTREES: dict[int, _SessionWorktreeState] = {}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_name(name: str | None) -> str:
    if not name:
        return "session"
    return _SAFE_NAME_RE.sub("-", name).strip(".-") or "session"


def _default_worktree_path(runtime: Runtime, name: str | None) -> Path:
    return runtime.session.dir / "worktrees" / _safe_name(name)


def _path_param_to_dest(runtime: Runtime, params: EnterWorktreeParams) -> Path | ToolReturnValue:
    if params.path is None:
        return _default_worktree_path(runtime, params.name)
    raw = Path(params.path).expanduser()
    if not raw.is_absolute():
        return tool_error(
            "`path` must be absolute when provided.",
            brief="Invalid worktree path",
            status=ToolResultStatus.error,
        )
    return raw


def _active_state(runtime: Runtime) -> _SessionWorktreeState | None:
    """Return active worktree state, pruning stale process-local entries."""
    state_key = id(runtime)
    state = _ACTIVE_WORKTREES.get(state_key)
    if state is not None and runtime.work_dir != state.worktree_path:
        _ACTIVE_WORKTREES.pop(state_key, None)
        return None
    return state


class EnterWorktree(CallableTool2[EnterWorktreeParams]):
    name: str = "EnterWorktree"
    description: str = load_desc(Path(__file__).parent / "enter_worktree.md")
    params: type[EnterWorktreeParams] = EnterWorktreeParams
    emits_tool_execution_started_after_approval: ClassVar[bool] = True
    external_side_effect_tool: ClassVar[bool] = True
    """Worktree creation mutates git state and must pass the external side-effect gate."""

    def __init__(self, runtime: Runtime, toolset: PythinkerToolset) -> None:
        super().__init__()
        self._runtime = runtime
        self._toolset = toolset

    @override
    async def __call__(self, params: EnterWorktreeParams) -> ToolReturnValue:
        if self._runtime.role != "root":
            return tool_error(
                "EnterWorktree is only available in the root session.",
                brief="Worktree unavailable",
                status=ToolResultStatus.denied,
            )
        state_key = id(self._runtime)
        state = _active_state(self._runtime)
        if state is not None:
            return tool_error(
                f"A session worktree is already active at {state.worktree_path}. "
                "Call ExitWorktree before entering another worktree.",
                brief="Worktree already active",
            )

        dest_or_error = _path_param_to_dest(self._runtime, params)
        if not isinstance(dest_or_error, Path):
            return dest_or_error
        dest = dest_or_error
        original = self._runtime.work_dir

        approval = await self._runtime.approval.request(
            self.name,
            "create git worktree",
            f"Create session git worktree `{dest}` from `{original}`",
        )
        if not approval:
            return approval.rejection_error()

        try:
            from pythinker_code.soul.toolset import emit_current_tool_execution_started

            emit_current_tool_execution_started()
            await create_agent_worktree(Path(str(original)), dest)
        except WorktreeError as exc:
            return tool_error(
                str(exc),
                brief="Worktree creation failed",
                status=ToolResultStatus.failure,
            )

        worktree_host_path = HostPath.unsafe_from_local_path(dest.resolve())
        effective = self._toolset.set_work_dir_override(worktree_host_path)
        if effective != worktree_host_path:
            self._toolset.set_work_dir_override(None)
            return tool_error(
                "Worktree was created, but the session working directory could not be changed.",
                brief="Worktree switch failed",
                status=ToolResultStatus.failure,
            )

        _ACTIVE_WORKTREES[state_key] = _SessionWorktreeState(
            original_work_dir=original,
            worktree_path=worktree_host_path,
        )
        output = "\n".join(
            [
                "session_worktree: entered",
                f"worktree_path: {worktree_host_path}",
                f"original_work_dir: {original}",
                "cleanup: retained until you remove it explicitly",
            ]
        )
        return ToolReturnValue(
            is_error=False,
            output=output,
            message=f"Session working directory changed to {worktree_host_path}.",
            display=[],
            extras={"status": ToolResultStatus.success.value},
        )


class ExitWorktree(CallableTool2[ExitWorktreeParams]):
    name: str = "ExitWorktree"
    description: str = load_desc(Path(__file__).parent / "exit_worktree.md")
    params: type[ExitWorktreeParams] = ExitWorktreeParams
    external_side_effect_tool: ClassVar[bool] = True
    """Restoring session workdir is process-local but paired with a side-effecting tool."""

    def __init__(self, runtime: Runtime, toolset: PythinkerToolset) -> None:
        super().__init__()
        self._runtime = runtime
        self._toolset = toolset

    @override
    async def __call__(self, params: ExitWorktreeParams) -> ToolReturnValue:
        if self._runtime.role != "root":
            return tool_error(
                "ExitWorktree is only available in the root session.",
                brief="Worktree unavailable",
                status=ToolResultStatus.denied,
            )
        state_key = id(self._runtime)
        state = _active_state(self._runtime)
        if state is None:
            return tool_error(
                "No session worktree is active.",
                brief="No active worktree",
                status=ToolResultStatus.failure,
            )

        effective = self._toolset.set_work_dir_override(None)
        if effective != state.original_work_dir:
            return tool_error(
                "Could not restore the original working directory.",
                brief="Worktree exit failed",
                status=ToolResultStatus.failure,
            )
        _ACTIVE_WORKTREES.pop(state_key, None)
        output = "\n".join(
            [
                "session_worktree: exited",
                f"worktree_path: {state.worktree_path}",
                f"restored_work_dir: {state.original_work_dir}",
                "retained: true",
                "cleanup: worktree was not deleted; remove it manually when finished",
            ]
        )
        return ToolReturnValue(
            is_error=False,
            output=output,
            message=f"Session working directory restored to {state.original_work_dir}.",
            display=[],
            extras={"status": ToolResultStatus.success.value},
        )
