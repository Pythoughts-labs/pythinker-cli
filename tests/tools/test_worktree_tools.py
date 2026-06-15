from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

from pythinker_core.tooling import ToolResult
from pythinker_host.path import HostPath

from pythinker_code.soul.approval import ApprovalResult
from pythinker_code.soul.toolset import PythinkerToolset
from pythinker_code.tools.worktree import EnterWorktree, EnterWorktreeParams, ExitWorktree
from pythinker_code.wire.types import ToolCall


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")


async def _approve_request(*args: object, **kwargs: object) -> ApprovalResult:
    return ApprovalResult(True)


async def test_enter_worktree_switches_runtime_work_dir(
    runtime, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    runtime.session.work_dir = HostPath.unsafe_from_local_path(repo)
    runtime.builtin_args = replace(
        runtime.builtin_args, PYTHINKER_WORK_DIR=HostPath.unsafe_from_local_path(repo)
    )
    toolset = PythinkerToolset(runtime)
    monkeypatch.setattr(runtime.approval, "request", _approve_request)

    result = await EnterWorktree(runtime, toolset)(
        EnterWorktreeParams(name="phase-c", path=str(tmp_path / "session-worktree"))
    )

    worktree = tmp_path / "session-worktree"
    assert not result.is_error
    assert runtime.work_dir == HostPath.unsafe_from_local_path(worktree)
    assert worktree.is_dir()
    assert "worktree_path:" in result.output
    assert "original_work_dir:" in result.output


async def test_exit_worktree_returns_to_original_work_dir(
    runtime, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    runtime.session.work_dir = HostPath.unsafe_from_local_path(repo)
    runtime.builtin_args = replace(
        runtime.builtin_args, PYTHINKER_WORK_DIR=HostPath.unsafe_from_local_path(repo)
    )
    toolset = PythinkerToolset(runtime)
    enter = EnterWorktree(runtime, toolset)
    exit_tool = ExitWorktree(runtime, toolset)
    monkeypatch.setattr(runtime.approval, "request", _approve_request)

    enter_result = await enter(EnterWorktreeParams(path=str(tmp_path / "session-worktree")))
    exit_result = await exit_tool(exit_tool.params())

    assert not enter_result.is_error
    assert not exit_result.is_error
    assert runtime.work_dir == HostPath.unsafe_from_local_path(repo)
    assert (tmp_path / "session-worktree").is_dir()
    assert "retained: true" in exit_result.output


async def test_enter_worktree_failure_does_not_change_work_dir(
    runtime, tmp_path: Path, monkeypatch
) -> None:
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    runtime.session.work_dir = HostPath.unsafe_from_local_path(non_repo)
    runtime.builtin_args = replace(
        runtime.builtin_args, PYTHINKER_WORK_DIR=HostPath.unsafe_from_local_path(non_repo)
    )
    toolset = PythinkerToolset(runtime)
    monkeypatch.setattr(runtime.approval, "request", _approve_request)

    result = await EnterWorktree(runtime, toolset)(
        EnterWorktreeParams(path=str(tmp_path / "session-worktree"))
    )

    assert result.is_error
    assert runtime.work_dir == HostPath.unsafe_from_local_path(non_repo)
    assert runtime.work_dir_override is None


async def test_enter_worktree_rejected_approval_does_not_create_worktree(
    runtime, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    runtime.session.work_dir = HostPath.unsafe_from_local_path(repo)
    runtime.builtin_args = replace(
        runtime.builtin_args, PYTHINKER_WORK_DIR=HostPath.unsafe_from_local_path(repo)
    )
    toolset = PythinkerToolset(runtime)
    dest = tmp_path / "session-worktree"

    async def reject_request(*args: object, **kwargs: object) -> ApprovalResult:
        return ApprovalResult(False, feedback="not now")

    monkeypatch.setattr(runtime.approval, "request", reject_request)

    result = await EnterWorktree(runtime, toolset)(EnterWorktreeParams(path=str(dest)))

    assert result.is_error
    assert "not now" in result.message
    assert runtime.work_dir == HostPath.unsafe_from_local_path(repo)
    assert runtime.work_dir_override is None
    assert not dest.exists()


async def test_exit_worktree_without_active_worktree_is_error(runtime) -> None:
    toolset = PythinkerToolset(runtime)

    result = await ExitWorktree(runtime, toolset)(ExitWorktree.params())

    assert result.is_error
    assert "No session worktree is active" in result.message


async def test_exit_worktree_is_root_only(runtime) -> None:
    sub_runtime = runtime.copy_for_subagent(agent_id="child", subagent_type="coder")
    toolset = PythinkerToolset(sub_runtime)

    result = await ExitWorktree(sub_runtime, toolset)(ExitWorktree.params())

    assert result.is_error
    assert "only available in the root session" in result.message


async def test_enter_worktree_respects_permission_profile(runtime, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    runtime.config.agent_execution_profile = "plan_only"
    runtime.session.work_dir = HostPath.unsafe_from_local_path(repo)
    runtime.builtin_args = replace(
        runtime.builtin_args, PYTHINKER_WORK_DIR=HostPath.unsafe_from_local_path(repo)
    )
    toolset = PythinkerToolset(runtime)
    toolset.add(EnterWorktree(runtime, toolset))

    handle_result = toolset.handle(
        ToolCall(
            id="enter-worktree",
            function=ToolCall.FunctionBody(
                name="EnterWorktree",
                arguments=f'{{"path": "{tmp_path / "session-worktree"}"}}',
            ),
        )
    )
    result = handle_result if isinstance(handle_result, ToolResult) else await handle_result

    assert result.return_value.is_error
    assert "permission profile blocks external tool" in result.return_value.message
    assert runtime.work_dir == HostPath.unsafe_from_local_path(repo)
    assert not (tmp_path / "session-worktree").exists()
