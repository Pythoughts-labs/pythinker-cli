import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Literal, override

from pydantic import BaseModel, Field
from pythinker_core.tooling import CallableTool2, ToolError, ToolReturnValue
from pythinker_host.path import HostPath

from pythinker_code.file_restore import create_file_restore_point
from pythinker_code.soul.agent import Runtime
from pythinker_code.soul.approval import Approval
from pythinker_code.soul.permission import check_file_mutation_allowed
from pythinker_code.tools.display import DisplayBlock
from pythinker_code.tools.file import classify_edit_action
from pythinker_code.tools.file.plan_mode import inspect_plan_edit_target
from pythinker_code.tools.utils import load_desc
from pythinker_code.utils.diff import build_diff_blocks
from pythinker_code.utils.file_read_cache import overwrite_is_stale
from pythinker_code.utils.logging import logger
from pythinker_code.utils.path import is_within_workspace

_BASE_DESCRIPTION = load_desc(Path(__file__).parent / "write.md")


class Params(BaseModel):
    path: str = Field(
        description=(
            "The path to the file to write. Absolute paths are required when writing files "
            "outside the working directory."
        )
    )
    content: str = Field(description="The content to write to the file")
    mode: Literal["overwrite", "append"] = Field(
        description=(
            "The mode to use to write to the file. "
            "Two modes are supported: `overwrite` for overwriting the whole file and "
            "`append` for appending to the end of an existing file."
        ),
        default="overwrite",
    )


