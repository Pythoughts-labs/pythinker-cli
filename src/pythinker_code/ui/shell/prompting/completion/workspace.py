"""Asynchronous workspace indexing for file-mention completion."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from stat import S_ISDIR
from typing import override

from prompt_toolkit.completion import (
    CompleteEvent,
    Completer,
    Completion,
    FuzzyCompleter,
    WordCompleter,
)
from prompt_toolkit.document import Document
from pythinker_host import AsyncReadable, Host, HostProcess
from pythinker_host.path import HostPath

from pythinker_code.ui.shell.prompting.completion.context import (
    CompletionKind,
    parse_completion_context,
)
from pythinker_code.ui.shell.prompting.lifecycle import PromptLifecycle
from pythinker_code.utils.file_filter import is_ignored
from pythinker_code.utils.logging import logger

_MAX_ENTRIES = 1000
_MAX_DEPTH = 8
_MAX_DIRECTORY_VISITS = 256
_MAX_DIRECTORY_ENTRIES = 2000
_MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    """One immutable path in a workspace completion snapshot."""

    relative_path: str
    is_directory: bool


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """One generation-owned immutable workspace scan result."""

    root: HostPath
    generation: int
    entries: tuple[WorkspaceEntry, ...]
    refreshed_at: float
    degraded: bool


class WorkspaceIndex:
    """Publish non-blocking workspace snapshots discovered through a ``Host``."""

    def __init__(
        self,
        host: Host,
        lifecycle: PromptLifecycle,
        root: HostPath,
        *,
        refresh_interval: float = 2.0,
        limit: int = _MAX_ENTRIES,
        on_publish: Callable[[], None] | None = None,
    ) -> None:
        self._host = host
        self._lifecycle = lifecycle
        self._root = root
        self._on_publish = on_publish
        self._generation = 1
        self._refresh_interval = max(0.0, refresh_interval)
        self._limit = min(_MAX_ENTRIES, max(1, limit))
        self._snapshots: dict[tuple[bool, str | None], WorkspaceSnapshot] = {}
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_key: tuple[bool, str | None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def set_root(self, root: HostPath) -> None:
        """Start a new ownership generation rooted at ``root``."""
        self._generation += 1
        self._root = root
        self._snapshots.clear()
        self._refresh_key = None
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
        self._refresh_task = None

    def snapshot(self, fragment: str) -> tuple[WorkspaceEntry, ...]:
        """Return the current immutable entries for ``fragment`` without I/O."""
        snapshot = self._snapshots.get(self._snapshot_key(fragment))
        if snapshot is None:
            return ()
        return snapshot.entries

    def request_refresh(self, fragment: str) -> None:
        """Schedule replacement discovery when the relevant snapshot is stale."""
        if self._closed:
            return

        key = self._snapshot_key(fragment)
        if (
            self._refresh_task is not None
            and not self._refresh_task.done()
            and self._refresh_key == key
        ):
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()

        current = self._snapshots.get(key)
        now = time.monotonic()
        if current is not None and now - current.refreshed_at <= self._refresh_interval:
            self._refresh_task = None
            self._refresh_key = None
            return

        generation = self._generation
        root = self._root
        refresh = self._refresh(root, generation, key)
        try:
            task = self._lifecycle.create_task(refresh)
        except RuntimeError as exc:
            refresh.close()
            logger.debug("Workspace refresh was not scheduled: error={!r}", exc)
            return
        self._refresh_task = task
        self._refresh_key = key
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        """Cancel and await all workspace discovery owned by this index."""
        if self._closed:
            return
        self._closed = True
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    logger.warning("Workspace refresh failed during shutdown: error={!r}", result)
        self._refresh_task = None
        self._refresh_key = None

    @staticmethod
    def _snapshot_key(fragment: str) -> tuple[bool, str | None]:
        if "/" not in fragment and len(fragment) < 3:
            return (False, None)
        if "/" not in fragment:
            return (True, None)
        scope = fragment.rsplit("/", 1)[0].strip("/")
        if not scope or ".." in scope.split("/"):
            return (True, None)
        return (True, scope)

    async def _refresh(
        self,
        root: HostPath,
        generation: int,
        key: tuple[bool, str | None],
    ) -> None:
        try:
            deep, scope = key
            if deep:
                entries, degraded = await self._discover_deep(root, scope)
            else:
                entries, degraded = await self._discover_top_level(root)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Workspace refresh failed; publishing a degraded snapshot: root={} error={!r}",
                root,
                exc,
            )
            entries = ()
            degraded = True

        if self._closed or generation != self._generation or root != self._root:
            return
        self._snapshots[key] = WorkspaceSnapshot(
            root=root,
            generation=generation,
            entries=entries[: self._limit],
            refreshed_at=time.monotonic(),
            degraded=degraded,
        )
        if self._on_publish is not None:
            try:
                self._on_publish()
            except Exception as exc:
                logger.warning("Workspace publish notification failed: error={!r}", exc)

    async def _discover_top_level(self, root: HostPath) -> tuple[tuple[WorkspaceEntry, ...], bool]:
        children: list[HostPath] = []
        truncated = False
        work = 0
        async for child in self._host.iterdir(root):
            work += 1
            if work > _MAX_DIRECTORY_ENTRIES:
                truncated = True
                break
            if is_ignored(child.name):
                continue
            if len(children) >= self._limit:
                truncated = True
                break
            children.append(child)

        entries: list[WorkspaceEntry] = []
        degraded = truncated
        if truncated:
            logger.warning("Workspace top-level discovery was truncated: root={}", root)
        for child in sorted(children, key=lambda path: path.name):
            try:
                stat_result = await self._host.stat(child)
            except OSError as exc:
                degraded = True
                logger.debug(
                    "Workspace top-level entry could not be inspected: path={} error={!r}",
                    child,
                    exc,
                )
                continue
            entries.append(WorkspaceEntry(child.name, S_ISDIR(stat_result.st_mode)))
        return tuple(entries), degraded

    async def _discover_deep(
        self, root: HostPath, scope: str | None
    ) -> tuple[tuple[WorkspaceEntry, ...], bool]:
        repository = await self._is_git_repository(root)
        if repository:
            git_entries = await self._discover_git(root, scope)
            if git_entries is not None:
                return git_entries[: self._limit], False
            logger.warning(
                "Workspace Git discovery failed; using bounded traversal: root={} scope={!r}",
                root,
                scope,
            )
        else:
            logger.debug(
                "Workspace is not a Git repository; using bounded traversal: root={} scope={!r}",
                root,
                scope,
            )
        return await self._discover_bounded(root, scope), True

    async def _is_git_repository(self, root: HostPath) -> bool:
        result = await self._run_git(root, "rev-parse", "--is-inside-work-tree")
        if result is None:
            return False
        return result.strip() == "true"

    async def _discover_git(
        self, root: HostPath, scope: str | None
    ) -> tuple[WorkspaceEntry, ...] | None:
        scope_args = ("--", f"{scope}/") if scope else ()
        tracked = await self._run_git(
            root,
            "-c",
            "core.quotepath=false",
            "ls-files",
            "-z",
            "--recurse-submodules",
            *scope_args,
        )
        if tracked is None:
            return None
        deleted = await self._run_git(
            root,
            "-c",
            "core.quotepath=false",
            "ls-files",
            "-z",
            "--deleted",
            *scope_args,
        )
        if deleted is None:
            return None
        untracked = await self._run_git(
            root,
            "-c",
            "core.quotepath=false",
            "ls-files",
            "-z",
            "--others",
            "--exclude-standard",
            *scope_args,
        )
        if untracked is None:
            return None

        deleted_paths = {path for path in deleted.split("\0") if path}
        paths = [path for path in tracked.split("\0") if path and path not in deleted_paths]
        tracked_paths = set(paths)
        paths.extend(
            path
            for path in untracked.split("\0")
            if path and path not in tracked_paths and path not in deleted_paths
        )
        return self._entries_from_git_paths(paths)

    def _entries_from_git_paths(self, paths: Iterable[str]) -> tuple[WorkspaceEntry, ...]:
        entries: list[WorkspaceEntry] = []
        seen: set[str] = set()
        for path in paths:
            if path.startswith("/"):
                continue
            normalized = path.strip("/")
            parts = normalized.split("/")
            if (
                not normalized
                or normalized.startswith("../")
                or ".." in parts
                or any(is_ignored(part) for part in parts)
            ):
                continue
            for index in range(1, len(parts)):
                directory = "/".join(parts[:index])
                if directory not in seen:
                    seen.add(directory)
                    entries.append(WorkspaceEntry(directory, True))
                    if len(entries) >= self._limit:
                        return tuple(entries)
            if normalized not in seen:
                seen.add(normalized)
                entries.append(WorkspaceEntry(normalized, False))
                if len(entries) >= self._limit:
                    return tuple(entries)
        return tuple(entries)

    async def _discover_bounded(
        self, root: HostPath, scope: str | None
    ) -> tuple[WorkspaceEntry, ...]:
        if scope and (scope.startswith("/") or ".." in scope.split("/")):
            return ()
        start = root.joinpath(*scope.split("/")) if scope else root
        entries: list[WorkspaceEntry] = []
        if scope:
            entries.append(WorkspaceEntry(scope, True))

        queue: list[tuple[HostPath, str, int]] = [(start, scope or "", 0)]
        directory_visits = 0
        work = 0
        while queue and len(entries) < self._limit and directory_visits < _MAX_DIRECTORY_VISITS:
            directory, relative_directory, depth = queue.pop(0)
            directory_visits += 1
            children: list[HostPath] = []
            try:
                async for child in self._host.iterdir(directory):
                    work += 1
                    if work > _MAX_DIRECTORY_ENTRIES:
                        logger.warning(
                            "Workspace traversal work limit reached: root={} scope={!r}",
                            root,
                            scope,
                        )
                        return tuple(entries[: self._limit])
                    if not is_ignored(child.name):
                        children.append(child)
            except OSError as exc:
                logger.debug(
                    "Workspace directory could not be traversed: path={} error={!r}",
                    directory,
                    exc,
                )
                continue

            for child in sorted(children, key=lambda path: path.name):
                relative_path = (
                    f"{relative_directory}/{child.name}" if relative_directory else child.name
                )
                try:
                    stat_result = await self._host.stat(child)
                except OSError as exc:
                    logger.debug(
                        "Workspace entry could not be inspected: path={} error={!r}", child, exc
                    )
                    continue
                is_directory = S_ISDIR(stat_result.st_mode)
                entries.append(WorkspaceEntry(relative_path, is_directory))
                if len(entries) >= self._limit:
                    break
                if is_directory and depth < _MAX_DEPTH:
                    queue.append((child, relative_path, depth + 1))
        if queue:
            logger.warning(
                "Workspace traversal was truncated: root={} scope={!r} entries={} directories={}",
                root,
                scope,
                len(entries),
                directory_visits,
            )
        return tuple(entries[: self._limit])

    async def _run_git(self, root: HostPath, *args: str) -> str | None:
        process: HostProcess | None = None
        try:
            async with asyncio.timeout(_GIT_TIMEOUT_SECONDS):
                process = await self._host.exec("git", *args, cwd=str(root))
                # Drain stdout and stderr concurrently: reading them
                # sequentially can deadlock if git fills its stderr pipe buffer
                # (e.g. submodule warnings) while we are still draining stdout.
                stdout, _stderr = await asyncio.gather(
                    self._read_bounded(process.stdout),
                    self._read_bounded(process.stderr),
                )
                returncode = await process.wait()
        except asyncio.CancelledError:
            if process is not None:
                await self._stop_process(process)
            raise
        except TimeoutError:
            if process is not None:
                await self._stop_process(process)
            logger.warning("Workspace Git command timed out: root={} command={!r}", root, args)
            return None
        except Exception as exc:
            if process is not None:
                await self._stop_process(process)
            logger.warning(
                "Workspace Git command could not run: root={} command={!r} error={!r}",
                root,
                args,
                exc,
            )
            return None

        if returncode != 0:
            return None
        encoding = "utf-8"
        return stdout.decode(encoding=encoding, errors="replace")

    @staticmethod
    async def _read_bounded(stream: AsyncReadable) -> bytes:
        chunks = bytearray()
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return bytes(chunks)
            chunks.extend(chunk)
            if len(chunks) > _MAX_GIT_OUTPUT_BYTES:
                raise RuntimeError("Git output exceeded workspace indexing limit")

    @staticmethod
    async def _stop_process(process: HostProcess) -> None:
        try:
            await process.kill()
        except Exception as exc:
            logger.warning("Workspace Git process could not be killed: error={!r}", exc)
        try:
            await process.wait()
        except Exception as exc:
            logger.warning("Workspace Git process cleanup failed: error={!r}", exc)


class HostFileMentionCompleter(Completer):
    """Offer fuzzy ``@`` completion using only immutable workspace snapshots."""

    _FRAGMENT_PATTERN = re.compile(r"[^\s@]+")

    def __init__(self, index: WorkspaceIndex) -> None:
        self._index = index

    @staticmethod
    def should_complete(document: Document) -> bool:
        """Return whether ``@`` file completion should be active for the buffer."""
        context = parse_completion_context(document, allow_slash=False)
        return context.kind is CompletionKind.FILE

    @override
    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        context = parse_completion_context(document, allow_slash=False)
        if context.kind is not CompletionKind.FILE:
            return
        fragment = context.token
        entries = self._index.snapshot(fragment)
        self._index.request_refresh(fragment)
        if any(
            entry.relative_path == fragment.rstrip("/") and not entry.is_directory
            for entry in entries
        ):
            return

        paths = [
            f"{entry.relative_path}/" if entry.is_directory else entry.relative_path
            for entry in entries
        ]
        word_completer = WordCompleter(
            paths,
            WORD=False,
            pattern=self._FRAGMENT_PATTERN,
        )
        fuzzy = FuzzyCompleter(word_completer, WORD=False, pattern=r"^[^\s@]*")
        mention_doc = Document(text=fragment, cursor_position=len(fragment))
        candidates = list(fuzzy.get_completions(mention_doc, complete_event))

        frag_lower = fragment.lower()
        # The typed fragment often carries a directory prefix (e.g. "src/mai"),
        # but candidates are ranked by basename, so compare against the
        # fragment's basename or prefix priority never applies to scoped paths.
        frag_basename = frag_lower.rsplit("/", 1)[-1]

        def _rank(completion: Completion) -> tuple[int, int]:
            path = completion.text
            basename = path.rstrip("/").split("/")[-1].lower()
            if basename.startswith(frag_basename):
                category = 0
            elif frag_basename in basename:
                category = 1
            else:
                category = 2
            test_penalty = int(any("test" in part.lower() for part in path.split("/")))
            return (category, test_penalty)

        candidates.sort(key=_rank)
        if not context.quoted:
            yield from candidates
            return
        for candidate in candidates:
            escaped = candidate.text.replace("\\", "\\\\").replace('"', '\\"')
            yield Completion(
                text=f'"{escaped}"',
                start_position=context.start_position,
                display=candidate.display,
                display_meta=candidate.display_meta,
                style=candidate.style,
                selected_style=candidate.selected_style,
            )


LocalFileMentionCompleter = HostFileMentionCompleter

__all__ = (
    "HostFileMentionCompleter",
    "LocalFileMentionCompleter",
    "WorkspaceEntry",
    "WorkspaceIndex",
    "WorkspaceSnapshot",
)
