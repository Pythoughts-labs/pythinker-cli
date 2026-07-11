from __future__ import annotations

import asyncio
import threading
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
        context._pending_messages,
    )


class _AppendFileProxy:
    def __init__(
        self,
        wrapped: Any,
        *,
        failure_point: str | None = None,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._failure_point = failure_point
        self._entered = entered
        self._release = release

    def __enter__(self) -> _AppendFileProxy:
        self._wrapped.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        if self._failure_point == "close":
            self._wrapped.__exit__(*args)
            raise OSError("close failed")
        if self._failure_point == "block_close":
            assert self._entered is not None
            assert self._release is not None
            self._entered.set()
            self._release.wait()
        return self._wrapped.__exit__(*args)

    def write(self, payload: str) -> int:
        if self._failure_point == "block_write":
            assert self._entered is not None
            assert self._release is not None
            self._entered.set()
            self._release.wait()
        if self._failure_point == "write":
            self._wrapped.write(payload[: len(payload) // 2])
            raise OSError("write failed")
        return self._wrapped.write(payload)

    def flush(self) -> None:
        if self._failure_point == "flush":
            raise OSError("flush failed")
        self._wrapped.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


def _patch_append_open(
    monkeypatch: pytest.MonkeyPatch,
    context_path: Path,
    failure_point: str,
    *,
    entered: threading.Event | None = None,
    release: threading.Event | None = None,
) -> None:
    real_open = cast(Callable[..., Any], Path.open)
    injected = False

    def injected_open(path: Path, *args: object, **kwargs: object) -> Any:
        nonlocal injected
        wrapped = real_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path == context_path and mode == "a" and not injected:
            injected = True
            return _AppendFileProxy(
                wrapped,
                failure_point=failure_point,
                entered=entered,
                release=release,
            )
        return wrapped

    monkeypatch.setattr(Path, "open", injected_open)


class _SyncFileProxy:
    def __init__(self, wrapped: Any, failure_point: str) -> None:
        self._wrapped = wrapped
        self._failure_point = failure_point

    def __enter__(self) -> _SyncFileProxy:
        self._wrapped.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        return self._wrapped.__exit__(*args)

    def write(self, payload: str) -> int:
        if self._failure_point == "write":
            self._wrapped.write(payload[: len(payload) // 2])
            raise OSError("system prompt write failed")
        return self._wrapped.write(payload)

    def flush(self) -> None:
        if self._failure_point == "flush":
            raise OSError("system prompt flush failed")
        self._wrapped.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


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

    _patch_append_open(monkeypatch, context_path, failure_point)

    with pytest.raises(OSError, match=f"{failure_point} failed"):
        await context.append_messages((_message("new one"), _message("new two")))

    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory


@pytest.mark.asyncio
async def test_append_close_failure_rolls_back_exact_old_disk_and_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    committed = _message("committed before close failed")
    old_bytes = context_path.read_bytes()
    old_memory = _memory(context)

    _patch_append_open(monkeypatch, context_path, "close")

    with pytest.raises(OSError, match="close failed"):
        await context.append_message(committed)

    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory

    monkeypatch.undo()
    await context.append_message(committed)

    assert list(context.history).count(committed) == 1


@pytest.mark.asyncio
async def test_append_cancellation_during_close_keeps_new_disk_and_memory_coherent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    committed = _message("committed before close cancellation")
    close_entered = threading.Event()
    release_close = threading.Event()

    _patch_append_open(
        monkeypatch,
        context_path,
        "block_close",
        entered=close_entered,
        release=release_close,
    )
    append = asyncio.create_task(context.append_message(committed))
    assert await asyncio.to_thread(close_entered.wait, 5)

    append.cancel()
    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await append

    assert list(context.history)[-1] == committed
    restored = Context(context_path)
    assert await restored.restore()
    assert list(restored.history) == list(context.history)


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

    _patch_append_open(monkeypatch, context_path, "write")

    with pytest.raises(OSError, match="write failed"):
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

    _patch_append_open(monkeypatch, context_path, "flush")

    with pytest.raises(OSError, match="flush failed"):
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
    first_write_entered = threading.Event()
    release_first_write = threading.Event()

    _patch_append_open(
        monkeypatch,
        context_path,
        "block_write",
        entered=first_write_entered,
        release=release_first_write,
    )
    first = asyncio.create_task(context.append_message(_message("first")))
    assert await asyncio.to_thread(first_write_entered.wait, 5)
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
    first_write_entered = threading.Event()
    release_first_write = threading.Event()

    _patch_append_open(
        monkeypatch,
        context_path,
        "block_write",
        entered=first_write_entered,
        release=release_first_write,
    )
    first = asyncio.create_task(context.append_message(_message("first")))
    assert await asyncio.to_thread(first_write_entered.wait, 5)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["write", "flush"])
async def test_system_prompt_failure_leaves_nonexistent_file_and_memory_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    old_memory = _memory(context)
    real_fdopen = cast(Callable[..., Any], context_module.os.fdopen)

    def failing_fdopen(*args: object, **kwargs: object) -> _SyncFileProxy:
        return _SyncFileProxy(real_fdopen(*args, **kwargs), failure_point)

    monkeypatch.setattr(context_module.os, "fdopen", failing_fdopen)

    with pytest.raises(OSError, match=f"system prompt {failure_point} failed"):
        await context.write_system_prompt("new prompt")

    assert not context_path.exists()
    assert _memory(context) == old_memory

    monkeypatch.undo()
    await context.write_system_prompt("new prompt")

    assert context.system_prompt == "new prompt"
    assert context_path.read_text(encoding="utf-8").count('"role": "_system_prompt"') == 1


@pytest.mark.asyncio
async def test_system_prompt_cancellation_waits_for_commit_and_swaps_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    replace_entered = threading.Event()
    release_replace = threading.Event()
    replace_finished = threading.Event()
    real_replace = Path.replace

    def blocking_replace(source: Path, target: Path) -> Path:
        replace_entered.set()
        release_replace.wait()
        try:
            return real_replace(source, target)
        finally:
            replace_finished.set()

    monkeypatch.setattr(Path, "replace", blocking_replace)
    write_prompt = asyncio.create_task(context.write_system_prompt("committed prompt"))
    assert await asyncio.to_thread(replace_entered.wait, 5)

    write_prompt.cancel()
    release_replace.set()
    with pytest.raises(asyncio.CancelledError):
        await write_prompt
    assert await asyncio.to_thread(replace_finished.wait, 5)

    assert context.system_prompt == "committed prompt"
    restored = Context(context_path)
    assert await restored.restore()
    assert restored.system_prompt == context.system_prompt