class WriteFile(CallableTool2[Params]):
    name: str = "WriteFile"
    description: str = _BASE_DESCRIPTION
    params: type[Params] = Params
    emits_tool_execution_started_after_approval = True

    def __init__(self, runtime: Runtime, approval: Approval):
        super().__init__()
        self._runtime = runtime
        self._work_dir = runtime.builtin_args.PYTHINKER_WORK_DIR
        self._additional_dirs = runtime.additional_dirs
        self._approval = approval
        self._plan_mode_checker: Callable[[], bool] | None = None
        self._plan_file_path_getter: Callable[[], Path | None] | None = None

    def bind_plan_mode(
        self, checker: Callable[[], bool], path_getter: Callable[[], Path | None]
    ) -> None:
        """Bind plan mode state checker and plan file path getter."""
        self._plan_mode_checker = checker
        self._plan_file_path_getter = path_getter

    async def _reject_if_stale(self, p: HostPath, real_p: HostPath) -> ToolError | None:
        """Reject an overwrite when the file changed on disk since the agent last read it.

        Returns ``None`` (allow) when the agent never read this file (an ordinary
        first-contact write) or the file cannot be stat'd — only a genuine
        read-then-externally-modified-then-overwrite is blocked.
        """
        if await overwrite_is_stale(self._runtime.file_read_cache, p, real_p):
            return ToolError(
                message=(
                    "File has been modified since you last read it. Read it again before "
                    "overwriting it so you do not clobber the external changes."
                ),
                brief="Stale read",
            )
        return None

    def _validate_path(
        self,
        path: HostPath,
        real_p: HostPath,
        real_work: HostPath,
        real_add: list[HostPath],
    ) -> ToolError | None:
        """Validate that the path is safe to write.

        Uses `real_p` (symlink-resolved) for workspace checks while `path` is kept for
        user-facing messages so reported paths remain unchanged. The resolved
        workspace roots come from the caller so they are realpath'd only once.
        """
        if not is_within_workspace(real_p, real_work, real_add) and not path.is_absolute():
            return ToolError(
                message=(
                    f"`{path}` is not an absolute path. "
                    "You must provide an absolute path to write a file "
                    "outside the working directory."
                ),
                brief="Invalid path",
            )
        return None

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        # TODO: checks:
        # - check if the path may contain secrets
        if not params.path:
            return ToolError(
                message="File path cannot be empty.",
                brief="Empty file path",
            )

        try:
            raw = HostPath(params.path).expanduser()
            # Relative tool paths resolve against the runtime work dir
            # (override-aware), NOT the process cwd — an isolated child's
            # relative write must land in its worktree. `raw` keeps the
            # original form: the workspace-escape rule for relative paths
            # checks (and reports) what the caller actually passed.
            base_joined = raw if raw.is_absolute() else self._work_dir.joinpath(str(raw))
            p = base_joined.canonical()

            # Resolve the real (symlink-followed) path for security checks only.
            # os.path.realpath follows symlinks at every component including the leaf.
            # For a non-existent target it correctly resolves the existing parent and
            # appends the missing final component, keeping in-workspace new files in-workspace.
            real_p = await p.realpath()
            real_work = await self._work_dir.realpath()
            real_add = [await d.realpath() for d in self._additional_dirs]

            if err := self._validate_path(raw, real_p, real_work, real_add):
                return err

            plan_target = inspect_plan_edit_target(
                p,
                plan_mode_checker=self._plan_mode_checker,
                plan_file_path_getter=self._plan_file_path_getter,
            )
            if isinstance(plan_target, ToolError):
                return plan_target

            is_plan_file_write = plan_target.is_plan_target
            if err := check_file_mutation_allowed(
                self._runtime, is_plan_artifact=is_plan_file_write
            ):
                return err
            if is_plan_file_write and plan_target.plan_path is not None:
                plan_target.plan_path.parent.mkdir(parents=True, exist_ok=True)

            if not await p.parent.exists():
                return ToolError(
                    message=f"`{params.path}` parent directory does not exist.",
                    brief="Parent directory not found",
                )

            # Validate mode parameter
            if params.mode not in ["overwrite", "append"]:
                return ToolError(
                    message=(
                        f"Invalid write mode: `{params.mode}`. "
                        "Mode must be either `overwrite` or `append`."
                    ),
                    brief="Invalid write mode",
                )

            file_existed = await p.exists()
            # Stale-overwrite guard: if the agent read this file and it has since changed on
            # disk (user or another tool), overwriting it would silently clobber those
            # changes. Require a fresh read first. Only fires when a prior read is recorded,
            # so it never blocks an ordinary first-contact write.
            if (
                file_existed
                and params.mode == "overwrite"
                and (err := await self._reject_if_stale(p, real_p))
            ):
                return err
            old_text = None
            if file_existed:
                old_text = await p.read_text(encoding="utf-8", errors="replace")

            new_text = (
                params.content if params.mode == "overwrite" else (old_text or "") + params.content
            )
            diff_blocks: list[DisplayBlock] = await build_diff_blocks(
                str(p),
                old_text or "",
                new_text,
            )

            # Plan file writes are auto-approved; other writes need approval
            if not is_plan_file_write:
                action = classify_edit_action(real_p, real_work, real_add)

                # Request approval
                result = await self._approval.request(
                    self.name,
                    action,
                    f"Write file `{p}`",
                    display=diff_blocks,
                )
                if not result:
                    return result.rejection_error()

            # Re-check staleness after approval: the prompt is unbounded user time
            # during which the file can change on disk, and the overwrite below writes
            # params.content wholesale. The first check cannot cover this window; the
            # read-cache is only refreshed after the write, so the read-state is valid.
            if (
                file_existed
                and params.mode == "overwrite"
                and (err := await self._reject_if_stale(p, real_p))
            ):
                return err

            from pythinker_code.soul.toolset import emit_current_tool_execution_started

            emit_current_tool_execution_started()
            create_file_restore_point(self._runtime.session, tool_name=self.name, path=str(p))

            # Write content to file
            match params.mode:
                case "overwrite":
                    await p.write_text(params.content, encoding="utf-8")
                case "append":
                    await p.append_text(params.content)

            # Get file info for the success message, and refresh the read-state to the post-write
            # (mtime, size) so the agent can immediately re-edit its own output without a false
            # stale flag. The write already succeeded above, so a stat hiccup here must NOT be
            # reported as a write failure — suppress it (the cache is simply not refreshed) and
            # omit the size note, matching the post-op stat handling in read.py and replace.py.
            file_size: int | None = None
            with contextlib.suppress(OSError):
                stat_after = await p.stat()
                file_size = stat_after.st_size
                self._runtime.file_read_cache.record(real_p, stat_after.st_mtime, file_size)
            if self._runtime.lsp and self._runtime.rearm_injection:
                file_uri = Path(str(p)).resolve().as_uri()
                self._runtime.lsp.diagnostics.clear_for_file(file_uri)
                try:
                    await self._runtime.lsp.change_file(str(p), new_text)
                    await self._runtime.lsp.save_file(str(p))
                    self._runtime.rearm_injection("lsp_diagnostics")
                except Exception:
                    logger.warning(
                        "LSP notification failed for {path}; skipping diagnostic rearm",
                        path=p,
                    )
            action = "overwritten" if params.mode == "overwrite" else "appended to"
            size_note = f" Current size: {file_size} bytes." if file_size is not None else ""
            return ToolReturnValue(
                is_error=False,
                output="",
                message=f"File successfully {action}.{size_note}",
                display=diff_blocks,
            )

        except Exception as e:
            from pythinker_code.telemetry.errors import report_handled_error

            report_handled_error(e, site="tool.write", tool="WriteFile")
            logger.warning("WriteFile failed: {path}: {error}", path=params.path, error=e)
            return ToolError(
                message=f"Failed to write to {params.path}. Error: {e}",
                brief="Failed to write file",
            )
