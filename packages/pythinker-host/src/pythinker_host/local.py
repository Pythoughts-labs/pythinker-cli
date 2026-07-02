from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from asyncio.subprocess import Process as AsyncioProcess
from collections.abc import AsyncGenerator
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Literal

if os.name == "nt":
    import ntpath as pathmodule
    from pathlib import PureWindowsPath as PurePathClass
else:
    import posixpath as pathmodule
    from pathlib import PurePosixPath as PurePathClass

from collections.abc import Mapping

import aiofiles
import aiofiles.os

from pythinker_host import (
    AsyncReadable,
    AsyncWritable,
    Host,
    HostProcess,
    StatResult,
    StrOrHostPath,
)
from pythinker_host.path import HostPath

if TYPE_CHECKING:

    def type_check(local: LocalHost) -> None:
        _: Host = local


class LocalHost:
    """
    A Host implementation that directly interacts with the local filesystem.
    """

    name: str = "local"

    class Process:
        """Local Host process wrapper around asyncio.subprocess.Process."""

        def __init__(self, process: AsyncioProcess) -> None:
            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise ValueError("Process must be created with stdin/stdout/stderr pipes.")

            self._process = process
            self.stdin: AsyncWritable = process.stdin
            self.stdout: AsyncReadable = process.stdout
            self.stderr: AsyncReadable = process.stderr

        @property
        def pid(self) -> int:
            return self._process.pid

        @property
        def returncode(self) -> int | None:
            return self._process.returncode

        async def wait(self) -> int:
            return await self._process.wait()

        async def kill(self) -> None:
            # If the process has already exited (and been reaped), its pid/pgid
            # may have been recycled by the OS; signaling it could hit an
            # unrelated process group. Skip the group-kill in that case.
            if self._process.returncode is not None:
                return
            if os.name != "nt":
                try:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                    return
                except ProcessLookupError:
                    return
                except Exception:
                    pass
            self._process.kill()

    def pathclass(self) -> type[PurePath]:
        return PurePathClass

    def normpath(self, path: StrOrHostPath) -> HostPath:
        return HostPath(pathmodule.normpath(str(path)))

    def gethome(self) -> HostPath:
        return HostPath.unsafe_from_local_path(Path.home())

    def getcwd(self) -> HostPath:
        return HostPath.unsafe_from_local_path(Path.cwd())

    async def chdir(self, path: StrOrHostPath) -> None:
        local_path = path.unsafe_to_local_path() if isinstance(path, HostPath) else Path(path)
        os.chdir(local_path)

    async def realpath(self, path: StrOrHostPath) -> HostPath:
        """Resolve symlinks and return the real path (follows symlinks)."""
        local = path.unsafe_to_local_path() if isinstance(path, HostPath) else Path(path)
        resolved = await asyncio.to_thread(os.path.realpath, str(local))
        return HostPath.unsafe_from_local_path(Path(resolved))

    async def stat(self, path: StrOrHostPath, *, follow_symlinks: bool = True) -> StatResult:
        local_path = path.unsafe_to_local_path() if isinstance(path, HostPath) else Path(path)
        st = await aiofiles.os.stat(local_path, follow_symlinks=follow_symlinks)
        return StatResult(
            st_mode=st.st_mode,
            st_ino=st.st_ino,
            st_dev=st.st_dev,
            st_nlink=st.st_nlink,
            st_uid=st.st_uid,
            st_gid=st.st_gid,
            st_size=st.st_size,
            st_atime=st.st_atime,
            st_mtime=st.st_mtime,
            st_ctime=st.st_ctime if os.name != "nt" else st.st_birthtime,
        )

    async def iterdir(self, path: StrOrHostPath) -> AsyncGenerator[HostPath]:
        local_path = path.unsafe_to_local_path() if isinstance(path, HostPath) else Path(path)
        for entry in await aiofiles.os.listdir(local_path):
            yield HostPath.unsafe_from_local_path(local_path / entry)

    async def glob(
        self, path: StrOrHostPath, pattern: str, *, case_sensitive: bool = True
    ) -> AsyncGenerator[HostPath]:
        local_path = path.unsafe_to_local_path() if isinstance(path, HostPath) else Path(path)
        entries = await asyncio.to_thread(
            lambda: list(local_path.glob(pattern, case_sensitive=case_sensitive))
        )
        for entry in entries:
            yield HostPath.unsafe_from_local_path(entry)

    async def readbytes(self, path: StrOrHostPath, n: int | None = None) -> bytes:
        local_path = path.unsafe_to_local_path() if isinstance(path, HostPath) else Path(path)
        async with aiofiles.open(local_path, mode="rb") as f:
            return await f.read() if n is None else await f.read(n)

    async def readtext(
        self,
        path: str | HostPath,
        *,
        encoding: str = "utf-8",
        errors: Literal["strict", "ignore", "replace"] = "strict",
    ) -> str:
        local_path = path.unsafe_to_local_path() if isinstance(path, HostPath) else Path(path)
        async with aiofiles.open(local_path, encoding=encoding, errors=errors, newline="") as f:
            return await f.read()

    async def readlines(
        self,
        path: str | HostPath,
        *,
        encoding: str = "utf-8",
        errors: Literal["strict", "ignore", "replace"] = "strict",
    ) -> AsyncGenerator[str]:
        local_path = path.unsafe_to_local_path() if isinstance(path, HostPath) else Path(path)
        async with aiofiles.open(local_path, encoding=encoding, errors=errors, newline="") as f:
            async for line in f:
                yield line

    async def writebytes(self, path: StrOrHostPath, data: bytes) -> int:
        local_path = path.unsafe_to_local_path() if isinstance(path, HostPath) else Path(path)
        async with aiofiles.open(local_path, mode="wb") as f:
            return await f.write(data)

    async def writetext(
        self,
        path: str | HostPath,
        data: str,
        *,
        mode: Literal["w"] | Literal["a"] = "w",
        encoding: str = "utf-8",
        errors: Literal["strict", "ignore", "replace"] = "strict",
    ) -> int:
        local_path = path.unsafe_to_local_path() if isinstance(path, HostPath) else Path(path)
        async with aiofiles.open(
            local_path, mode=mode, encoding=encoding, errors=errors, newline=""
        ) as f:
            return await f.write(data)

    async def mkdir(
        self, path: StrOrHostPath, parents: bool = False, exist_ok: bool = False
    ) -> None:
        local_path = path.unsafe_to_local_path() if isinstance(path, HostPath) else Path(path)
        await asyncio.to_thread(local_path.mkdir, parents=parents, exist_ok=exist_ok)

    async def exec(
        self, *args: str, env: Mapping[str, str] | None = None, cwd: str | None = None
    ) -> HostProcess:
        if not args:
            raise ValueError("At least one argument (the program to execute) is required.")

        process_options: dict[str, Any] = {}
        if os.name == "nt":
            # CREATE_NO_WINDOW detaches the child from the interactive console
            # (it gets its own hidden one): console-API writes, `cls`, or
            # SetConsoleMode calls from the child would otherwise bypass the
            # stdio pipes and corrupt the parent TUI until terminal restart.
            # CREATE_NEW_PROCESS_GROUP keeps kill() semantics unchanged.
            process_options["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
            ) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        else:
            process_options["start_new_session"] = True

        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            **process_options,
        )
        return self.Process(process)


local_host = LocalHost()
"""The default local Host instance."""
