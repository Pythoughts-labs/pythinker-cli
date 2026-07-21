"""Session-owned, non-blocking Git status snapshots for the prompt footer."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Literal

from pythinker_host import AsyncReadable, Host, HostProcess
from pythinker_host.path import HostPath

from pythinker_code.ui.shell.prompting.lifecycle import PromptLifecycle
from pythinker_code.utils.logging import logger

_GIT_TIMEOUT_SECONDS = 5.0
_MAX_GIT_OUTPUT_BYTES = 2 * 1024 * 1024
_REFRESH_INTERVAL_SECONDS = 5.0
_AHEAD_BEHIND_RE = re.compile(r"\[(?:ahead (\d+))?(?:, )?(?:behind (\d+))?\]")
_INSERTIONS_RE = re.compile(r"(\d+) insertion")
_DELETIONS_RE = re.compile(r"(\d+) deletion")

type GitDegradedReason = Literal[
    "detached-head",
    "not-a-repository",
    "command-failed",
    "timeout",
    "output-limit",
]


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    """Immutable Git information belonging to exactly one requested root."""

    root: HostPath
    repository_root: HostPath | None = None
    branch: str | None = None
    dirty: bool = False
    ahead: int = 0
    behind: int = 0
    added: int = 0
    removed: int = 0
    refreshed_at: float = 0.0
    degraded: bool = False
    degraded_reason: GitDegradedReason | None = None

    @property
    def diffstat(self) -> tuple[int, int] | None:
        if self.added == 0 and self.removed == 0:
            return None
        return self.added, self.removed


@dataclass(frozen=True, slots=True)
class _BranchCache:
    branch: str | None


@dataclass(frozen=True, slots=True)
class _StatusCache:
    dirty: bool
    ahead: int
    behind: int


@dataclass(frozen=True, slots=True)
class _DiffCache:
    added: int
    removed: int


@dataclass(frozen=True, slots=True)
class _CommandFailure:
    reason: GitDegradedReason


class GitStatusIndex:
    """Own asynchronous Git refreshes and root-isolated immutable caches."""

    def __init__(
        self,
        host: Host,
        lifecycle: PromptLifecycle,
        *,
        refresh_interval: float = _REFRESH_INTERVAL_SECONDS,
        on_publish: Callable[[], None] | None = None,
    ) -> None:
        self._host = host
        self._lifecycle = lifecycle
        self._refresh_interval = max(0.0, refresh_interval)
        self._on_publish = on_publish
        self._active_root_key: str | None = None
        self._generation = 0
        self._aliases: dict[str, str] = {}
        self._repository_roots: dict[str, HostPath | None] = {}
        self._branches: dict[str, _BranchCache] = {}
        self._statuses: dict[str, _StatusCache] = {}
        self._diffs: dict[str, _DiffCache] = {}
        self._refreshed_at: dict[str, float] = {}
        self._degraded: dict[str, GitDegradedReason | None] = {}
        self._refresh_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    def snapshot(self, root: HostPath) -> GitSnapshot:
        """Read cached data for ``root`` synchronously, without filesystem I/O."""
        canonical_root = root.canonical()
        root_key = str(canonical_root)
        cache_key = self._aliases.get(root_key, root_key)
        branch = self._branches.get(cache_key)
        status = self._statuses.get(cache_key)
        diff = self._diffs.get(cache_key)
        reason = self._degraded.get(root_key)
        return GitSnapshot(
            root=canonical_root,
            repository_root=self._repository_roots.get(root_key),
            branch=branch.branch if branch is not None else None,
            dirty=status.dirty if status is not None else False,
            ahead=status.ahead if status is not None else 0,
            behind=status.behind if status is not None else 0,
            added=diff.added if diff is not None else 0,
            removed=diff.removed if diff is not None else 0,
            refreshed_at=self._refreshed_at.get(root_key, 0.0),
            degraded=reason is not None,
            degraded_reason=reason,
        )

    def request_refresh(self, root: HostPath) -> None:
        """Schedule a bounded refresh for ``root`` if its snapshot is stale."""
        if self._closed:
            return
        canonical_root = root.canonical()
        root_key = str(canonical_root)
        if root_key != self._active_root_key:
            self._generation += 1
            self._active_root_key = root_key
            if self._refresh_task is not None and not self._refresh_task.done():
                self._refresh_task.cancel()
            self._refresh_task = None

        refreshed_at = self._refreshed_at.get(root_key, 0.0)
        if time.monotonic() - refreshed_at <= self._refresh_interval:
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return

        refresh = self._refresh(canonical_root, root_key, self._generation)
        try:
            task = self._lifecycle.create_task(refresh)
        except RuntimeError as exc:
            logger.debug("Git refresh was not scheduled: error={!r}", exc)
            return
        self._refresh_task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        """Cancel and await all Git work owned by this index."""
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
                    logger.warning("Git refresh failed during shutdown: error={!r}", result)
        self._refresh_task = None

    async def _refresh(self, root: HostPath, root_key: str, generation: int) -> None:
        repository = await self._run_git(root, "rev-parse", "--show-toplevel")
        if isinstance(repository, _CommandFailure):
            self._publish_degraded(root, root_key, generation, repository.reason)
            return

        repository_root = HostPath(repository.strip()).canonical()
        repository_key = str(repository_root)
        branch = await self._run_git(repository_root, "symbolic-ref", "--quiet", "--short", "HEAD")
        if isinstance(branch, _CommandFailure):
            self._publish_degraded(root, root_key, generation, branch.reason)
            return
        if not branch.strip():
            self._publish_degraded(root, root_key, generation, "detached-head")
            return

        status_output = await self._run_git(
            repository_root,
            "-c",
            "core.quotepath=false",
            "status",
            "--porcelain",
            "-b",
        )
        if isinstance(status_output, _CommandFailure):
            self._publish_degraded(root, root_key, generation, status_output.reason)
            return
        diff_output = await self._run_git(
            repository_root,
            "--no-optional-locks",
            "diff",
            "--shortstat",
        )
        if isinstance(diff_output, _CommandFailure):
            self._publish_degraded(root, root_key, generation, diff_output.reason)
            return

        dirty, ahead, behind = self._parse_status(status_output)
        added, removed = self._parse_diffstat(diff_output)
        if not self._can_publish(root_key, generation):
            return
        self._aliases[root_key] = repository_key
        self._repository_roots[root_key] = repository_root
        self._branches[repository_key] = _BranchCache(branch=branch.strip())
        self._statuses[repository_key] = _StatusCache(
            dirty=dirty,
            ahead=ahead,
            behind=behind,
        )
        self._diffs[repository_key] = _DiffCache(added=added, removed=removed)
        self._refreshed_at[root_key] = time.monotonic()
        self._degraded[root_key] = None
        self._notify_publish()

    def _publish_degraded(
        self,
        root: HostPath,
        root_key: str,
        generation: int,
        reason: GitDegradedReason,
    ) -> None:
        if not self._can_publish(root_key, generation):
            return
        self._aliases[root_key] = root_key
        self._repository_roots[root_key] = None
        self._branches[root_key] = _BranchCache(branch=None)
        self._statuses[root_key] = _StatusCache(dirty=False, ahead=0, behind=0)
        self._diffs[root_key] = _DiffCache(added=0, removed=0)
        self._refreshed_at[root_key] = time.monotonic()
        self._degraded[root_key] = reason
        logger.debug("Git status is degraded: root={} reason={}", root, reason)
        self._notify_publish()

    def _can_publish(self, root_key: str, generation: int) -> bool:
        return (
            not self._closed
            and generation == self._generation
            and root_key == self._active_root_key
        )

    def _notify_publish(self) -> None:
        if self._on_publish is None:
            return
        try:
            self._on_publish()
        except Exception as exc:
            logger.warning("Git publish notification failed: error={!r}", exc)

    @staticmethod
    def _parse_status(output: str) -> tuple[bool, int, int]:
        dirty = False
        ahead = 0
        behind = 0
        for line in output.splitlines():
            if line.startswith("## "):
                match = _AHEAD_BEHIND_RE.search(line)
                if match is not None:
                    ahead = int(match.group(1) or 0)
                    behind = int(match.group(2) or 0)
            elif line.strip():
                dirty = True
        return dirty, ahead, behind

    @staticmethod
    def _parse_diffstat(output: str) -> tuple[int, int]:
        insertions = _INSERTIONS_RE.search(output)
        deletions = _DELETIONS_RE.search(output)
        return (
            int(insertions.group(1)) if insertions is not None else 0,
            int(deletions.group(1)) if deletions is not None else 0,
        )

    async def _run_git(self, root: HostPath, *args: str) -> str | _CommandFailure:
        process: HostProcess | None = None
        try:
            async with asyncio.timeout(_GIT_TIMEOUT_SECONDS):
                process = await self._host.exec("git", *args, cwd=str(root))
                stdout = await self._read_streams(process)
                returncode = await process.wait()
        except asyncio.CancelledError:
            if process is not None:
                await self._stop_process(process)
            raise
        except TimeoutError:
            if process is not None:
                await self._stop_process(process)
            logger.warning("Git command timed out: root={} command={!r}", root, args)
            return _CommandFailure("timeout")
        except _OutputLimitError:
            if process is not None:
                await self._stop_process(process)
            logger.warning("Git command exceeded output limit: root={} command={!r}", root, args)
            return _CommandFailure("output-limit")
        except Exception as exc:
            if process is not None:
                await self._stop_process(process)
            logger.warning(
                "Git command could not run: root={} command={!r} error={!r}",
                root,
                args,
                exc,
            )
            return _CommandFailure("command-failed")

        if returncode != 0:
            if args == ("rev-parse", "--show-toplevel"):
                return _CommandFailure("not-a-repository")
            if args == ("symbolic-ref", "--quiet", "--short", "HEAD") and returncode == 1:
                return _CommandFailure("detached-head")
            return _CommandFailure("command-failed")
        encoding = "utf-8"
        return stdout.decode(encoding=encoding, errors="replace")

    async def _read_streams(self, process: HostProcess) -> bytes:
        stdout_reader = asyncio.create_task(self._read_bounded(process.stdout, keep=True))
        stderr_reader = asyncio.create_task(self._read_bounded(process.stderr, keep=False))
        try:
            stdout, _stderr = await asyncio.gather(stdout_reader, stderr_reader)
        except BaseException:
            for reader in (stdout_reader, stderr_reader):
                reader.cancel()
            await asyncio.gather(stdout_reader, stderr_reader, return_exceptions=True)
            raise
        return stdout

    @staticmethod
    async def _read_bounded(stream: AsyncReadable, *, keep: bool) -> bytes:
        chunks = bytearray()
        total = 0
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return bytes(chunks)
            total += len(chunk)
            if total > _MAX_GIT_OUTPUT_BYTES:
                raise _OutputLimitError
            if keep:
                chunks.extend(chunk)

    @staticmethod
    async def _stop_process(process: HostProcess) -> None:
        try:
            await process.kill()
        except Exception as exc:
            logger.warning("Git process could not be killed: error={!r}", exc)
        try:
            await process.wait()
        except Exception as exc:
            logger.warning("Git process cleanup failed: error={!r}", exc)


class _OutputLimitError(RuntimeError):
    pass


_active_index: ContextVar[GitStatusIndex | None] = ContextVar(
    "prompt_git_status_index",
    default=None,
)


def bind_git_status_index(index: GitStatusIndex) -> Token[GitStatusIndex | None]:
    """Bind ``index`` to the current prompt-session context."""
    return _active_index.set(index)


def reset_git_status_index(token: Token[GitStatusIndex | None]) -> None:
    """Restore the Git status binding that preceded ``token``."""
    _active_index.reset(token)


def current_git_snapshot(root: HostPath | None = None) -> GitSnapshot:
    """Compatibility facade for synchronous access to the active session index."""
    requested_root = (root or HostPath.cwd()).canonical()
    index = _active_index.get()
    if index is None:
        return GitSnapshot(root=requested_root)
    snapshot = index.snapshot(requested_root)
    index.request_refresh(requested_root)
    return snapshot


__all__ = (
    "GitSnapshot",
    "GitStatusIndex",
    "bind_git_status_index",
    "current_git_snapshot",
    "reset_git_status_index",
)
