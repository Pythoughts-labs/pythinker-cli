"""LSP agent tool."""

# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

import asyncio
import contextlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, override
from urllib.parse import unquote

import pythinker_host
from pythinker_core.tooling import CallableTool2, ToolReturnValue
from pythinker_host.path import HostPath

from pythinker_code.lsp.recommend import get_matching_lsp_plugins
from pythinker_code.lsp.service import LspInitStatus
from pythinker_code.soul.agent import Runtime
from pythinker_code.tools import SkipThisTool
from pythinker_code.tools.lsp.formatters import MAX_RESULT_SIZE_CHARS, format_result
from pythinker_code.tools.lsp.schemas import Operation, Params
from pythinker_code.tools.lsp.symbol_context import get_symbol_at_position
from pythinker_code.tools.utils import ToolResultBuilder, load_desc
from pythinker_code.utils.logging import logger

MAX_LSP_FILE_SIZE_BYTES = 10_000_000
_GIT_CHECK_IGNORE_BATCH_SIZE = 50
_GIT_CHECK_IGNORE_TIMEOUT = 5.0


class Lsp(CallableTool2[Params]):
    name: str = "LSP"
    supports_parallel: bool = True
    description: str = load_desc(Path(__file__).parent / "tool.md", {})
    params: type[Params] = Params

    def __init__(self, runtime: Runtime) -> None:
        super().__init__()
        if not runtime.config.lsp.enabled or runtime.lsp is None:
            raise SkipThisTool()
        self._runtime = runtime
        self._lsp = runtime.lsp
        self._work_dir = runtime.work_dir
        self._recommended_exts: set[str] = set()

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        builder = ToolResultBuilder(max_chars=MAX_RESULT_SIZE_CHARS, max_line_length=None)

        if self._lsp.status() == LspInitStatus.PENDING:
            await self._lsp.wait_for_init()

        if self._lsp.status() != LspInitStatus.SUCCESS or self._lsp.manager is None:
            return builder.error(
                (
                    "LSP is still initializing or unavailable. "
                    "Try again after language servers finish starting."
                ),
                brief="LSP unavailable",
            )

        absolute_path, validation_error = await self._validate_file(params.file_path)
        if validation_error is not None:
            return validation_error

        # Re-check the manager after the validation await: a concurrent
        # reinitialize() can clear it (the property returns None during reinit),
        # so the earlier check at the top of __call__ may now be stale.
        manager = self._lsp.manager
        # pyright narrows the property from the check at the top of __call__ and
        # flags this as unreachable, but the value can change across the await
        # above (reinitialize clears it), so the re-check is deliberate.
        if manager is None:  # pyright: ignore[reportUnnecessaryComparison]
            return builder.error(
                (
                    "LSP is still initializing or unavailable. "
                    "Try again after language servers finish starting."
                ),
                brief="LSP unavailable",
            )
        assert absolute_path is not None

        if manager.server_for_file(absolute_path) is None:
            ext = Path(absolute_path).suffix
            builder.write(f"No LSP server available for file type: {ext or '(none)'}\n")
            hint = self._recommendation_hint(absolute_path)
            if hint:
                builder.write(hint)
            builder.mark_untrusted()
            return builder.ok(brief=self._brief(params))

        try:
            if not manager.is_file_open(absolute_path):
                size_error = await self._ensure_file_open(manager, absolute_path, params.file_path)
                if size_error is not None:
                    return size_error

            method, request_params = _method_and_params(params, absolute_path)
            # A None result here means the server ran and returned an empty/null
            # response (e.g. definition not found) — distinct from "no server",
            # which is handled above. format_result() renders empty as guidance.
            result = await manager.send_request(absolute_path, method, request_params)

            if params.operation in (Operation.INCOMING_CALLS, Operation.OUTGOING_CALLS):
                call_items = result if isinstance(result, list) else []
                if not call_items:
                    builder.write("No call hierarchy item found at this position\n")
                    builder.mark_untrusted()
                    return builder.ok(brief=self._brief(params))

                call_method = (
                    "callHierarchy/incomingCalls"
                    if params.operation == Operation.INCOMING_CALLS
                    else "callHierarchy/outgoingCalls"
                )
                result = await manager.send_request(
                    absolute_path,
                    call_method,
                    {"item": call_items[0]},
                )

            result = await _filter_gitignored_results(
                params.operation,
                result,
                str(self._work_dir),
            )

            formatted, _result_count, _file_count = format_result(
                params.operation,
                result,
                str(self._work_dir),
            )
            builder.write(formatted)
            builder.mark_untrusted()
            return builder.ok(brief=self._brief(params))
        except Exception as exc:
            logger.error(
                "LSP tool request failed for {operation} on {file_path}: {err}",
                operation=params.operation,
                file_path=params.file_path,
                err=exc,
            )
            return builder.error(
                f"Error performing {params.operation}: {exc}",
                brief=self._brief(params),
            )

    def _recommendation_hint(self, absolute_path: str) -> str | None:
        # CLI re-expression of the reference's plugin-recommendation menu: when the
        # agent hits a file type with no installed server, suggest a marketplace
        # plugin once per extension per session. Gated by recommendation_disabled /
        # recommendation_never inside get_matching_lsp_plugins. The reference's
        # persisted >=5 ignored-count auto-disable is intentionally NOT wired here:
        # it requires incremental writes to the shared global config, and the
        # current save_config() rewrites the whole file with no lock/atomic rename
        # (multi-instance clobber risk). Deferred until a safe global-write path
        # exists; the disabled/never flags still apply.
        ext = Path(absolute_path).suffix.lower()
        if not ext or ext in self._recommended_exts:
            return None
        self._recommended_exts.add(ext)
        try:
            matches = get_matching_lsp_plugins(absolute_path, self._runtime.config)
        except Exception:
            logger.debug("LSP plugin recommendation lookup failed for {ext}", ext=ext)
            return None
        if not matches:
            return None
        top = matches[0]
        return (
            f"\nTip: install the '{top.plugin_name}' plugin for {ext} code intelligence "
            f"(pythinker plugin add {top.plugin_id}).\n"
        )

    def _brief(self, params: Params) -> str:
        symbol = get_symbol_at_position(
            _resolve_path(params.file_path, self._work_dir),
            params.line,
            params.character,
        )
        if symbol:
            return f"{params.operation} {symbol}"
        return f"{params.operation} {params.file_path}:{params.line}:{params.character}"

    async def _validate_file(self, file_path: str) -> tuple[str | None, ToolReturnValue | None]:
        builder = ToolResultBuilder(max_chars=MAX_RESULT_SIZE_CHARS, max_line_length=None)

        if _is_unc_path(file_path):
            return None, builder.error(
                "UNC paths are not supported for LSP operations.",
                brief="UNC path rejected",
            )

        absolute = _resolve_path(file_path, self._work_dir)

        if _is_unc_path(absolute):
            return None, builder.error(
                "UNC paths are not supported for LSP operations.",
                brief="UNC path rejected",
            )

        host_path = HostPath(absolute)
        try:
            if not await host_path.is_file():
                if await host_path.exists():
                    return None, builder.error(
                        f"Path is not a file: {file_path}",
                        brief="Not a file",
                    )
                return None, builder.error(
                    f"File does not exist: {file_path}",
                    brief="File not found",
                )
        except OSError as exc:
            return None, builder.error(
                f"Cannot access file: {file_path}. {exc}",
                brief="File access error",
            )

        return absolute, None

    async def _ensure_file_open(
        self,
        manager: Any,
        absolute_path: str,
        display_path: str,
    ) -> ToolReturnValue | None:
        builder = ToolResultBuilder(max_chars=MAX_RESULT_SIZE_CHARS, max_line_length=None)
        host_path = HostPath(absolute_path)
        try:
            stat = await host_path.stat()
        except OSError as exc:
            return builder.error(
                f"Cannot access file: {display_path}. {exc}",
                brief="File access error",
            )

        if stat.st_size > MAX_LSP_FILE_SIZE_BYTES:
            size_mb = (stat.st_size + 999_999) // 1_000_000
            builder.write(f"File too large for LSP analysis ({size_mb}MB exceeds 10MB limit)\n")
            builder.mark_untrusted()
            return builder.ok(brief=_brief_for_path(display_path))

        content = await host_path.read_text(encoding="utf-8", errors="replace")
        await manager.open_file(absolute_path, content)
        return None


