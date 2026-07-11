from __future__ import annotations

import asyncio
import errno
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


def _replacement(
    *messages: Message,
    system_prompt: str | None = "replacement prompt",
    token_count: int = 37,
    create_checkpoint: bool = True,
    checkpoint_user_marker: bool = True,
) -> Any:
    return context_module.ContextReplacement(
        system_prompt=system_prompt,
        messages=messages,
        token_count=token_count,
        create_checkpoint=create_checkpoint,
        checkpoint_user_marker=checkpoint_user_marker,
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
        if self._failure_point in {"block_write", "block_write_fail"}:
            assert self._entered is not None
            assert self._release is not None
            self._entered.set()
            self._release.wait()
        if self._failure_point == "block_write_fail":
            self._wrapped.write(payload[: len(payload) // 2])
            raise OSError("write failed after cancellation")
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
async def test_cancelled_append_with_worker_failure_remains_cancelled_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    old_bytes = context_path.read_bytes()
    old_memory = _memory(context)
    write_entered = threading.Event()
    release_write = threading.Event()

    _patch_append_open(
        monkeypatch,
        context_path,
        "block_write_fail",
        entered=write_entered,
        release=release_write,
    )
    append = asyncio.create_task(context.append_message(_message("not committed")))
    assert await asyncio.to_thread(write_entered.wait, 5)

    append.cancel()
    release_write.set()
    with pytest.raises(asyncio.CancelledError) as cancellation:
        await append

    assert append.cancelled()
    assert isinstance(cancellation.value.__cause__, OSError)
    assert str(cancellation.value.__cause__) == "write failed after cancellation"
    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory


@pytest.mark.asyncio
async def test_repeated_cancellation_settles_commit_before_final_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    committed = _message("committed despite repeated cancellation")
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
    second_cancellation_sent = asyncio.Event()

    def cancel_again() -> None:
        append.cancel()
        second_cancellation_sent.set()

    asyncio.get_running_loop().call_soon(cancel_again)
    await second_cancellation_sent.wait()
    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await append

    assert append.cancelled()
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
    assert not list(tmp_path.glob(f"{context_path.name}*.tmp"))
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


class _ReplacementFileProxy:
    def __init__(self, wrapped: Any, *, fail_write: int | None = None, fail_flush: bool = False):
        self._wrapped = wrapped
        self._fail_write = fail_write
        self._fail_flush = fail_flush
        self._writes = 0

    def __enter__(self) -> _ReplacementFileProxy:
        self._wrapped.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        return self._wrapped.__exit__(*args)

    def write(self, payload: str) -> int:
        self._writes += 1
        if self._writes == self._fail_write:
            raise OSError(f"record write {self._writes} failed")
        return self._wrapped.write(payload)

    def flush(self) -> None:
        if self._fail_flush:
            raise OSError("replacement flush failed")
        self._wrapped.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


@pytest.mark.asyncio
async def test_replace_history_commits_compatible_records_and_exact_archive(tmp_path: Path) -> None:
    context_path = tmp_path / "context.jsonl"
    old_bytes = b'{"role":"user","content":"old"}\n{"torn":'
    context_path.write_bytes(old_bytes)
    context = Context(context_path)
    context._history[:] = [_message("old")]

    commit = await context.replace_history(_replacement(_message("new")))

    records = [
        context._parse_context_line(line, file_backend=context_path, line_no=index)
        for index, line in enumerate(context_path.read_text(encoding="utf-8").splitlines(), 1)
    ]
    assert [record["role"] for record in records if record is not None] == [
        "_system_prompt",
        "_checkpoint",
        "user",
        "user",
        "_usage",
    ]
    assert commit.checkpoint_id == 0
    assert commit.rotated_file == tmp_path / "context_1.jsonl"
    assert commit.rotated_file is not None
    assert commit.rotated_file.read_bytes() == old_bytes
    assert commit.message_count == 2
    assert context.system_prompt == "replacement prompt"
    assert [message.extract_text("") for message in context.history] == [
        "<system>CHECKPOINT 0</system>",
        "new",
    ]
    assert context.token_count == 37
    assert context.token_count_with_pending == 37
    assert context.n_checkpoints == 1
    assert not list(tmp_path.glob("context.jsonl*.tmp"))

    restored = Context(context_path)
    assert await restored.restore()
    assert _memory(restored) == _memory(context)


@pytest.mark.asyncio
async def test_replace_history_validates_before_filesystem_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context_path.write_bytes(b"old bytes")
    context = Context(context_path)
    old_memory = _memory(context)

    def unexpected_temp(*args: object, **kwargs: object) -> tuple[int, str]:
        pytest.fail("validation touched the filesystem")

    monkeypatch.setattr(context_module.tempfile, "mkstemp", unexpected_temp)
    with pytest.raises(ValueError, match="token_count"):
        await context.replace_history(_replacement(token_count=-1))

    assert context_path.read_bytes() == b"old bytes"
    assert _memory(context) == old_memory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_point", "expected_category"),
    [
        ("temp_create", "temporary_creation"),
        ("write_1", "write"),
        ("write_2", "write"),
        ("write_3", "write"),
        ("write_4", "write"),
        ("write_5", "write"),
        ("flush", "write"),
        ("temp_fsync", "synchronization"),
    ],
)
async def test_replace_history_preparation_failures_preserve_exact_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    expected_category: str,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    old_bytes = context_path.read_bytes()
    old_memory = _memory(context)

    if failure_point == "temp_create":
        monkeypatch.setattr(
            context_module.tempfile,
            "mkstemp",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("temp create failed")),
        )
    else:
        real_fdopen = cast(Callable[..., Any], context_module.os.fdopen)
        write_number = (
            int(failure_point.removeprefix("write_")) if "write_" in failure_point else None
        )

        def failing_fdopen(*args: object, **kwargs: object) -> _ReplacementFileProxy:
            return _ReplacementFileProxy(
                real_fdopen(*args, **kwargs),
                fail_write=write_number,
                fail_flush=failure_point == "flush",
            )

        monkeypatch.setattr(context_module.os, "fdopen", failing_fdopen)
        if failure_point == "temp_fsync":
            monkeypatch.setattr(
                context_module.os,
                "fsync",
                lambda _fd: (_ for _ in ()).throw(OSError("temp fsync failed")),
            )

    with pytest.raises(context_module.ContextPersistenceError) as raised:
        await context.replace_history(_replacement(_message("new")))

    assert raised.value.operation == "replace_history"
    assert raised.value.category == expected_category
    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory
    assert not list(tmp_path.glob("context.jsonl*.tmp"))
    assert not (tmp_path / "context_1.jsonl").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["archive_write", "archive_flush", "archive_fsync"])
