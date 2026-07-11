from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import aiofiles
import aiofiles.os
from pydantic import ValidationError
from pythinker_core.message import Message, TextPart

from pythinker_code.soul.compaction import estimate_text_tokens
from pythinker_code.soul.message import system
from pythinker_code.utils.io import ends_with_newline
from pythinker_code.utils.logging import logger
from pythinker_code.utils.path import next_available_rotation

_LOST_RESULT_NOTE = (
    "Tool call result was lost before it could be recorded (the session ended "
    "unexpectedly). Re-run the tool if its output is still needed."
)

_ContextRecord = Message | dict[str, object]


@dataclass(frozen=True, slots=True)
class ContextReplacement:
    system_prompt: str | None
    messages: tuple[Message, ...]
    token_count: int
    create_checkpoint: bool
    checkpoint_user_marker: bool = False


@dataclass(frozen=True, slots=True)
class ContextCommit:
    checkpoint_id: int | None
    rotated_file: Path | None
    message_count: int


class ContextPersistenceError(OSError):
    def __init__(self, operation: str, category: str, path: Path):
        self.operation = operation
        self.category = category
        self.path = path
        super().__init__(f"{operation} failed ({category}) for {path}")


@dataclass(frozen=True, slots=True)
class _ContextState:
    history: tuple[Message, ...]
    token_count: int
    pending_messages: tuple[Message, ...]
    pending_token_estimate: int
    next_checkpoint_id: int
    system_prompt: str | None
    tail_repaired: bool


def _empty_context_state() -> _ContextState:
    return _ContextState((), 0, (), 0, 0, None, False)


def _serialize_context_records(records: Sequence[_ContextRecord]) -> str:
    serialized: list[str] = []
    for record in records:
        if isinstance(record, Message):
            serialized.append(record.model_dump_json(exclude_none=True))
        else:
            serialized.append(json.dumps(record))
    return "".join(f"{record}\n" for record in serialized)


def _persistence_error(category: str, path: Path) -> ContextPersistenceError:
    return ContextPersistenceError("replace_history", category, path)


def _cleanup_replacement_path(path: Path | None, primary_error: BaseException) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as cleanup_error:
        logger.warning(
            "Failed to clean context replacement artifact {path}: {error}",
            path=path,
            error=cleanup_error,
        )
        primary_error.add_note(f"Cleanup also failed for {path}")


def _prepare_replacement_file(file_backend: Path, serialized_records: Sequence[str]) -> Path:
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=file_backend.parent,
            prefix=file_backend.name,
            suffix=".tmp",
        )
    except OSError as error:
        raise _persistence_error("temporary_creation", file_backend) from error

    tmp_path = Path(tmp_name)
    descriptor_owned = True
    try:
        try:
            replacement_file = os.fdopen(fd, "w", encoding="utf-8")
            descriptor_owned = False
        except OSError as error:
            raise _persistence_error("write", file_backend) from error
        with replacement_file:
            for record in serialized_records:
                try:
                    replacement_file.write(record)
                except OSError as error:
                    raise _persistence_error("write", file_backend) from error
            try:
                replacement_file.flush()
            except OSError as error:
                raise _persistence_error("write", file_backend) from error
            try:
                os.fsync(replacement_file.fileno())
            except OSError as error:
                raise _persistence_error("synchronization", file_backend) from error
    except BaseException as error:
        primary_error = error
        if isinstance(error, OSError) and not isinstance(error, ContextPersistenceError):
            primary_error = _persistence_error("write", file_backend)
        if descriptor_owned:
            with contextlib.suppress(OSError):
                os.close(fd)
        _cleanup_replacement_path(tmp_path, primary_error)
        if primary_error is not error:
            raise primary_error from error
        raise
    return tmp_path


def _read_live_bytes(file_backend: Path) -> bytes | None:
    if not file_backend.exists():
        return None
    try:
        return file_backend.read_bytes()
    except OSError as error:
        raise _persistence_error("rotation_archive", file_backend) from error