def _brief_for_path(file_path: str) -> str:
    return f"LSP {file_path}"


def _resolve_path(file_path: str, work_dir: HostPath) -> str:
    raw = HostPath(file_path).expanduser()
    joined = raw if raw.is_absolute() else work_dir.joinpath(str(raw))
    return str(Path(str(joined)).resolve())


def _is_unc_path(path: str) -> bool:
    return path.startswith("\\\\") or path.startswith("//")


def _file_uri(path: str) -> str:
    return Path(path).resolve().as_uri()


def _method_and_params(params: Params, absolute_path: str) -> tuple[str, dict[str, Any]]:
    uri = _file_uri(absolute_path)
    position = {"line": params.line - 1, "character": params.character - 1}
    text_document = {"textDocument": {"uri": uri}, "position": position}

    match params.operation:
        case Operation.GO_TO_DEFINITION:
            return "textDocument/definition", text_document
        case Operation.FIND_REFERENCES:
            return "textDocument/references", {
                **text_document,
                "context": {"includeDeclaration": True},
            }
        case Operation.HOVER:
            return "textDocument/hover", text_document
        case Operation.DOCUMENT_SYMBOL:
            return "textDocument/documentSymbol", {"textDocument": {"uri": uri}}
        case Operation.WORKSPACE_SYMBOL:
            return "workspace/symbol", {"query": ""}
        case Operation.GO_TO_IMPLEMENTATION:
            return "textDocument/implementation", text_document
        case Operation.PREPARE_CALL_HIERARCHY | Operation.INCOMING_CALLS | Operation.OUTGOING_CALLS:
            return "textDocument/prepareCallHierarchy", text_document
        case _:
            raise ValueError(f"Unsupported LSP operation: {params.operation}")


