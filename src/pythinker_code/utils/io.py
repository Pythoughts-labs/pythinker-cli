from __future__ import annotations

import contextlib
import errno
import json
import os
import tempfile
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any


class FileLockTimeoutError(TimeoutError):
    """An advisory file lock could not be acquired within its deadline."""


@contextlib.contextmanager
def file_lock(path: Path, *, timeout: float | None = None) -> Generator[None]:
    """Cross-process exclusive lock for read-modify-write cycles on *path*.

    ``atomic_json_write`` prevents torn files but not lost updates: two processes
    that both load before either saves drop each other's changes. Wrap the whole
    load → mutate → save in this lock to serialize concurrent writers. The lock
    file (``<path>.lock``) is kept on disk — unlinking would split the lock across
    inodes. By default this call blocks indefinitely. Pass ``timeout`` for a
    bounded wait; event-loop callers must run either form via ``asyncio.to_thread``.
    """
    if timeout is not None and timeout < 0:
        raise ValueError("File lock timeout must be non-negative.")

    lock_file = path.with_name(path.name + ".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_file.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fh.fileno()).st_size == 0:
                fh.write(b"\0")
                fh.flush()
            deadline = time.monotonic() + timeout if timeout is not None else None
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise FileLockTimeoutError(
                                "Timed out waiting for an advisory file lock."
                            ) from exc
                        time.sleep(min(0.05, remaining))
                    else:
                        time.sleep(0.05)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            if timeout is None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as exc:
                        if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                            raise
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise FileLockTimeoutError(
                                "Timed out waiting for an advisory file lock."
                            ) from exc
                        time.sleep(min(0.05, remaining))
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def ends_with_newline(path: Path) -> bool:
    """True if *path* is missing/empty or its last byte is a newline.

    A crash mid-append can leave a torn final line with no terminator; a JSONL
    appender that does not repair it glues its next record onto the torn line,
    and readers then skip BOTH records.
    """
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return True
            f.seek(-1, os.SEEK_END)
            return f.read(1) == b"\n"
    except FileNotFoundError:
        return True


def atomic_json_write(data: Any, path: Path) -> None:
    """Write JSON data to a file atomically using tmp-file + os.replace.

    This prevents data corruption if the process crashes mid-write: either the
    old file is kept intact or the new file is fully committed.
    """
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