def _write_rotation_archive(rotated_file: Path, live_bytes: bytes) -> None:
    try:
        with rotated_file.open("wb") as archive_file:
            archive_file.write(live_bytes)
            archive_file.flush()
            os.fsync(archive_file.fileno())
    except OSError as error:
        raise _persistence_error("rotation_archive", rotated_file) from error


def _sync_parent_directory(parent: Path) -> bool:
    if os.name != "posix":
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(parent, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}
        if error.errno in unsupported:
            return False
        raise
    return True


async def _settle_awaitable[T](
    operation: Awaitable[T],
) -> tuple[T, asyncio.CancelledError | None]:
    task = asyncio.ensure_future(operation)
    cancellation: asyncio.CancelledError | None = None
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError as error:
        cancellation = error
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({task})

    try:
        result = task.result()
    except BaseException as operation_error:
        if cancellation is not None:
            raise cancellation from operation_error
        raise
    return result, cancellation


async def _settle_thread[T](
    operation: Callable[[], T],
) -> tuple[T, asyncio.CancelledError | None]:
    return await _settle_awaitable(asyncio.to_thread(operation))


def _rollback_context_append(file_backend: Path, existed: bool, original_size: int) -> None:
    if not existed:
        file_backend.unlink(missing_ok=True)
        return
    with file_backend.open("r+b") as rollback_file:
        rollback_file.truncate(original_size)
        rollback_file.flush()


def _append_context_sync(file_backend: Path, payload: str) -> None:
    existed = file_backend.exists()
    original_size = file_backend.stat().st_size if existed else 0
    try:
        with file_backend.open("a", encoding="utf-8") as context_file:
            context_file.write(payload)
            context_file.flush()
    except BaseException as append_error:
        try:
            _rollback_context_append(file_backend, existed, original_size)
        except BaseException as rollback_error:
            raise BaseExceptionGroup(
                "Context append and rollback both failed",
                (append_error, rollback_error),
            ) from append_error
        raise


