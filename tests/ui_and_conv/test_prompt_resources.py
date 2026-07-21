from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, cast

import pytest
from pythinker_host import Host, HostProcess
from pythinker_host.path import HostPath

from pythinker_code.ui.shell.prompting.git_status import GitStatusIndex
from pythinker_code.ui.shell.prompting.lifecycle import PromptLifecycle
from pythinker_code.ui.shell.prompting.toasts import (
    ToastManager,
    bind_toast_manager,
    reset_toast_manager,
    toast,
)


class _FakeProcess:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        encoding = "utf-8"
        self.stdout.feed_data(stdout.encode(encoding=encoding))
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = returncode
        self.pid = 1

    async def wait(self) -> int:
        return self.returncode

    async def kill(self) -> None:
        self.returncode = -9


class _FakeGitHost:
    name = "fake-git"

    def __init__(self, branches: Mapping[str, str]) -> None:
        self.branches = branches

    async def exec(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> HostProcess:
        del env
        assert cwd is not None
        branch = self.branches[cwd]
        command = args[1:]
        if command == ("rev-parse", "--show-toplevel"):
            output = f"{cwd}\n"
        elif command == ("symbolic-ref", "--quiet", "--short", "HEAD"):
            output = f"{branch}\n"
        elif command[-3:] == ("status", "--porcelain", "-b"):
            output = f"## {branch}...origin/{branch} [ahead 1]\n M tracked.py\n"
        elif command == ("--no-optional-locks", "diff", "--shortstat"):
            output = " 1 file changed, 3 insertions(+), 1 deletion(-)\n"
        else:
            raise AssertionError(f"unexpected Git command: {args!r}")
        return cast(HostProcess, _FakeProcess(output))


async def _wait_for_snapshot(index: GitStatusIndex, root: HostPath) -> None:
    for _ in range(100):
        if index.snapshot(root).refreshed_at > 0:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"Git snapshot was not published for {root}")


@pytest.mark.asyncio
async def test_prompt_resources_are_isolated_across_sessions() -> None:
    root_a = HostPath("/workspace/a")
    root_b = HostPath("/workspace/b")
    root_c = HostPath("/workspace/c")
    host = cast(
        Host,
        _FakeGitHost(
            {
                str(root_a): "branch-a",
                str(root_b): "branch-b",
                str(root_c): "branch-c",
            }
        ),
    )
    lifecycle_a = PromptLifecycle()
    lifecycle_b = PromptLifecycle()
    git_a = GitStatusIndex(host, lifecycle_a, refresh_interval=0)
    git_b = GitStatusIndex(host, lifecycle_b, refresh_interval=0)
    lifecycle_a.register_closer("Git A", git_a.aclose)
    lifecycle_b.register_closer("Git B", git_b.aclose)
    toasts_a = ToastManager(adopt_bootstrap=False)
    toasts_b = ToastManager(adopt_bootstrap=False)
    lifecycle_a.register_closer("toasts A", toasts_a.aclose)
    lifecycle_b.register_closer("toasts B", toasts_b.aclose)

    git_a.request_refresh(root_a)
    git_b.request_refresh(root_b)
    await _wait_for_snapshot(git_a, root_a)
    await _wait_for_snapshot(git_b, root_b)

    assert git_a.snapshot(root_a).branch == "branch-a"
    assert git_a.snapshot(root_b).branch is None
    assert git_b.snapshot(root_b).branch == "branch-b"
    assert git_b.snapshot(root_a).branch is None

    token_a = bind_toast_manager(toasts_a)
    toast("session A only", topic="session")
    reset_toast_manager(token_a)
    token_b = bind_toast_manager(toasts_b)
    try:
        assert toasts_b.current() is None
        toast("session B only", topic="session")
    finally:
        reset_toast_manager(token_b)
    toast_a = toasts_a.current()
    toast_b = toasts_b.current()
    assert toast_a is not None
    assert toast_a.message == "session A only"
    assert toast_b is not None
    assert toast_b.message == "session B only"

    await lifecycle_a.aclose()
    assert toasts_a.current() is None
    assert toasts_b.current() is not None
    git_b.request_refresh(root_c)
    await _wait_for_snapshot(git_b, root_c)
    assert git_b.snapshot(root_c).branch == "branch-c"

    await lifecycle_b.aclose()


@pytest.mark.asyncio
async def test_stale_git_result_cannot_replace_new_root_snapshot() -> None:
    root_a = HostPath("/workspace/a")
    root_b = HostPath("/workspace/b")
    host = cast(
        Host,
        _FakeGitHost(
            {
                str(root_a): "branch-a",
                str(root_b): "branch-b",
            }
        ),
    )
    lifecycle = PromptLifecycle()
    index = GitStatusIndex(host, lifecycle, refresh_interval=0)
    lifecycle.register_closer("Git", index.aclose)

    index.request_refresh(root_a)
    await _wait_for_snapshot(index, root_a)
    index.request_refresh(root_b)
    await _wait_for_snapshot(index, root_b)

    internal = cast(Any, index)
    internal._publish_degraded(root_a, str(root_a), 1, "command-failed")

    assert index.snapshot(root_a).branch == "branch-a"
    assert index.snapshot(root_a).degraded is False
    assert index.snapshot(root_b).branch == "branch-b"
    assert index.snapshot(root_b).degraded is False
    await lifecycle.aclose()