async def test_replace_history_archive_failures_preserve_old_bytes_and_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    old_bytes = context_path.read_bytes() + b'{"torn":'
    context_path.write_bytes(old_bytes)
    old_memory = _memory(context)
    real_open = cast(Callable[..., Any], Path.open)

    def failing_open(path: Path, *args: object, **kwargs: object) -> Any:
        wrapped = real_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path.name == "context_1.jsonl" and mode == "wb":
            return _ReplacementFileProxy(
                wrapped,
                fail_write=1 if failure_point == "archive_write" else None,
                fail_flush=failure_point == "archive_flush",
            )
        return wrapped

    monkeypatch.setattr(Path, "open", failing_open)
    if failure_point == "archive_fsync":
        real_fsync = context_module.os.fsync
        calls = 0

        def fail_second_fsync(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("archive fsync failed")
            real_fsync(fd)

        monkeypatch.setattr(context_module.os, "fsync", fail_second_fsync)

    with pytest.raises(context_module.ContextPersistenceError) as raised:
        await context.replace_history(_replacement(_message("new")))

    assert raised.value.category == "rotation_archive"
    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory
    assert not list(tmp_path.glob("context.jsonl*.tmp"))
    assert not (tmp_path / "context_1.jsonl").exists()


@pytest.mark.asyncio
async def test_replace_history_atomic_replace_failure_preserves_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    old_bytes = context_path.read_bytes()
    old_memory = _memory(context)
    monkeypatch.setattr(
        context_module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(context_module.ContextPersistenceError) as raised:
        await context.replace_history(_replacement(_message("new")))

    assert raised.value.category == "atomic_replacement"
    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory
    assert (tmp_path / "context_1.jsonl").read_bytes() == old_bytes
    assert not list(tmp_path.glob("context.jsonl*.tmp"))


@pytest.mark.asyncio
async def test_directory_sync_failure_reports_coherent_visible_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    monkeypatch.setattr(
        context_module,
        "_sync_parent_directory",
        lambda _parent: (_ for _ in ()).throw(OSError("directory fsync failed")),
    )

    with pytest.raises(context_module.ContextPersistenceError) as raised:
        await context.replace_history(_replacement(_message("committed")))

    assert raised.value.category == "visible_commit_durability"
    assert context.history[-1] == _message("committed")
    restored = Context(context_path)
    assert await restored.restore()
    assert _memory(restored) == _memory(context)


@pytest.mark.asyncio
async def test_unsupported_directory_sync_is_not_reported_as_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context_path.write_bytes(b"old")
    context = Context(context_path)
    monkeypatch.setattr(context_module, "_sync_parent_directory", lambda _parent: False)

    commit = await context.replace_history(_replacement(_message("new")))

    assert commit.rotated_file is not None
    assert context.history[-1] == _message("new")


@pytest.mark.asyncio
async def test_cancellation_during_atomic_replace_settles_coherent_new_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    replace_entered = threading.Event()
    release_replace = threading.Event()
    real_replace = context_module.os.replace

    def blocking_replace(source: Path, target: Path) -> None:
        replace_entered.set()
        release_replace.wait()
        real_replace(source, target)

    monkeypatch.setattr(context_module.os, "replace", blocking_replace)
    replacement = asyncio.create_task(context.replace_history(_replacement(_message("new"))))
    assert await asyncio.to_thread(replace_entered.wait, 5)

    replacement.cancel()
    replacement.cancel()
    release_replace.set()
    with pytest.raises(asyncio.CancelledError):
        await replacement

    assert context.history[-1] == _message("new")
    restored = Context(context_path)
    assert await restored.restore()
    assert _memory(restored) == _memory(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["temporary_write", "archive_write"])
async def test_precommit_cancellation_preserves_old_generation_and_cleans_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    old_bytes = context_path.read_bytes()
    old_memory = _memory(context)
    entered = threading.Event()
    release = threading.Event()
    if boundary == "temporary_write":
        original = context_module._prepare_replacement_file

        def block_temporary_write(path: Path, records: Sequence[str]) -> Path:
            entered.set()
            release.wait()
            return original(path, records)

        monkeypatch.setattr(context_module, "_prepare_replacement_file", block_temporary_write)
    else:
        original_archive = context_module._write_rotation_archive

        def block_archive_write(path: Path, content: bytes) -> None:
            entered.set()
            release.wait()
            original_archive(path, content)

        monkeypatch.setattr(context_module, "_write_rotation_archive", block_archive_write)

    replacement = asyncio.create_task(context.replace_history(_replacement(_message("new"))))
    assert await asyncio.to_thread(entered.wait, 5)
    replacement.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await replacement
    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory
    assert not list(tmp_path.glob("context.jsonl*.tmp"))
    assert not (tmp_path / "context_1.jsonl").exists()


@pytest.mark.asyncio
async def test_cleanup_failure_retains_atomic_replace_error_as_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    real_unlink = Path.unlink

    monkeypatch.setattr(
        context_module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    def fail_temp_cleanup(path: Path, missing_ok: bool = False) -> None:
        if path.name.endswith(".tmp"):
            raise OSError("cleanup failed")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_temp_cleanup)

    with pytest.raises(context_module.ContextPersistenceError) as raised:
        await context.replace_history(_replacement(_message("new")))

    assert raised.value.category == "atomic_replacement"
    assert "Cleanup also failed" in "\n".join(raised.value.__notes__)


@pytest.mark.asyncio
async def test_replace_history_serializes_with_concurrent_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    replace_entered = threading.Event()
    release_replace = threading.Event()
    real_replace = context_module.os.replace

    def blocking_replace(source: Path, target: Path) -> None:
        replace_entered.set()
        release_replace.wait()
        real_replace(source, target)

    monkeypatch.setattr(context_module.os, "replace", blocking_replace)
    replacement = asyncio.create_task(
        context.replace_history(_replacement(_message("replacement")))
    )
    assert await asyncio.to_thread(replace_entered.wait, 5)
    append = asyncio.create_task(context.append_message(_message("after replacement")))
    release_replace.set()
    await asyncio.gather(replacement, append)

    assert [message.extract_text("") for message in context.history] == [
        "<system>CHECKPOINT 0</system>",
        "replacement",
        "after replacement",
    ]
    restored = Context(context_path)
    assert await restored.restore()
    assert list(restored.history) == list(context.history)


@pytest.mark.asyncio
async def test_rotation_reservation_failure_is_categorized_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    old_bytes = context_path.read_bytes()
    old_memory = _memory(context)

    async def fail_reservation(_path: Path) -> Path | None:
        raise OSError("listdir failed")

    monkeypatch.setattr(context_module, "next_available_rotation", fail_reservation)

    with pytest.raises(context_module.ContextPersistenceError) as raised:
        await context.replace_history(_replacement(_message("new")))

    assert raised.value.category == "rotation_archive"
    assert isinstance(raised.value.__cause__, OSError)
    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory
    assert not list(tmp_path.glob("context.jsonl*.tmp"))
    assert not (tmp_path / "context_1.jsonl").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "invalid_value", "message"),
    [
        ("system_prompt", 7, "system_prompt"),
        ("messages", [_message("list")], "messages"),
        ("messages", ({"role": "_usage", "token_count": 0},), "messages"),
        ("token_count", True, "token_count"),
        ("token_count", "7", "token_count"),
        ("token_count", -1, "token_count"),
        ("create_checkpoint", 1, "create_checkpoint"),
        ("checkpoint_user_marker", 1, "checkpoint_user_marker"),
    ],
)
async def test_replacement_exact_type_validation_precedes_lock_and_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    invalid_value: object,
    message: str,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context_path.write_bytes(b"old generation")
    context = Context(context_path)
    values: dict[str, object] = {
        "system_prompt": "prompt",
        "messages": (_message("valid"),),
        "token_count": 1,
        "create_checkpoint": True,
        "checkpoint_user_marker": False,
    }
    values[field] = invalid_value
    replacement = context_module.ContextReplacement(
        system_prompt=cast(Any, values["system_prompt"]),
        messages=cast(Any, values["messages"]),
        token_count=cast(Any, values["token_count"]),
        create_checkpoint=cast(Any, values["create_checkpoint"]),
        checkpoint_user_marker=cast(Any, values["checkpoint_user_marker"]),
    )

    monkeypatch.setattr(
        context_module.tempfile,
        "mkstemp",
        lambda *args, **kwargs: pytest.fail("invalid input touched filesystem"),
    )
    async with context._mutation_lock:
        with pytest.raises((TypeError, ValueError), match=message):
            await asyncio.wait_for(context.replace_history(replacement), timeout=0.1)

    assert context_path.read_bytes() == b"old generation"