def _write_system_prompt_sync(file_backend: Path, prompt_line: str) -> None:
    fd, tmp_name = tempfile.mkstemp(
        dir=file_backend.parent,
        prefix=file_backend.name,
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as prompt_file:
            prompt_file.write(prompt_line)
            if file_backend.exists() and file_backend.stat().st_size > 0:
                with file_backend.open(encoding="utf-8") as source_file:
                    while chunk := source_file.read(64 * 1024):
                        prompt_file.write(chunk)
            prompt_file.flush()
        tmp_path.replace(file_backend)
    except BaseException as write_error:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as cleanup_error:
            raise BaseExceptionGroup(
                "System prompt write and cleanup both failed",
                (write_error, cleanup_error),
            ) from write_error
        raise


async def _settle_sync_commit(operation: Callable[[], None]) -> asyncio.CancelledError | None:
    commit_task = asyncio.create_task(asyncio.to_thread(operation))
    cancellation: asyncio.CancelledError | None = None
    try:
        await asyncio.shield(commit_task)
    except asyncio.CancelledError as error:
        cancellation = error
        while not commit_task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({commit_task})

    try:
        commit_task.result()
    except BaseException as commit_error:
        if cancellation is not None:
            raise cancellation from commit_error
        raise
    return cancellation


def repair_history_invariants(history: Sequence[Message]) -> list[Message]:
    """Restore tool call/result pairing broken by a crash mid-persistence.

    Synthesizes a lost-result message for every assistant tool call without a
    recorded result and drops tool results with no matching open call —
    either anomaly makes every subsequent provider request fail with a
    pairing error. Runtime appends are pair-shielded, so this runs only at
    the restore boundary; it never rewrites the file, so re-repair on each
    restore is idempotent.
    """
    repaired: list[Message] = []
    open_call_ids: list[str] = []

    def _synthesize_lost_results() -> None:
        for call_id in open_call_ids:
            logger.warning(
                "Context repair: synthesizing lost result for tool call {call_id}",
                call_id=call_id,
            )
            repaired.append(
                Message(
                    role="tool",
                    content=[TextPart(text=_LOST_RESULT_NOTE)],
                    tool_call_id=call_id,
                )
            )
        open_call_ids.clear()

    for message in history:
        if message.role == "tool":
            if message.tool_call_id is None:
                # An unpaired tool result cannot satisfy the call/result pairing
                # invariant this repair enforces; keeping it would re-break the
                # next provider request, so drop it like an orphan.
                logger.warning("Context repair: dropping tool result without tool_call_id")
            elif message.tool_call_id in open_call_ids:
                open_call_ids.remove(message.tool_call_id)
                repaired.append(message)
            else:
                logger.warning(
                    "Context repair: dropping orphaned tool result for {call_id}",
                    call_id=message.tool_call_id,
                )
            continue
        _synthesize_lost_results()
        repaired.append(message)
        if message.role == "assistant" and message.tool_calls:
            open_call_ids.extend(call.id for call in message.tool_calls)
    _synthesize_lost_results()
    return repaired


def _reduce_context_records(
    state: _ContextState,
    records: Sequence[Message | dict[str, Any]],
    *,
    file_backend: Path,
    line_numbers: Sequence[int] | None = None,
) -> tuple[_ContextState, tuple[bool, ...]]:
    history = list(state.history)
    pending_messages = list(state.pending_messages)
    pending_token_estimate = state.pending_token_estimate
    pending_segment: list[Message] = []
    token_count = state.token_count
    next_checkpoint_id = state.next_checkpoint_id
    system_prompt = state.system_prompt
    accepted: list[bool] = []

    for index, record in enumerate(records):
        line_no = line_numbers[index] if line_numbers is not None else 0
        if isinstance(record, Message):
            history.append(record)
            pending_messages.append(record)
            pending_segment.append(record)
            accepted.append(True)
            continue

        role = record.get("role")
        if not isinstance(role, str):
            logger.warning(
                "Skipping context line {line_no} in {file}: missing or invalid role",
                line_no=line_no,
                file=file_backend,
            )
            accepted.append(False)
            continue
        if role == "_system_prompt":
            content = record.get("content")
            if not isinstance(content, str):
                logger.warning(
                    "Skipping invalid system prompt line {line_no} in {file}",
                    line_no=line_no,
                    file=file_backend,
                )
                accepted.append(False)
                continue
            system_prompt = content
            accepted.append(True)
            continue
        if role == "_usage":
            usage_token_count = record.get("token_count")
            if not isinstance(usage_token_count, int):
                logger.warning(
                    "Skipping invalid usage line {line_no} in {file}",
                    line_no=line_no,
                    file=file_backend,
                )
                accepted.append(False)
                continue
            token_count = usage_token_count
            pending_messages.clear()
            pending_token_estimate = 0
            pending_segment.clear()
            accepted.append(True)
            continue
        if role == "_checkpoint":
            checkpoint_id = record.get("id")
            if not isinstance(checkpoint_id, int):
                logger.warning(
                    "Skipping invalid checkpoint line {line_no} in {file}",
                    line_no=line_no,
                    file=file_backend,
                )
                accepted.append(False)
                continue
            next_checkpoint_id = checkpoint_id + 1
            accepted.append(True)
            continue
        try:
            message = Message.model_validate(record)
        except ValidationError as exc:
            logger.warning(
                "Skipping invalid context message line {line_no} in {file}: {error}",
                line_no=line_no,
                file=file_backend,
                error=exc,
            )
            accepted.append(False)
            continue
        history.append(message)
        pending_messages.append(message)
        pending_segment.append(message)
        accepted.append(True)

    pending_token_estimate += estimate_text_tokens(pending_segment)
    return (
        _ContextState(
            history=tuple(history),
            token_count=token_count,
            pending_messages=tuple(pending_messages),
            pending_token_estimate=pending_token_estimate,
            next_checkpoint_id=next_checkpoint_id,
            system_prompt=system_prompt,
            tail_repaired=state.tail_repaired,
        ),
        tuple(accepted),
    )


def _repair_context_state(state: _ContextState) -> _ContextState:
    history = tuple(repair_history_invariants(state.history))
    pending_messages = tuple(repair_history_invariants(state.pending_messages))
    return replace(
        state,
        history=history,
        pending_messages=pending_messages,
        pending_token_estimate=estimate_text_tokens(pending_messages),
    )


def _replacement_records(replacement: ContextReplacement) -> tuple[_ContextRecord, ...]:
    if replacement.token_count < 0:
        raise ValueError("token_count must be a non-negative integer")
    if replacement.checkpoint_user_marker and not replacement.create_checkpoint:
        raise ValueError("checkpoint_user_marker requires create_checkpoint")

    records: list[_ContextRecord] = []
    if replacement.system_prompt is not None:
        records.append({"role": "_system_prompt", "content": replacement.system_prompt})
    if replacement.create_checkpoint:
        records.append({"role": "_checkpoint", "id": 0})
        if replacement.checkpoint_user_marker:
            records.append(Message(role="user", content=[system("CHECKPOINT 0")]))
    records.extend(replacement.messages)
    records.append({"role": "_usage", "token_count": replacement.token_count})
    return tuple(records)


def _serialize_replacement_records(
    file_backend: Path,
    records: Sequence[_ContextRecord],
) -> tuple[str, ...]:
    try:
        return tuple(_serialize_context_records((record,)) for record in records)
    except (TypeError, ValueError) as error:
        raise _persistence_error("serialization", file_backend) from error


class Context:
    def __init__(self, file_backend: Path):
        self._file_backend = file_backend
        self._history: list[Message] = []
        self._token_count: int = 0
        self._pending_messages: tuple[Message, ...] = ()
        self._pending_token_estimate: int = 0
        self._next_checkpoint_id: int = 0
        """The ID of the next checkpoint, starting from 0, incremented after each checkpoint."""
        self._system_prompt: str | None = None
        self._tail_repaired: bool = False
        self._mutation_lock = asyncio.Lock()

    def _tail_repair_prefix(self, state: _ContextState) -> str:
        """One-time torn-line terminator for the append paths.

        A crash mid-append can leave an unterminated final line; without the
        repair the next record glues onto it and readers skip both lines.
        """
        if state.tail_repaired:
            return ""
        return "" if ends_with_newline(self._file_backend) else "\n"

    def _state(self) -> _ContextState:
        return _ContextState(
            history=tuple(self._history),
            token_count=self._token_count,
            pending_messages=self._pending_messages,
            pending_token_estimate=self._pending_token_estimate,
            next_checkpoint_id=self._next_checkpoint_id,
            system_prompt=self._system_prompt,
            tail_repaired=self._tail_repaired,
        )

    def _swap_state(self, state: _ContextState) -> None:
        self._history[:] = state.history
        self._token_count = state.token_count
        self._pending_messages = state.pending_messages
        self._pending_token_estimate = state.pending_token_estimate
        self._next_checkpoint_id = state.next_checkpoint_id
        self._system_prompt = state.system_prompt
        self._tail_repaired = state.tail_repaired

    async def restore(self) -> bool:
        async with self._mutation_lock:
            logger.debug(
                "Restoring context from file: {file_backend}", file_backend=self._file_backend
            )
            if self._history:
                logger.error("The context storage is already modified")
                raise RuntimeError("The context storage is already modified")
            if not self._file_backend.exists():
                logger.debug("No context file found, skipping restoration")
                return False
            if self._file_backend.stat().st_size == 0:
                logger.debug("Empty context file, skipping restoration")
                return False

            state = _empty_context_state()
            records: list[dict[str, Any]] = []
            line_numbers: list[int] = []
            async with aiofiles.open(self._file_backend, encoding="utf-8", errors="replace") as f:
                line_no = 0
                async for line in f:
                    line_no += 1
                    if not line.strip():
                        continue
                    line_json = self._parse_context_line(
                        line,
                        file_backend=self._file_backend,
                        line_no=line_no,
                    )
                    if line_json is None:
                        continue
                    records.append(line_json)
                    line_numbers.append(line_no)

            state, _ = _reduce_context_records(
                state,
                records,
                file_backend=self._file_backend,
                line_numbers=line_numbers,
            )
            self._swap_state(_repair_context_state(state))
            return True

    @property
    def history(self) -> Sequence[Message]:
        return self._history

    @property
    def token_count(self) -> int:
        return self._token_count

    @property
    def token_count_with_pending(self) -> int:
        return self._token_count + self._pending_token_estimate

    @property
    def n_checkpoints(self) -> int:
        return self._next_checkpoint_id

    @property
    def system_prompt(self) -> str | None:
        return self._system_prompt

    @property
    def file_backend(self) -> Path:
        return self._file_backend

    async def write_system_prompt(self, prompt: str) -> None:
        """Write the system prompt as the first record of the context file.

        If the file is empty, writes it directly. If the file already has content
        (e.g. a legacy session without system prompt), prepends it atomically via a
        temporary file to avoid corruption on crash and avoid loading the entire file
        into memory.
        """
        prompt_record: dict[str, object] = {"role": "_system_prompt", "content": prompt}
        prompt_line = _serialize_context_records((prompt_record,))

        async with self._mutation_lock:
            state, _ = _reduce_context_records(
                self._state(),
                (prompt_record,),
                file_backend=self._file_backend,
            )
            cancellation = await _settle_sync_commit(
                lambda: _write_system_prompt_sync(self._file_backend, prompt_line)
            )
            self._swap_state(state)
            if cancellation is not None:
                raise cancellation

    async def replace_history(self, replacement: ContextReplacement) -> ContextCommit:
        records = _replacement_records(replacement)
        serialized_records = _serialize_replacement_records(self._file_backend, records)

        async with self._mutation_lock:
            next_state, accepted = _reduce_context_records(
                _empty_context_state(),
                records,
                file_backend=self._file_backend,
            )
            if not all(accepted):
                raise ValueError("replacement contains an invalid context record")
            next_state = replace(next_state, tail_repaired=False)

            temp_path, cancellation = await _settle_thread(
                lambda: _prepare_replacement_file(self._file_backend, serialized_records)
            )
            if cancellation is not None:
                _cleanup_replacement_path(temp_path, cancellation)
                raise cancellation

            try:
                live_bytes, cancellation = await _settle_thread(
                    lambda: _read_live_bytes(self._file_backend)
                )
            except BaseException as error:
                _cleanup_replacement_path(temp_path, error)
                raise
            if cancellation is not None:
                _cleanup_replacement_path(temp_path, cancellation)
                raise cancellation

            rotated_file: Path | None = None
            if live_bytes is not None:
                rotated_file, cancellation = await _settle_awaitable(
                    next_available_rotation(self._file_backend)
                )
                if rotated_file is None:
                    error = _persistence_error("rotation_archive", self._file_backend)
                    _cleanup_replacement_path(temp_path, error)
                    raise error
                if cancellation is not None:
                    _cleanup_replacement_path(rotated_file, cancellation)
                    _cleanup_replacement_path(temp_path, cancellation)
                    raise cancellation
                try:
                    _, cancellation = await _settle_thread(
                        lambda: _write_rotation_archive(rotated_file, live_bytes)
                    )
                except BaseException as error:
                    _cleanup_replacement_path(rotated_file, error)
                    _cleanup_replacement_path(temp_path, error)
                    raise
                if cancellation is not None:
                    _cleanup_replacement_path(rotated_file, cancellation)
                    _cleanup_replacement_path(temp_path, cancellation)
                    raise cancellation

            async def commit_visible_generation() -> None:
                try:
                    await asyncio.to_thread(os.replace, temp_path, self._file_backend)
                except OSError as error:
                    raise _persistence_error("atomic_replacement", self._file_backend) from error
                self._swap_state(next_state)

            try:
                _, cancellation = await _settle_awaitable(commit_visible_generation())
            except BaseException as error:
                _cleanup_replacement_path(temp_path, error)
                raise

            def synchronize_visible_generation() -> bool:
                try:
                    return _sync_parent_directory(self._file_backend.parent)
                except OSError as error:
                    raise _persistence_error(
                        "visible_commit_durability", self._file_backend
                    ) from error

            try:
                _, sync_cancellation = await _settle_thread(synchronize_visible_generation)
            except ContextPersistenceError as error:
                if cancellation is not None:
                    raise cancellation from error
                raise
            if cancellation is None:
                cancellation = sync_cancellation
            if cancellation is not None:
                raise cancellation

            return ContextCommit(
                checkpoint_id=0 if replacement.create_checkpoint else None,
                rotated_file=rotated_file,
                message_count=len(next_state.history),
            )

    async def _append_serialized(
        self,
        payload: str,
        state: _ContextState,
        next_state: _ContextState,
    ) -> None:
        append_payload = self._tail_repair_prefix(state) + payload
        cancellation = await _settle_sync_commit(
            lambda: _append_context_sync(self._file_backend, append_payload)
        )
        self._swap_state(next_state)
        if cancellation is not None:
            raise cancellation

    async def checkpoint(self, add_user_message: bool) -> None:
        async with self._mutation_lock:
            state = self._state()
            checkpoint_id = state.next_checkpoint_id
            logger.debug("Checkpointing, ID: {id}", id=checkpoint_id)
            checkpoint_record: dict[str, object] = {
                "role": "_checkpoint",
                "id": checkpoint_id,
            }
            records: tuple[_ContextRecord, ...]
            if add_user_message:
                records = (
                    checkpoint_record,
                    Message(role="user", content=[system(f"CHECKPOINT {checkpoint_id}")]),
                )
            else:
                records = (checkpoint_record,)
            payload = _serialize_context_records(records)
            next_state, _ = _reduce_context_records(
                state,
                records,
                file_backend=self._file_backend,
            )
            next_state = replace(next_state, tail_repaired=True)
            await self._append_serialized(payload, state, next_state)

    async def revert_to(self, checkpoint_id: int) -> None:
        async with self._mutation_lock:
            await self._revert_to(checkpoint_id)

    async def _revert_to(self, checkpoint_id: int) -> None:
        """
        Revert the context to the specified checkpoint.
        After this, the specified checkpoint and all subsequent content will be
        removed from the context. File backend will be rotated.

        Args:
            checkpoint_id (int): The ID of the checkpoint to revert to. 0 is the first checkpoint.

        Raises:
            ValueError: When the checkpoint does not exist.
            RuntimeError: When no available rotation path is found.
        """

        logger.debug("Reverting checkpoint, ID: {id}", id=checkpoint_id)
        if checkpoint_id >= self._next_checkpoint_id:
            logger.error("Checkpoint {checkpoint_id} does not exist", checkpoint_id=checkpoint_id)
            raise ValueError(f"Checkpoint {checkpoint_id} does not exist")

        # rotate the context file
        rotated_file_path = await next_available_rotation(self._file_backend)
        if rotated_file_path is None:
            logger.error("No available rotation path found")
            raise RuntimeError("No available rotation path found")
        await aiofiles.os.replace(self._file_backend, rotated_file_path)
        logger.debug(
            "Rotated context file: {rotated_file_path}", rotated_file_path=rotated_file_path
        )

        # restore the context until the specified checkpoint
        self._history.clear()
        self._token_count = 0
        self._next_checkpoint_id = 0
        self._system_prompt = None
        messages_after_last_usage: list[Message] = []
        async with (
            aiofiles.open(rotated_file_path, encoding="utf-8", errors="replace") as old_file,
            aiofiles.open(self._file_backend, "w", encoding="utf-8") as new_file,
        ):
            line_no = 0
            async for line in old_file:
                line_no += 1
                if not line.strip():
                    continue

                line_json = self._parse_context_line(
                    line,
                    file_backend=rotated_file_path,
                    line_no=line_no,
                )
                if line_json is None:
                    continue
                if line_json.get("role") == "_checkpoint" and line_json.get("id") == checkpoint_id:
                    break

                keep_line = self._apply_context_record(
                    line_json,
                    history=self._history,
                    messages_after_last_usage=messages_after_last_usage,
                    file_backend=rotated_file_path,
                    line_no=line_no,
                )
                if keep_line:
                    await new_file.write(line)

        self._history[:] = repair_history_invariants(self._history)
        messages_after_last_usage[:] = repair_history_invariants(messages_after_last_usage)
        self._pending_messages = tuple(messages_after_last_usage)
        self._pending_token_estimate = estimate_text_tokens(messages_after_last_usage)

    async def clear(self) -> None:
        async with self._mutation_lock:
            await self._clear()

    async def _clear(self) -> None:
        """
        Clear the context history.
        This is almost equivalent to revert_to(0), but without relying on the assumption
        that the first checkpoint exists.
        File backend will be rotated.

        Raises:
            RuntimeError: When no available rotation path is found.
        """

        logger.debug("Clearing context")

        # rotate the context file
        rotated_file_path = await next_available_rotation(self._file_backend)
        if rotated_file_path is None:
            logger.error("No available rotation path found")
            raise RuntimeError("No available rotation path found")
        await aiofiles.os.replace(self._file_backend, rotated_file_path)
        self._file_backend.touch()
        logger.debug(
            "Rotated context file: {rotated_file_path}", rotated_file_path=rotated_file_path
        )

        self._history.clear()
        self._token_count = 0
        self._pending_messages = ()
        self._pending_token_estimate = 0
        self._next_checkpoint_id = 0
        self._system_prompt = None

    async def append_message(self, message: Message | Sequence[Message]) -> None:
        messages = (message,) if isinstance(message, Message) else message
        await self.append_messages(messages)

    async def append_messages(self, messages: Sequence[Message]) -> None:
        logger.debug("Appending messages to context: {messages}", messages=messages)
        message_batch = tuple(messages)
        payload = _serialize_context_records(message_batch)
        async with self._mutation_lock:
            state = self._state()
            next_state, _ = _reduce_context_records(
                state,
                message_batch,
                file_backend=self._file_backend,
            )
            next_state = replace(next_state, tail_repaired=True)
            await self._append_serialized(payload, state, next_state)

    async def update_token_count(self, token_count: int) -> None:
        logger.debug("Updating token count in context: {token_count}", token_count=token_count)
        usage_record: dict[str, object] = {"role": "_usage", "token_count": token_count}
        payload = _serialize_context_records((usage_record,))
        async with self._mutation_lock:
            state = self._state()
            next_state, _ = _reduce_context_records(
                state,
                (usage_record,),
                file_backend=self._file_backend,
            )
            next_state = replace(next_state, tail_repaired=True)
            await self._append_serialized(payload, state, next_state)

    def _parse_context_line(
        self,
        line: str,
        *,
        file_backend: Path,
        line_no: int,
    ) -> dict[str, Any] | None:
        try:
            line_json = json.loads(line, strict=False)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Skipping malformed context line {line_no} in {file}: {error}",
                line_no=line_no,
                file=file_backend,
                error=exc,
            )
            return None
        if not isinstance(line_json, dict):
            logger.warning(
                "Skipping non-object context line {line_no} in {file}",
                line_no=line_no,
                file=file_backend,
            )
            return None
        return cast(dict[str, Any], line_json)

    def _apply_context_record(
        self,
        line_json: dict[str, Any],
        *,
        history: list[Message],
        messages_after_last_usage: list[Message],
        file_backend: Path,
        line_no: int,
    ) -> bool:
        state = _ContextState(
            history=tuple(history),
            token_count=self._token_count,
            pending_messages=tuple(messages_after_last_usage),
            pending_token_estimate=estimate_text_tokens(messages_after_last_usage),
            next_checkpoint_id=self._next_checkpoint_id,
            system_prompt=self._system_prompt,
            tail_repaired=self._tail_repaired,
        )
        next_state, accepted = _reduce_context_records(
            state,
            (line_json,),
            file_backend=file_backend,
            line_numbers=(line_no,),
        )
        history[:] = next_state.history
        messages_after_last_usage[:] = next_state.pending_messages
        self._token_count = next_state.token_count
        self._next_checkpoint_id = next_state.next_checkpoint_id
        self._system_prompt = next_state.system_prompt
        return accepted[0]