def _to_location(item: dict[str, Any]) -> dict[str, Any]:
    if "targetUri" in item:
        return {
            "uri": item.get("targetUri"),
            "range": item.get("targetSelectionRange") or item.get("targetRange") or {},
        }
    return item


def _uri_to_file_path(uri: str) -> str:
    file_path = uri.removeprefix("file://")
    if len(file_path) >= 3 and file_path[0] == "/" and file_path[2] == ":":
        file_path = file_path[1:]
    with contextlib.suppress(Exception):
        file_path = unquote(file_path)
    return file_path


async def _filter_gitignored_results(
    operation: Operation,
    result: Any,
    cwd: str,
) -> Any:
    if not result or not isinstance(result, list):
        return result

    if operation not in (
        Operation.FIND_REFERENCES,
        Operation.GO_TO_DEFINITION,
        Operation.GO_TO_IMPLEMENTATION,
        Operation.WORKSPACE_SYMBOL,
    ):
        return result

    if operation == Operation.WORKSPACE_SYMBOL:
        locations = [
            sym.get("location")
            for sym in result
            if isinstance(sym, dict) and (sym.get("location") or {}).get("uri")
        ]
        filtered = await _filter_gitignored_locations(locations, cwd)
        filtered_uris = {loc.get("uri") for loc in filtered if loc.get("uri")}
        return [
            sym
            for sym in result
            if not (sym.get("location") or {}).get("uri") or sym["location"]["uri"] in filtered_uris
        ]

    locations = [_to_location(item) for item in result if isinstance(item, dict)]
    filtered = await _filter_gitignored_locations(locations, cwd)
    filtered_uris = {loc.get("uri") for loc in filtered if loc.get("uri")}
    return [
        item
        for item in result
        if isinstance(item, dict) and _to_location(item).get("uri") in filtered_uris
    ]


async def _filter_gitignored_locations(
    locations: Sequence[dict[str, Any] | None],
    cwd: str,
) -> list[dict[str, Any]]:
    valid_locations = [loc for loc in locations if loc and loc.get("uri")]
    if not valid_locations:
        return []

    uri_to_path: dict[str, str] = {}
    for loc in valid_locations:
        uri = loc["uri"]
        if uri not in uri_to_path:
            uri_to_path[uri] = _uri_to_file_path(uri)

    unique_paths = list(dict.fromkeys(uri_to_path.values()))
    if not unique_paths:
        return valid_locations

    ignored_paths: set[str] = set()
    for index in range(0, len(unique_paths), _GIT_CHECK_IGNORE_BATCH_SIZE):
        batch = unique_paths[index : index + _GIT_CHECK_IGNORE_BATCH_SIZE]
        ok, stdout = await _run_git_check_ignore(cwd, batch)
        if not ok:
            logger.warning("git check-ignore failed; dropping locations for safety")
            return []
        if stdout:
            ignored_paths.update(line.strip() for line in stdout.splitlines() if line.strip())

    if not ignored_paths:
        return valid_locations

    return [loc for loc in valid_locations if uri_to_path.get(loc["uri"], "") not in ignored_paths]


async def _run_git_check_ignore(cwd: str, paths: list[str]) -> tuple[bool, str]:
    # Fail closed when ignore status cannot be determined so gitignored paths
    # are not leaked when git is unavailable or check-ignore errors out.
    proc = None
    try:
        proc = await pythinker_host.exec("git", "-C", cwd, "check-ignore", *paths)
        proc.stdin.close()
        stdout_bytes = await asyncio.wait_for(
            proc.stdout.read(-1),
            timeout=_GIT_CHECK_IGNORE_TIMEOUT,
        )
        exit_code = await asyncio.wait_for(proc.wait(), timeout=_GIT_CHECK_IGNORE_TIMEOUT)
        if exit_code == 0:
            return True, stdout_bytes.decode("utf-8", errors="replace")
        if exit_code == 1:
            return True, ""
        # Outside a git work tree there is no ignore metadata to apply.
        if exit_code == 128:
            return True, ""
        logger.debug(
            "git check-ignore failed in {cwd} with exit code {code}",
            cwd=cwd,
            code=exit_code,
        )
        return False, ""
    except TimeoutError:
        logger.debug("git check-ignore timed out in {cwd}", cwd=cwd)
        if proc is not None:
            await proc.kill()
            await proc.wait()
        return False, ""
    except Exception as exc:
        logger.debug("git check-ignore errored in {cwd}: {err}", cwd=cwd, err=exc)
        if proc is not None and proc.returncode is None:
            await proc.kill()
            await proc.wait()
        return False, ""
