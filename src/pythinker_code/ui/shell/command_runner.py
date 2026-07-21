from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pythinker_host import AsyncReadable, Host, HostProcess, get_current_host

from pythinker_code.utils.environment import Environment
from pythinker_code.utils.logging import logger
from pythinker_code.utils.subprocess_env import get_clean_env

_MAX_STREAM_BYTES = 1024 * 1024
_TRUNCATION_NOTICE = b"\n... output truncated ...\n"


@dataclass(frozen=True, slots=True)
class ShellCommandResult:
    stdout: str
    stderr: str
    returncode: int
    truncated_stdout: bool
    truncated_stderr: bool


class ShellCommandRunner:
    """Execute isolated foreground commands through the detected host shell."""

    def __init__(
        self,
        host: Host | None = None,
        *,
        environment: Environment | None = None,
        output_limit_bytes: int = _MAX_STREAM_BYTES,
    ) -> None:
        if output_limit_bytes < 0:
            raise ValueError("output_limit_bytes must be non-negative")
        self._host = host or get_current_host()
        self._environment = environment
        self._output_limit_bytes = output_limit_bytes

    @staticmethod
    def build_argv(command: str, *, shell_name: str, shell_path: str) -> tuple[str, ...]:
        """Build the explicit argv used for POSIX shells and PowerShell."""
        option = "-command" if shell_name == "Windows PowerShell" else "-c"
        return (shell_path, option, command)

    async def run(self, command: str) -> ShellCommandResult:
        environment = self._environment or await Environment.detect()
        argv = self.build_argv(
            command,
            shell_name=environment.shell_name,
            shell_path=str(environment.shell_path),
        )
        process = await self._host.exec(*argv, env=get_clean_env())
        process.stdin.close()

        stdout_task = asyncio.create_task(
            self._read_stream_limited(process.stdout, self._output_limit_bytes)
        )
        stderr_task = asyncio.create_task(
            self._read_stream_limited(process.stderr, self._output_limit_bytes)
        )
        wait_task = asyncio.create_task(process.wait())
        tasks = (stdout_task, stderr_task, wait_task)
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            await self._terminate_and_reap(process, tasks)
            raise
        except Exception:
            await self._terminate_and_reap(process, tasks)
            raise

        stdout_bytes, truncated_stdout = stdout_task.result()
        stderr_bytes, truncated_stderr = stderr_task.result()
        returncode = wait_task.result()

        encoding = "utf-8"
        return ShellCommandResult(
            stdout=stdout_bytes.decode(encoding=encoding, errors="replace"),
            stderr=stderr_bytes.decode(encoding=encoding, errors="replace"),
            returncode=returncode,
            truncated_stdout=truncated_stdout,
            truncated_stderr=truncated_stderr,
        )

    @staticmethod
    async def _read_stream_limited(stream: AsyncReadable, limit: int) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        stored = 0
        truncated = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            remaining = limit - stored
            if remaining > 0:
                kept = chunk[:remaining]
                chunks.append(kept)
                stored += len(kept)
            if len(chunk) > max(remaining, 0):
                truncated = True
        if truncated:
            chunks.append(_TRUNCATION_NOTICE)
        return b"".join(chunks), truncated

    @staticmethod
    async def _terminate_and_reap(
        process: HostProcess,
        tasks: tuple[
            asyncio.Task[tuple[bytes, bool]],
            asyncio.Task[tuple[bytes, bool]],
            asyncio.Task[int],
        ],
    ) -> None:
        try:
            await process.kill()
        except Exception:
            logger.exception("Failed to terminate cancelled shell command")

        try:
            await process.wait()
        except Exception:
            logger.exception("Failed to reap cancelled shell command")

        for task in tasks:
            if not task.done():
                task.cancel()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, asyncio.CancelledError
            ):
                logger.debug("Shell command cleanup task failed: {error}", error=outcome)