@pytest.mark.asyncio
async def test_checkpoint_marker_relationship_validation_precedes_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(tmp_path / "context.jsonl")
    monkeypatch.setattr(
        context_module.tempfile,
        "mkstemp",
        lambda *args, **kwargs: pytest.fail("invalid input touched filesystem"),
    )

    with pytest.raises(ValueError, match="checkpoint_user_marker"):
        await context.replace_history(
            _replacement(create_checkpoint=False, checkpoint_user_marker=True)
        )


def test_context_persistence_error_renders_only_safe_path() -> None:
    absolute_path = Path("/private/session-secret/context.jsonl")
    error = context_module.ContextPersistenceError(
        "replace_history", "rotation_archive", absolute_path
    )

    assert str(absolute_path) not in str(error)
    assert "session-secret" not in str(error)
    assert "context.jsonl" in str(error)


def test_visible_commit_durability_error_is_explicit() -> None:
    error = context_module.ContextPersistenceError(
        "replace_history",
        "visible_commit_durability",
        Path("/private/session-secret/context.jsonl"),
    )

    assert "new generation is visible" in str(error)
    assert "power-loss durability is uncertain" in str(error)


@pytest.mark.asyncio
async def test_cancellation_before_archive_reservation_keeps_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    old_bytes = context_path.read_bytes()
    old_memory = _memory(context)
    read_entered = threading.Event()
    release_read = threading.Event()
    original_read = context_module._read_live_bytes

    def blocking_read(path: Path) -> bytes | None:
        read_entered.set()
        release_read.wait()
        return original_read(path)

    monkeypatch.setattr(context_module, "_read_live_bytes", blocking_read)
    replacement = asyncio.create_task(context.replace_history(_replacement(_message("new"))))
    assert await asyncio.to_thread(read_entered.wait, 5)
    replacement.cancel()
    release_read.set()

    with pytest.raises(asyncio.CancelledError):
        await replacement
    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory
    assert not list(tmp_path.glob("context.jsonl*.tmp"))
    assert not (tmp_path / "context_1.jsonl").exists()


