"""Tests for host-backed workspace file-mention indexing."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from stat import S_IFDIR, S_IFREG
from typing import Any, cast

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from pythinker_host import Host, StatResult
from pythinker_host.path import HostPath

from pythinker_code.ui.shell.prompt import LocalFileMentionCompleter
from pythinker_code.ui.shell.prompting.completion.workspace import (
    HostFileMentionCompleter,
    WorkspaceIndex,
)
from tests.ui_and_conv._prompt_lifecycle import RecordingLifecycle as _Lifecycle


class _Stream:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._read = False

    async def read(self, _n: int = -1) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._data


class _Process:
    def __init__(self, stdout: str, returncode: int) -> None:
        encoding = "utf-8"
        self.stdout = _Stream(stdout.encode(encoding=encoding))
        self.stderr = _Stream(b"")
        self.stdin = cast(Any, None)
        self.pid = 1
        self.returncode: int | None = None
        self._exit_code = returncode

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self._exit_code

    async def kill(self) -> None:
        self.returncode = -1


class _FakeHost:
    name = "workspace-test"

    def __init__(self) -> None:
        self.children: dict[str, list[tuple[str, bool]]] = {}
        self.iterdir_calls = 0
        self.stat_calls = 0
        self.exec_calls = 0
        self.git_repository = False
        self.git_listing_fails = False

    def add_directory(self, path: str, children: list[tuple[str, bool]]) -> None:
        self.children[path] = children

    async def _iterdir(self, path: HostPath) -> AsyncGenerator[HostPath]:
        self.iterdir_calls += 1
        for name, _is_directory in self.children.get(str(path), []):
            yield path / name

    def iterdir(self, path: HostPath) -> AsyncGenerator[HostPath]:
        return self._iterdir(path)

    async def stat(self, path: HostPath, *, follow_symlinks: bool = True) -> StatResult:
        del follow_symlinks
        self.stat_calls += 1
        parent_children = self.children.get(str(path.parent), [])
        is_directory = next(
            (directory for name, directory in parent_children if name == path.name), False
        )
        mode = (S_IFDIR if is_directory else S_IFREG) | 0o755
        return StatResult(mode, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0)

    async def exec(self, *args: str, env: object = None, cwd: str | None = None) -> _Process:
        del env, cwd
        self.exec_calls += 1
        if "rev-parse" in args:
            output = "true\n" if self.git_repository else ""
            return _Process(output, 0 if self.git_repository else 1)
        if self.git_listing_fails:
            return _Process("", 1)
        return _Process("", 0)


class _ControlledHost(_FakeHost):
    def __init__(self) -> None:
        super().__init__()
        self.started: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.ignore_cancellation: set[str] = set()
        self.cancelled: set[str] = set()

    async def _iterdir(self, path: HostPath) -> AsyncGenerator[HostPath]:
        root = str(path)
        self.iterdir_calls += 1
        self.started.setdefault(root, asyncio.Event()).set()
        gate = self.release.setdefault(root, asyncio.Event())
        try:
            await gate.wait()
        except asyncio.CancelledError:
            self.cancelled.add(root)
            if root not in self.ignore_cancellation:
                raise
            await gate.wait()
        for name, _is_directory in self.children.get(root, []):
            yield path / name


class _InspectableWorkspaceIndex(WorkspaceIndex):
    # White-box accessor for the index's internal degradation bookkeeping, which
    # has no production consumer to observe. Aggregates across every primed
    # snapshot so the assertion holds regardless of how many keys are populated.
    def degraded(self) -> bool:
        return any(snapshot.degraded for snapshot in self._snapshots.values())


def _index(
    host: _FakeHost, root: HostPath, *, limit: int = 1000
) -> tuple[_InspectableWorkspaceIndex, _Lifecycle]:
    lifecycle = _Lifecycle()
    return (
        _InspectableWorkspaceIndex(
            cast(Host, host), lifecycle, root, refresh_interval=60, limit=limit
        ),
        lifecycle,
    )


def _completion_texts(completer: HostFileMentionCompleter, text: str) -> list[str]:
    document = Document(text=text, cursor_position=len(text))
    event = CompleteEvent(completion_requested=True)
    return [completion.text for completion in completer.get_completions(document, event)]


def test_local_completer_name_is_a_facade_alias() -> None:
    assert LocalFileMentionCompleter is HostFileMentionCompleter


@pytest.mark.asyncio
async def test_late_old_generation_never_publishes() -> None:
    host = _ControlledHost()
    root_a = HostPath("/workspace-a")
    root_b = HostPath("/workspace-b")
    host.add_directory(str(root_a), [("from-a.py", False)])
    host.add_directory(str(root_b), [("from-b.py", False)])
    host.ignore_cancellation.add(str(root_a))
    index, lifecycle = _index(host, root_a)

    index.request_refresh("")
    await host.started.setdefault(str(root_a), asyncio.Event()).wait()
    index.set_root(root_b)
    index.request_refresh("")
    await host.started.setdefault(str(root_b), asyncio.Event()).wait()

    host.release[str(root_b)].set()
    await lifecycle.created[-1]
    host.release[str(root_a)].set()
    await lifecycle.drain()

    assert [entry.relative_path for entry in index.snapshot("")] == ["from-b.py"]
    await lifecycle.aclose()


@pytest.mark.asyncio
async def test_obsolete_refresh_is_cancelled() -> None:
    host = _ControlledHost()
    root = HostPath("/workspace")
    host.add_directory(str(root), [("file.py", False)])
    index, lifecycle = _index(host, root)

    index.request_refresh("")
    await host.started.setdefault(str(root), asyncio.Event()).wait()
    index.request_refresh("source")
    await asyncio.sleep(0)

    assert str(root) in host.cancelled
    await index.aclose()
    await lifecycle.aclose()


@pytest.mark.asyncio
async def test_git_failure_uses_degraded_fallback() -> None:
    host = _FakeHost()
    root = HostPath("/workspace")
    host.git_repository = True
    host.git_listing_fails = True
    host.add_directory(str(root), [("source.py", False)])
    index, lifecycle = _index(host, root)

    index.request_refresh("sou")
    await lifecycle.drain()

    assert [entry.relative_path for entry in index.snapshot("sou")] == ["source.py"]
    assert index.degraded() is True
    await lifecycle.aclose()


@pytest.mark.asyncio
async def test_read_streams_cancels_sibling_when_one_reader_fails() -> None:
    # When one stream reader raises, the sibling reader must be cancelled and
    # awaited rather than left draining a closing pipe.
    host = _FakeHost()
    index, lifecycle = _index(host, HostPath("/workspace"))

    class _Blocking:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False

        async def read(self, _n: int = -1) -> bytes:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return b""

    class _Raising:
        def __init__(self, gate: asyncio.Event) -> None:
            self._gate = gate

        async def read(self, _n: int = -1) -> bytes:
            await self._gate.wait()  # ensure the sibling is mid-read first
            raise RuntimeError("stdout reader boom")

    blocking = _Blocking()

    class _Proc:
        stdout = _Raising(blocking.started)
        stderr = blocking

    with pytest.raises(RuntimeError, match="boom"):
        await index._read_streams(cast(Any, _Proc()))

    assert blocking.cancelled is True
    await lifecycle.aclose()


@pytest.mark.asyncio
async def test_results_are_truncated_at_one_thousand_entries() -> None:
    host = _FakeHost()
    root = HostPath("/workspace")
    host.add_directory(str(root), [(f"file-{index:04}.py", False) for index in range(1100)])
    index, lifecycle = _index(host, root)

    index.request_refresh("")
    await lifecycle.drain()

    assert len(index.snapshot("")) == 1000
    await lifecycle.aclose()


@pytest.mark.asyncio
async def test_bounded_discovery_filters_ignored_paths() -> None:
    host = _FakeHost()
    root = HostPath("/workspace")
    host.add_directory(
        str(root),
        [("node_modules", True), ("src", True), (".DS_Store", False)],
    )
    host.add_directory(str(root / "src"), [("app.py", False), ("__pycache__", True)])
    index, lifecycle = _index(host, root)

    index.request_refresh("source")
    await lifecycle.drain()

    paths = [entry.relative_path for entry in index.snapshot("source")]
    assert "src/app.py" in paths
    assert not any("node_modules" in path or "__pycache__" in path for path in paths)
    assert ".DS_Store" not in paths
    await lifecycle.aclose()


@pytest.mark.asyncio
async def test_quoted_path_with_spaces_uses_snapshot() -> None:
    host = _FakeHost()
    root = HostPath("/workspace")
    host.add_directory(str(root), [("docs", True)])
    host.add_directory(str(root / "docs"), [("design notes.md", False)])
    index, lifecycle = _index(host, root)
    index.request_refresh("docs/design")
    await lifecycle.drain()

    completer = HostFileMentionCompleter(index)

    assert _completion_texts(completer, '@"docs/design') == ['"docs/design notes.md"']
    await lifecycle.aclose()


@pytest.mark.asyncio
async def test_scoped_completion_ranks_basename_prefix_first() -> None:
    # With a directory prefix in the fragment ("src/mai"), ranking must compare
    # against the fragment's basename ("mai"): "main.py" (basename prefix) should
    # sort before "domain.py" (basename only contains "mai").
    host = _FakeHost()
    root = HostPath("/workspace")
    host.add_directory(str(root), [("src", True)])
    host.add_directory(str(root / "src"), [("main.py", False), ("domain.py", False)])
    index, lifecycle = _index(host, root)
    index.request_refresh("src/mai")
    await lifecycle.drain()

    completer = HostFileMentionCompleter(index)

    assert _completion_texts(completer, "@src/mai")[:2] == ["src/main.py", "src/domain.py"]
    await lifecycle.aclose()


@pytest.mark.asyncio
async def test_snapshot_and_completion_reads_perform_no_host_io() -> None:
    host = _FakeHost()
    root = HostPath("/workspace")
    host.add_directory(str(root), [("source.py", False)])
    index, lifecycle = _index(host, root)
    index.request_refresh("")
    await lifecycle.drain()
    completer = HostFileMentionCompleter(index)
    calls_before = (host.iterdir_calls, host.stat_calls, host.exec_calls)

    assert index.snapshot("")
    assert _completion_texts(completer, "@s") == ["source.py"]

    assert (host.iterdir_calls, host.stat_calls, host.exec_calls) == calls_before
    await lifecycle.aclose()


@pytest.mark.asyncio
async def test_on_publish_fires_for_fresh_snapshot_only() -> None:
    host = _ControlledHost()
    root_a = HostPath("/workspace-a")
    root_b = HostPath("/workspace-b")
    host.add_directory(str(root_a), [("from-a.py", False)])
    host.add_directory(str(root_b), [("from-b.py", False)])
    host.ignore_cancellation.add(str(root_a))
    lifecycle = _Lifecycle()
    published: list[int] = []
    index = WorkspaceIndex(
        cast(Host, host),
        lifecycle,
        root_a,
        refresh_interval=60,
        on_publish=lambda: published.append(1),
    )

    index.request_refresh("")
    await host.started.setdefault(str(root_a), asyncio.Event()).wait()
    index.set_root(root_b)
    index.request_refresh("")
    await host.started.setdefault(str(root_b), asyncio.Event()).wait()

    host.release[str(root_b)].set()
    await lifecycle.created[-1]
    host.release[str(root_a)].set()
    await lifecycle.drain()

    assert len(published) == 1
    await lifecycle.aclose()
