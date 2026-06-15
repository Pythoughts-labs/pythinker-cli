"""mcpext-3: inject --rm into docker/podman stdio MCP launch commands.

A stdio MCP server configured as `docker run ...` leaves a stopped container behind
on teardown unless --rm is present; killing the client process does not remove the
daemon-managed container. ensure_docker_rm adds --rm so the container is cleaned up.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from pythinker_code.cli.mcp import apply_docker_rm_to_mcp_config_dict, ensure_docker_rm


@pytest.mark.parametrize("cmd", ["docker", "podman"])
def test_injects_rm_after_run(cmd: str) -> None:
    args = ensure_docker_rm(cmd, ["run", "-i", "ghcr.io/example/mcp"])
    assert args == ["run", "--rm", "-i", "ghcr.io/example/mcp"]


def test_keeps_existing_rm() -> None:
    original = ["run", "--rm", "-i", "img"]
    assert ensure_docker_rm("docker", original) == original


def test_ignores_non_run_docker_subcommand() -> None:
    args = ["ps", "-a"]
    assert ensure_docker_rm("docker", args) == args


def test_ignores_non_container_runtime() -> None:
    args = ["mcp-server", "--port", "3000"]
    assert ensure_docker_rm("npx", args) == args


def test_handles_empty_args() -> None:
    assert ensure_docker_rm("docker", []) == []


def test_apply_docker_rm_to_mcp_config_dict() -> None:
    config = apply_docker_rm_to_mcp_config_dict(
        {
            "mcpServers": {
                "ctx": {
                    "command": "docker",
                    "args": ["run", "-i", "ghcr.io/example/mcp"],
                }
            }
        }
    )
    assert config["mcpServers"]["ctx"]["args"] == ["run", "--rm", "-i", "ghcr.io/example/mcp"]


def test_full_path_runtime_is_recognized() -> None:
    # A docker binary referenced by path should still be treated as docker.
    args = ensure_docker_rm("/usr/bin/docker", ["run", "img"])
    assert args == ["run", "--rm", "img"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions not available on Windows")
def test_save_mcp_config_is_owner_only(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_save_mcp_config must write mcp.json with permissions 0600 (owner-read/write only)."""
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    from pythinker_code.cli.mcp import _save_mcp_config, get_global_mcp_config_file

    _save_mcp_config({"mcpServers": {}})
    mcp_file = get_global_mcp_config_file()
    assert mcp_file.exists(), "mcp.json was not created"
    file_mode = stat.S_IMODE(os.stat(mcp_file).st_mode)
    assert file_mode == 0o600, f"Expected 0o600, got {oct(file_mode)}"

    # A pre-existing 0644 file (e.g. from an older version) must be tightened to 0600
    # on the next save — and the secret content is only written after the tighten.
    os.chmod(mcp_file, 0o644)
    _save_mcp_config({"mcpServers": {"x": {"command": "echo", "args": ["secret"]}}})
    assert stat.S_IMODE(os.stat(mcp_file).st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions not available on Windows")
def test_get_share_dir_hardens_preexisting_loose_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_share_dir must re-tighten a pre-existing world-traversable (0755) dir to 0700."""
    from pythinker_code.share import get_share_dir

    share = tmp_path / "share"
    share.mkdir()
    os.chmod(share, 0o755)  # simulate an older version's loose perms
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(share))

    result = get_share_dir()
    assert stat.S_IMODE(os.stat(result).st_mode) == 0o700