@pytest.mark.asyncio
async def test_cancellation_immediately_before_replace_keeps_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    old_bytes = context_path.read_bytes()
    old_memory = _memory(context)
    checkpoint_entered = asyncio.Event()
    release_checkpoint = asyncio.Event()

    async def pause_before_replace() -> None:
        checkpoint_entered.set()
        await release_checkpoint.wait()

    monkeypatch.setattr(context_module, "_before_replacement_commit", pause_before_replace)
    replacement = asyncio.create_task(context.replace_history(_replacement(_message("new"))))
    await checkpoint_entered.wait()
    replacement.cancel()
    release_checkpoint.set()

    with pytest.raises(asyncio.CancelledError):
        await replacement
    assert context_path.read_bytes() == old_bytes
    assert _memory(context) == old_memory
    assert not list(tmp_path.glob("context.jsonl*.tmp"))
    assert not (tmp_path / "context_1.jsonl").exists()


@pytest.mark.asyncio
async def test_cancellation_during_directory_sync_propagates_after_coherent_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    sync_entered = threading.Event()
    release_sync = threading.Event()
    original_sync = context_module._sync_parent_directory

    def blocking_sync(parent: Path) -> bool:
        sync_entered.set()
        release_sync.wait()
        return original_sync(parent)

    monkeypatch.setattr(context_module, "_sync_parent_directory", blocking_sync)
    replacement = asyncio.create_task(context.replace_history(_replacement(_message("new"))))
    assert await asyncio.to_thread(sync_entered.wait, 5)
    replacement.cancel()
    release_sync.set()

    with pytest.raises(asyncio.CancelledError):
        await replacement
    assert context.history[-1] == _message("new")
    restored = Context(context_path)
    assert await restored.restore()
    assert _memory(restored) == _memory(context)


