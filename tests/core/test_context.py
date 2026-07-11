"""Tests for Context class, focusing on system prompt persistence in context.jsonl."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pythinker_core.message import Message, Role

from pythinker_code.soul.context import Context
from pythinker_code.wire.types import TextPart


def _write_lines(path: Path, lines: list[dict]) -> None:
    """Write JSON lines to a file."""
    path.write_text(
        "".join(json.dumps(line) + "\n" for line in lines),
        encoding="utf-8",
    )


def _read_lines(path: Path) -> list[dict]:
    """Read all JSON lines from a file."""
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _message_dict(role: Role, text: str) -> dict:
    """Create a serialized message dict."""
    return json.loads(
        Message(role=role, content=[TextPart(text=text)]).model_dump_json(exclude_none=True)
    )


@pytest.fixture
def mixed_context_jsonl(tmp_path: Path) -> Path:
    path = tmp_path / "mixed-context.jsonl"
    path.write_text(
        '{"role":"_system_prompt","content":"Frozen prompt"}\n'
        '{"role":"user","content":"Before checkpoint"}\n'
        '{"role":"_checkpoint","id":3}\n'
        '{"role":"assistant","content":"Recorded answer"}\n'
        '{"role":"_usage","token_count":144}\n'
        '{"role":"user","content":"After usage"}\n',
        encoding="utf-8",
    )
    return path


# --- write_system_prompt tests ---


@pytest.mark.asyncio
async def test_write_system_prompt_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    path.touch()
    ctx = Context(file_backend=path)

    await ctx.write_system_prompt("You are a helpful assistant.")

    assert ctx.system_prompt == "You are a helpful assistant."
    lines = _read_lines(path)
    assert len(lines) == 1
    assert lines[0] == {"role": "_system_prompt", "content": "You are a helpful assistant."}


@pytest.mark.asyncio
async def test_write_system_prompt_nonexistent_file(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    ctx = Context(file_backend=path)

    await ctx.write_system_prompt("Test prompt")

    assert ctx.system_prompt == "Test prompt"
    assert path.exists()
    lines = _read_lines(path)
    assert lines[0]["role"] == "_system_prompt"


@pytest.mark.asyncio
async def test_write_system_prompt_prepends_to_existing(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    msg = _message_dict("user", "Hello")
    _write_lines(path, [msg])

    ctx = Context(file_backend=path)
    await ctx.write_system_prompt("Prepended prompt")

    lines = _read_lines(path)
    assert len(lines) == 2
    assert lines[0] == {"role": "_system_prompt", "content": "Prepended prompt"}
    assert lines[1] == msg


# --- restore tests ---


@pytest.mark.asyncio
async def test_restore_preserves_literal_mixed_record_contract(mixed_context_jsonl: Path) -> None:
    ctx = Context(file_backend=mixed_context_jsonl)

    restored = await ctx.restore()

    assert restored is True
    assert ctx.system_prompt == "Frozen prompt"
    assert ctx.n_checkpoints == 4
    assert ctx.token_count == 144
    assert tuple(ctx.history) == (
        Message(role="user", content=[TextPart(text="Before checkpoint")]),
        Message(role="assistant", content=[TextPart(text="Recorded answer")]),
        Message(role="user", content=[TextPart(text="After usage")]),
    )


@pytest.mark.asyncio
async def test_restore_reads_system_prompt(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [
            {"role": "_system_prompt", "content": "Frozen prompt"},
            _message_dict("user", "Hello"),
        ],
    )

    ctx = Context(file_backend=path)
    restored = await ctx.restore()

    assert restored is True
    assert ctx.system_prompt == "Frozen prompt"


@pytest.mark.asyncio
async def test_restore_without_system_prompt(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(path, [_message_dict("user", "Hello")])

    ctx = Context(file_backend=path)
    await ctx.restore()

    assert ctx.system_prompt is None


@pytest.mark.asyncio
async def test_restore_system_prompt_excluded_from_history(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [
            {"role": "_system_prompt", "content": "System prompt here"},
            _message_dict("user", "Hello"),
            _message_dict("assistant", "Hi there"),
        ],
    )

    ctx = Context(file_backend=path)
    await ctx.restore()

    assert ctx.system_prompt == "System prompt here"
    assert len(ctx.history) == 2
    assert all(msg.role in ("user", "assistant") for msg in ctx.history)


@pytest.mark.asyncio
async def test_restore_with_all_record_types(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [
            {"role": "_system_prompt", "content": "The system prompt"},
            _message_dict("user", "Hello"),
            {"role": "_usage", "token_count": 42},
            {"role": "_checkpoint", "id": 0},
            _message_dict("assistant", "World"),
            {"role": "_checkpoint", "id": 1},
        ],
    )

    ctx = Context(file_backend=path)
    await ctx.restore()

    assert ctx.system_prompt == "The system prompt"
    assert ctx.token_count == 42
    assert ctx.n_checkpoints == 2
    assert len(ctx.history) == 2


@pytest.mark.asyncio
async def test_restore_skips_malformed_trailing_line(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    valid_lines = [
        {"role": "_system_prompt", "content": "Frozen prompt"},
        _message_dict("user", "Hello"),
    ]
    path.write_text(
        "".join(json.dumps(line) + "\n" for line in valid_lines)
        + '{"role":"assistant","content":"unterminated\n',
        encoding="utf-8",
    )

    ctx = Context(file_backend=path)
    restored = await ctx.restore()

    assert restored is True
    assert ctx.system_prompt == "Frozen prompt"
    assert len(ctx.history) == 1
    assert ctx.history[0].role == "user"


@pytest.mark.asyncio
async def test_restore_skips_truncated_utf8_trailing_line(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    valid_prefix = (
        json.dumps({"role": "_system_prompt", "content": "Frozen prompt"}, ensure_ascii=False)
        + "\n"
        + json.dumps(_message_dict("user", "Hello"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    path.write_bytes(valid_prefix + b'{"role":"assistant","content":"\xe4\xb8\n')

    ctx = Context(file_backend=path)
    restored = await ctx.restore()

    assert restored is True
    assert ctx.system_prompt == "Frozen prompt"
    assert len(ctx.history) == 1
    assert ctx.history[0].role == "user"


# --- clear tests ---


@pytest.mark.asyncio
async def test_clear_resets_system_prompt(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [
            {"role": "_system_prompt", "content": "Will be cleared"},
            _message_dict("user", "Hello"),
        ],
    )

    ctx = Context(file_backend=path)
    await ctx.restore()
    assert ctx.system_prompt == "Will be cleared"

    await ctx.clear()

    assert ctx.system_prompt is None
    assert len(ctx.history) == 0


# --- revert_to tests ---


@pytest.mark.asyncio
async def test_revert_preserves_system_prompt(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [
            {"role": "_system_prompt", "content": "Preserved prompt"},
            _message_dict("user", "Before checkpoint"),
            {"role": "_checkpoint", "id": 0},
            _message_dict("user", "After checkpoint"),
            {"role": "_checkpoint", "id": 1},
        ],
    )

    ctx = Context(file_backend=path)
    await ctx.restore()
    assert ctx.system_prompt == "Preserved prompt"
    assert len(ctx.history) == 2

    # revert_to(1) removes checkpoint 1 and everything after it;
    # both messages (before checkpoint 0 and between 0 and 1) are preserved
    await ctx.revert_to(1)

    assert ctx.system_prompt == "Preserved prompt"
    assert len(ctx.history) == 2

    # revert_to(0) removes checkpoint 0 and everything after it;
    # only the first message is preserved
    await ctx.revert_to(0)

    assert ctx.system_prompt == "Preserved prompt"
    assert len(ctx.history) == 1


@pytest.mark.parametrize("operation", ["revert", "clear"])
@pytest.mark.parametrize("error", [OSError("disk full"), asyncio.CancelledError()])
async def test_history_replacement_failure_preserves_exact_generation(
    tmp_path: Path,
    operation: str,
    error: BaseException,
) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [
            {"role": "_system_prompt", "content": "Preserved prompt"},
            _message_dict("user", "Before checkpoint"),
            {"role": "_checkpoint", "id": 0},
            _message_dict("assistant", "After checkpoint"),
            {"role": "_usage", "token_count": 23},
            {"role": "_checkpoint", "id": 1},
        ],
    )
    ctx = Context(file_backend=path)
    await ctx.restore()
    before_bytes = path.read_bytes()
    before_memory = (
        tuple(ctx.history),
        ctx.system_prompt,
        ctx.token_count,
        ctx.token_count_with_pending,
        ctx.n_checkpoints,
    )
    replace_history = AsyncMock(side_effect=error)
    ctx.replace_history = replace_history  # type: ignore[method-assign]

    with pytest.raises(type(error)):
        if operation == "revert":
            await ctx.revert_to(1)
        else:
            await ctx.clear()

    replace_history.assert_awaited_once()
    assert path.read_bytes() == before_bytes
    assert (
        tuple(ctx.history),
        ctx.system_prompt,
        ctx.token_count,
        ctx.token_count_with_pending,
        ctx.n_checkpoints,
    ) == before_memory


@pytest.mark.asyncio
async def test_revert_preserves_system_prompt_in_file(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [
            {"role": "_system_prompt", "content": "File preserved"},
            _message_dict("user", "Message 1"),
            {"role": "_checkpoint", "id": 0},
            _message_dict("user", "Message 2"),
            {"role": "_checkpoint", "id": 1},
        ],
    )

    ctx = Context(file_backend=path)
    await ctx.restore()
    await ctx.revert_to(1)

    lines = _read_lines(path)
    assert lines[0] == {"role": "_system_prompt", "content": "File preserved"}


@pytest.mark.asyncio
async def test_revert_skips_malformed_line_before_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    path.write_text(
        "".join(
            [
                json.dumps({"role": "_system_prompt", "content": "Recovered prompt"}) + "\n",
                json.dumps(_message_dict("user", "Before bad line")) + "\n",
                '{"role":"assistant","content":"unterminated\n',
                json.dumps({"role": "_checkpoint", "id": 0}) + "\n",
            ]
        ),
        encoding="utf-8",
    )

    ctx = Context(file_backend=path)
    await ctx.restore()
    await ctx.revert_to(0)

    lines = _read_lines(path)
    assert lines == [
        {"role": "_system_prompt", "content": "Recovered prompt"},
        _message_dict("user", "Before bad line"),
    ]


@pytest.mark.asyncio
async def test_revert_skips_structurally_invalid_json_object_before_checkpoint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "context.jsonl"
    path.write_text(
        "".join(
            [
                json.dumps({"role": "_system_prompt", "content": "Recovered prompt"}) + "\n",
                json.dumps(_message_dict("user", "Before bad line")) + "\n",
                json.dumps({"oops": 1}) + "\n",
                json.dumps({"role": "_checkpoint", "id": 0}) + "\n",
            ]
        ),
        encoding="utf-8",
    )

    ctx = Context(file_backend=path)
    await ctx.restore()
    await ctx.revert_to(0)

    lines = _read_lines(path)
    assert lines == [
        {"role": "_system_prompt", "content": "Recovered prompt"},
        _message_dict("user", "Before bad line"),
    ]


@pytest.mark.asyncio
async def test_revert_skips_truncated_utf8_line_before_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    path.write_bytes(
        (
            json.dumps(
                {"role": "_system_prompt", "content": "Recovered prompt"}, ensure_ascii=False
            )
            + "\n"
            + json.dumps(_message_dict("user", "Before bad line"), ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        + b'{"role":"assistant","content":"\xe4\xb8\n'
        + (json.dumps({"role": "_checkpoint", "id": 0}) + "\n").encode("utf-8")
    )

    ctx = Context(file_backend=path)
    await ctx.restore()
    await ctx.revert_to(0)

    lines = _read_lines(path)
    assert lines == [
        {"role": "_system_prompt", "content": "Recovered prompt"},
        _message_dict("user", "Before bad line"),
    ]


# --- round-trip tests ---


@pytest.mark.asyncio
async def test_write_system_prompt_then_restore(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    path.touch()

    ctx1 = Context(file_backend=path)
    await ctx1.write_system_prompt("Round trip prompt")

    ctx2 = Context(file_backend=path)
    await ctx2.restore()

    assert ctx2.system_prompt == "Round trip prompt"


@pytest.mark.asyncio
async def test_write_append_messages_then_restore(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    path.touch()

    ctx1 = Context(file_backend=path)
    await ctx1.write_system_prompt("Frozen system prompt")
    await ctx1.append_message(Message(role="user", content=[TextPart(text="Hello")]))
    await ctx1.append_message(Message(role="assistant", content=[TextPart(text="Hi")]))

    ctx2 = Context(file_backend=path)
    await ctx2.restore()

    assert ctx2.system_prompt == "Frozen system prompt"
    assert len(ctx2.history) == 2
    assert ctx2.history[0].role == "user"
    assert ctx2.history[1].role == "assistant"


@pytest.mark.asyncio
async def test_prepend_then_restore(tmp_path: Path) -> None:
    """Legacy session migration: prepend system prompt to existing messages, then restore."""
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [
            _message_dict("user", "Old message 1"),
            _message_dict("assistant", "Old reply 1"),
        ],
    )

    ctx1 = Context(file_backend=path)
    await ctx1.write_system_prompt("Migrated prompt")

    ctx2 = Context(file_backend=path)
    await ctx2.restore()

    assert ctx2.system_prompt == "Migrated prompt"
    assert len(ctx2.history) == 2


# --- file format verification ---


@pytest.mark.asyncio
async def test_system_prompt_is_first_line_in_file(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    path.touch()

    ctx = Context(file_backend=path)
    await ctx.write_system_prompt("First line prompt")
    await ctx.append_message(Message(role="user", content=[TextPart(text="Second line")]))
    await ctx.checkpoint(add_user_message=False)
    await ctx.update_token_count(100)

    lines = _read_lines(path)
    assert lines[0]["role"] == "_system_prompt"
    assert lines[0]["content"] == "First line prompt"
    assert lines[1]["role"] == "user"
    assert lines[2]["role"] == "_checkpoint"
    assert lines[3]["role"] == "_usage"
