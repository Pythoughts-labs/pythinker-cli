from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from pathlib import Path


def collect_benchmark_environment(
    *,
    repo_root: Path,
    task_timeout_seconds: int,
    task_max_steps: int,
    verification_command: str,
) -> dict[str, object]:
    return {
        "git_commit": _git_text(repo_root, "rev-parse", "HEAD"),
        "git_dirty": _git_dirty(repo_root),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "task_timeout_seconds": task_timeout_seconds,
        "task_max_steps": task_max_steps,
        "verification_command_sha256": hashlib.sha256(
            verification_command.encode("utf-8")
        ).hexdigest(),
    }


def _git_text(repo_root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _git_dirty(repo_root: Path) -> bool | None:
    try:
        completed = subprocess.run(
            ["git", "status", "--short"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return bool(completed.stdout.strip())