@pytest.mark.asyncio
async def test_second_cancellation_after_commit_settlement_has_started_is_settled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "context.jsonl"
    context = Context(context_path)
    await context.append_message(_message("existing"))
    replace_entered = threading.Event()
    release_replace = threading.Event()
    settlement_entered = asyncio.Event()
    real_replace = context_module.os.replace
    real_wait = context_module.asyncio.wait

    def blocking_replace(source: Path, target: Path) -> None:
        replace_entered.set()
        release_replace.wait()
        real_replace(source, target)

    async def tracked_wait(fs: Any, **kwargs: Any) -> Any:
        settlement_entered.set()
        return await real_wait(fs, **kwargs)

    monkeypatch.setattr(context_module.os, "replace", blocking_replace)
    monkeypatch.setattr(context_module.asyncio, "wait", tracked_wait)
    replacement = asyncio.create_task(context.replace_history(_replacement(_message("new"))))
    assert await asyncio.to_thread(replace_entered.wait, 5)
    replacement.cancel()
    await settlement_entered.wait()
    replacement.cancel()
    release_replace.set()

    with pytest.raises(asyncio.CancelledError):
        await replacement
    assert context.history[-1] == _message("new")
    restored = Context(context_path)
    assert await restored.restore()
    assert _memory(restored) == _memory(context)


def test_parent_directory_sync_non_posix_is_explicitly_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context_module.os, "name", "nt")
    monkeypatch.setattr(
        context_module.os,
        "open",
        lambda *args, **kwargs: pytest.fail("non-POSIX directory sync opened a directory"),
    )

    assert context_module._sync_parent_directory(tmp_path) is False


@pytest.mark.asyncio
async def test_expected_generation_conflict_precedes_replacement_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(tmp_path / "context.jsonl")
    expected_generation = context.mutation_generation
    await context.append_message(_message("concurrent"))
    before_bytes = context.file_backend.read_bytes()
    monkeypatch.setattr(
        context_module.tempfile,
        "mkstemp",
        lambda *args, **kwargs: pytest.fail("generation conflict touched replacement I/O"),
    )

    with pytest.raises(context_module.ContextGenerationConflictError):
        await context.replace_history(
            _replacement(_message("stale")),
            expected_generation=expected_generation,
        )

    assert context.file_backend.read_bytes() == before_bytes
    assert context.history[-1] == _message("concurrent")


@pytest.mark.asyncio
async def test_revert_generation_conflicts_stop_after_bounded_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = Context(tmp_path / "context.jsonl")
    await context.append_message(_message("before"))
    await context.checkpoint(add_user_message=False)
    attempts = 0

    async def conflict_then_forbidden(*_args: object, **_kwargs: object) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise context_module.ContextGenerationConflictError(attempts - 1, attempts)
        raise AssertionError("revert retried past its conflict budget")

    monkeypatch.setattr(context, "replace_history", conflict_then_forbidden)

    with pytest.raises(context_module.ContextGenerationConflictError):
        await context.revert_to(0)

    assert attempts == 3


@pytest.mark.parametrize("unsupported_errno", [errno.EINVAL, errno.ENOTSUP])
def test_parent_directory_sync_treats_known_posix_errors_as_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsupported_errno: int,
) -> None:
    monkeypatch.setattr(context_module.os, "name", "posix")
    monkeypatch.setattr(context_module.os, "open", lambda *_args: 17)
    monkeypatch.setattr(
        context_module.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError(unsupported_errno, "unsupported")),
    )
    closed: list[int] = []
    monkeypatch.setattr(context_module.os, "close", closed.append)

    assert context_module._sync_parent_directory(tmp_path) is False
    assert closed == [17]
