from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from pythinker_core.message import Message

import pythinker_code.soul.context as context_module
from pythinker_code.soul.compaction import estimate_text_tokens
from pythinker_code.soul.context import Context
from pythinker_code.wire.types import TextPart


def _message(text: str) -> Message:
    return Message(role="user", content=[TextPart(text=text)])


def _memory(context: Context) -> tuple[object, ...]:
    return (
        tuple(context.history),
        context.token_count,
        context.token_count_with_pending,
        context.n_checkpoints,
        context.system_prompt,
        context._tail_repaired,
    )


class _AsyncFileProxy:
    def __init__(
        self,
        wrapped: Any,
        *,
        write: Callable[[str], Any] | None = None,
        flush: Callable[[], Any] | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._write = write
        self._flush = flush

    async def write(self, payload: str) -> int:
        if self._write is not None:
            return await self._write(payload)
        return await self._wrapped.write(payload)

    async def flush(self) -> None:
        if self._flush is not None:
            await self._flush()
            return
        await self._wrapped.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def __aiter__(self) -> Any:
        return self._wrapped.__aiter__()


class _AsyncOpenProxy:
    def __init__(self, wrapped: Any, wrap_file: Callable[[Any], _AsyncFileProxy]) -> None:
        self._wrapped = wrapped
        self._wrap_file = wrap_file

    async def __aenter__(self) -> _AsyncFileProxy:
        return self._wrap_file(await self._wrapped.__aenter__())

    async def __aexit__(self, *args: object) -> object:
        return await self._wrapped.__aexit__(*args)


def _patch_open(
    monkeypatch: pytest.MonkeyPatch,
    wrap_file: Callable[[Any], _AsyncFileProxy],
) -> None:
    real_open = cast(Callable[..., Any], context_module.aiofiles.open)

    def injected_open(*args: object, **kwargs: object) -> _AsyncOpenProxy:
        return _AsyncOpenProxy(real_open(*args, **kwargs), wrap_file)

    monkeypatch.setattr(context_module.aiofiles, "open", injected_open)


@pytest.mark.asyncio
async def test_serialization_failure_leaves_exact_old_bytes_and_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    old_bytes = context_path.read_bytes()
    old_memory = _memory(context)

    def fail_serialization(_records: Sequence[object]) -> str:
        raise ValueError("cannot serialize")

    monkeypatch.setattr(context_module, "_serialize_context_records", fail_serialization)

    with pytest.raises(ValueError, match="cannot serialize"):
        await context.append_messages((_message("new"),))

    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory


@pytest.mark.asyncio
async def test_real_open_failure_leaves_memory_unchanged_and_retry_appends_once(
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    reminder = _message("required reminder")
    await context.append_message(_message("existing"))
    old_bytes = context_path.read_bytes()
    old_memory = _memory(context)

    context._file_backend = tmp_path / "missing" / "context.jsonl"
    with pytest.raises(FileNotFoundError):
        await context.append_message(reminder)

    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory

    context._file_backend = context_path
    await context.append_message(reminder)

    assert list(context.history).count(reminder) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["write", "flush"])
async def test_append_boundary_failure_leaves_exact_old_bytes_and_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    old_bytes = context_path.read_bytes()
    old_memory = _memory(context)

    async def fail_flush() -> None:
        raise OSError("flush failed")

    def wrap_file(wrapped: Any) -> _AsyncFileProxy:
        async def fail_write(payload: str) -> int:
            await wrapped.write(payload[: len(payload) // 2])
            raise OSError("write failed")

        return _AsyncFileProxy(
            wrapped,
            write=fail_write if failure_point == "write" else None,
            flush=fail_flush if failure_point == "flush" else None,
        )

    _patch_open(monkeypatch, wrap_file)

    with pytest.raises(OSError, match=f"{failure_point} failed"):
        await context.append_messages((_message("new one"), _message("new two")))

    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory


@pytest.mark.asyncio
async def test_checkpoint_failure_keeps_id_and_optional_marker_uncommitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    old_bytes = context_path.read_bytes()
    old_memory = _memory(context)

    async def fail_write(_payload: str) -> int:
        raise OSError("checkpoint write failed")

    _patch_open(monkeypatch, lambda wrapped: _AsyncFileProxy(wrapped, write=fail_write))

    with pytest.raises(OSError, match="checkpoint write failed"):
        await context.checkpoint(add_user_message=True)

    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory

    monkeypatch.undo()
    await context.checkpoint(add_user_message=True)

    assert context.n_checkpoints == 1
    assert context.history[-1].extract_text("") == "<system>CHECKPOINT 0</system>"
    assert context_path.read_text(encoding="utf-8").count('"role": "_checkpoint", "id": 0') == 1


@pytest.mark.asyncio
async def test_usage_failure_keeps_authoritative_and_pending_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    pending = _message("pending content")
    await context.append_message(pending)
    old_bytes = context_path.read_bytes()
    old_memory = _memory(context)

    async def fail_flush() -> None:
        raise OSError("usage flush failed")

    _patch_open(monkeypatch, lambda wrapped: _AsyncFileProxy(wrapped, flush=fail_flush))

    with pytest.raises(OSError, match="usage flush failed"):
        await context.update_token_count(400)

    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory
    assert context.token_count_with_pending == estimate_text_tokens((pending,))


@pytest.mark.asyncio
async def test_concurrent_appends_commit_in_lock_arrival_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    first_write_entered = asyncio.Event()
    release_first_write = asyncio.Event()
    first_write = True

    def wrap_file(wrapped: Any) -> _AsyncFileProxy:
        nonlocal first_write

        async def write(payload: str) -> int:
            nonlocal first_write
            if first_write:
                first_write = False
                first_write_entered.set()
                await release_first_write.wait()
            return await wrapped.write(payload)

        return _AsyncFileProxy(wrapped, write=write)

    _patch_open(monkeypatch, wrap_file)
    first = asyncio.create_task(context.append_message(_message("first")))
    await first_write_entered.wait()
    second_started = asyncio.Event()

    async def append_second() -> None:
        second_started.set()
        await context.append_message(_message("second"))

    second = asyncio.create_task(append_second())
    await second_started.wait()
    release_first_write.set()
    await asyncio.gather(first, second)

    assert [message.extract_text("") for message in context.history] == ["first", "second"]
    restored = Context(context_path)
    assert await restored.restore()
    assert list(restored.history) == list(context.history)


@pytest.mark.asyncio
async def test_cancelled_queued_writer_does_not_poison_lock_or_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    first_write_entered = asyncio.Event()
    release_first_write = asyncio.Event()
    first_write = True

    def wrap_file(wrapped: Any) -> _AsyncFileProxy:
        nonlocal first_write

        async def write(payload: str) -> int:
            nonlocal first_write
            if first_write:
                first_write = False
                first_write_entered.set()
                await release_first_write.wait()
            return await wrapped.write(payload)

        return _AsyncFileProxy(wrapped, write=write)

    _patch_open(monkeypatch, wrap_file)
    first = asyncio.create_task(context.append_message(_message("first")))
    await first_write_entered.wait()
    queued_started = asyncio.Event()

    async def append_queued() -> None:
        queued_started.set()
        await context.append_message(_message("cancelled"))

    queued = asyncio.create_task(append_queued())
    await queued_started.wait()
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    release_first_write.set()
    await first
    await context.append_message(_message("after cancellation"))

    assert [message.extract_text("") for message in context.history] == [
        "first",
        "after cancellation",
    ]


@pytest.mark.asyncio
async def test_system_prompt_open_failure_leaves_memory_unchanged(tmp_path: Path) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    context._file_backend = tmp_path / "missing" / "context.jsonl"

    with pytest.raises(FileNotFoundError):
        await context.write_system_prompt("prompt")

    assert context.system_prompt is None
    assert not context_path.exists()
