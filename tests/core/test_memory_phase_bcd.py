from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pythinker_core.message import Message, TextPart
from pythinker_core.tooling.empty import EmptyToolset
from pythinker_host.path import HostPath

import pythinker_code.soul.pythinkersoul as pythinkersoul_module  # alias used for monkeypatch
from pythinker_code import scratchpad
from pythinker_code.config import Config
from pythinker_code.memory.consolidation import generate_inbox_candidates
from pythinker_code.memory.harvest import CompactionHarvester
from pythinker_code.memory.recap import build_session_recap, content_hash
from pythinker_code.memory.retriever import (
    LexicalRetriever,
    RankedBlock,
    RecallQuery,
    estimate_tokens,
)
from pythinker_code.project_memory import ProjectMemoryStore
from pythinker_code.session_state import SessionState, TodoItemState
from pythinker_code.soul.agent import Agent, Runtime
from pythinker_code.soul.context import Context

PythinkerSoul = pythinkersoul_module.PythinkerSoul
TurnOutcome = pythinkersoul_module.TurnOutcome


def _hp(p: Path) -> HostPath:
    return HostPath.unsafe_from_local_path(p)


@pytest.fixture(autouse=True)
def _reset_scratchpad_verification():
    scratchpad._VERIFIED_WORK_DIRS.clear()
    yield
    scratchpad._VERIFIED_WORK_DIRS.clear()


def test_content_hash_is_stable_and_normalized():
    assert content_hash(tier="memory", title="T", body="Body") == content_hash(
        tier=" MEMORY ", title="t", body=" body "
    )


def test_stop_time_memory_harvest_defaults_off():
    assert Config().memory.harvest_on_stop is False


async def test_append_journal_prepends_and_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path / "share"))
    store = ProjectMemoryStore(_hp(tmp_path / "repo"))
    assert (await store.append_journal("first recap")).ok is True
    assert (await store.append_journal("second recap")).ok is True
    assert (await store.append_journal("first recap")).ok is True
    assert await store._read_journal(last_n=10) == ["second recap", "first recap"]


def test_build_session_recap_has_stable_schema():
    state = SessionState(
        custom_title="memory work",
        todos=[
            TodoItemState(title="ship phase b", status="done"),
            TodoItemState(title="ship phase c", status="pending"),
        ],
    )
    recap = build_session_recap(
        state=state,
        session_id="abc12345-0000",
        request="implement memory",
        scratch_blocks=["decision: lexical first"],
        files_read=["src/x.py"],
        files_modified=["src/y.py"],
    )
    for heading in (
        "## request",
        "## investigated",
        "## learned",
        "## completed",
        "## next_steps",
        "## open_todos",
        "## files_read",
        "## files_modified",
        "## labels",
    ):
        assert heading in recap
    assert "ship phase c" in recap


def test_compaction_harvester_extracts_safe_assistant_notes():
    messages = [
        Message(
            role="assistant",
            content=[
                TextPart(
                    text="Decision: use lexical recall\nBlocker: <private>secret</private>\nNext: run tests"
                )
            ],
        ),
        Message(role="tool", content=[TextPart(text="Decision: ignore tool output")]),
    ]
    notes = CompactionHarvester().harvest(messages)
    assert [(note.kind, note.content) for note in notes] == [
        ("decision", "use lexical recall"),
        ("next", "run tests"),
    ]


def _ranked(content: str, *, title: str = "retriever") -> RankedBlock:
    return RankedBlock(
        tier="memory",
        source_path="MEMORY.md",
        source_id=None,
        session_id=None,
        title=title,
        labels=(),
        files=(),
        created_at_epoch=time.time(),
        token_estimate=estimate_tokens(content),
        score=0.0,
        content=content,
    )


async def test_lexical_retriever_matches_unicode_terms():
    out = await LexicalRetriever(
        [_ranked("решение использовать кеш"), _ranked("english only")]
    ).retrieve(RecallQuery(text="решение"), budget_tokens=100)
    assert out and out[0].content == "решение использовать кеш"


async def test_lexical_retriever_uses_query_labels_as_terms():
    out = await LexicalRetriever([_ranked("lexical sqlite fallback")]).retrieve(
        RecallQuery(labels=("sqlite fallback",)), budget_tokens=100
    )
    assert out and out[0].content == "lexical sqlite fallback"


async def test_generate_inbox_candidates_skips_approved_scratch_note(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path / "share"))
    repo = tmp_path / "repo"
    scratch = repo / ".pythinker" / "scratch"
    scratch.mkdir(parents=True)
    (scratch / "notes.md").write_text(
        "### decision — 2026-05-30 10:00\n\nUse lexical recall for project memory.",
        encoding="utf-8",
    )
    store = ProjectMemoryStore(_hp(repo))
    assert (await store.add("memory", "Use lexical recall for project memory.")).ok is True

    assert await generate_inbox_candidates(store, _hp(repo)) == []


async def test_generate_inbox_candidates_ignores_corrupt_duplicate_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path / "share"))
    repo = tmp_path / "repo"
    scratch = repo / ".pythinker" / "scratch"
    scratch.mkdir(parents=True)
    (scratch / "notes.md").write_text(
        "### decision — 2026-05-30 10:00\n\nKeep corrupt inbox files from crashing harvest.",
        encoding="utf-8",
    )
    store = ProjectMemoryStore(_hp(repo))
    first = await generate_inbox_candidates(store, _hp(repo))
    assert len(first) == 1
    root = await store._ensure_dir()  # pyright: ignore[reportPrivateUsage]
    inbox = root / "memory" / "inbox"
    (inbox / f"{first[0].id}.json").write_text("{not json", encoding="utf-8")

    assert await generate_inbox_candidates(store, _hp(repo)) == []


def _make_memory_soul(runtime: Runtime, tmp_path: Path) -> PythinkerSoul:
    runtime.work_dir_override = _hp(tmp_path)
    agent = Agent(
        name="Memory Stop Agent",
        system_prompt="Test prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    return PythinkerSoul(agent, context=Context(file_backend=tmp_path / "history.jsonl"))


async def test_stop_time_memory_harvest_default_off_noops(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("pythinker_code.scratchpad._is_local_host", lambda: True)
    monkeypatch.setattr("pythinker_code.scratchpad._is_verified", lambda _wd: True)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _msg: None)
    monkeypatch.setattr(runtime.oauth, "ensure_fresh", AsyncMock())
    soul = _make_memory_soul(runtime, tmp_path)

    async def fake_turn(user_message: Message) -> TurnOutcome:
        await soul.context.append_message(user_message)
        final = Message(role="assistant", content=[TextPart(text="Decision: do not persist")])
        await soul.context.append_message(final)
        return TurnOutcome(stop_reason="no_tool_calls", final_message=final, step_count=1)

    monkeypatch.setattr(soul, "_turn", fake_turn)

    await soul.run("remember nothing")

    assert not (tmp_path / ".pythinker" / "scratch").exists()


async def test_stop_time_memory_harvest_opt_in_stages_sanitized_deduped_notes(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    runtime.config.memory.harvest_on_stop = True
    monkeypatch.setattr("pythinker_code.scratchpad._is_local_host", lambda: True)
    monkeypatch.setattr("pythinker_code.scratchpad._is_verified", lambda _wd: True)
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _msg: None)
    monkeypatch.setattr(runtime.oauth, "ensure_fresh", AsyncMock())
    soul = _make_memory_soul(runtime, tmp_path)

    async def fake_turn(user_message: Message) -> TurnOutcome:
        await soul.context.append_message(user_message)
        final = Message(
            role="assistant",
            content=[
                TextPart(
                    text=(
                        "Decision: stage this fact\n"
                        "Decision: stage this fact\n"
                        "Next: <private>do not stage</private>\n"
                        "Next: run focused memory tests"
                    )
                )
            ],
        )
        await soul.context.append_message(final)
        return TurnOutcome(stop_reason="no_tool_calls", final_message=final, step_count=1)

    monkeypatch.setattr(soul, "_turn", fake_turn)

    await soul.run("remember safely")

    files = list((tmp_path / ".pythinker" / "scratch").glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert text.count("stage this fact") == 1
    assert "run focused memory tests" in text
    assert "do not stage" not in text
    assert "source:stop" in text


async def test_stop_time_memory_harvest_failure_does_not_break_turn(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    runtime.config.memory.harvest_on_stop = True
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _msg: None)
    monkeypatch.setattr(runtime.oauth, "ensure_fresh", AsyncMock())
    append_note = AsyncMock(side_effect=RuntimeError("scratch unavailable"))
    monkeypatch.setattr(
        "pythinker_code.scratchpad.append_scratch_note",
        append_note,
    )
    soul = _make_memory_soul(runtime, tmp_path)

    async def fake_turn(user_message: Message) -> TurnOutcome:
        await soul.context.append_message(user_message)
        final = Message(role="assistant", content=[TextPart(text="Decision: resilient turn")])
        await soul.context.append_message(final)
        return TurnOutcome(stop_reason="no_tool_calls", final_message=final, step_count=1)

    monkeypatch.setattr(soul, "_turn", fake_turn)

    await soul.run("do not crash")

    append_note.assert_awaited_once()
    assert soul.context.history[-1].extract_text(" ") == "Decision: resilient turn"


async def test_stop_time_memory_harvest_non_appended_note_does_not_rearm(
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    runtime.config.memory.harvest_on_stop = True
    monkeypatch.setattr(pythinkersoul_module, "wire_send", lambda _msg: None)
    monkeypatch.setattr(runtime.oauth, "ensure_fresh", AsyncMock())
    append_note = AsyncMock(
        return_value=scratchpad.ScratchpadAppendResult(False, "disabled_not_ignored")
    )
    monkeypatch.setattr("pythinker_code.scratchpad.append_scratch_note", append_note)
    soul = _make_memory_soul(runtime, tmp_path)
    rearmed: list[str] = []
    monkeypatch.setattr(soul, "rearm_injection", lambda key: rearmed.append(key))

    async def fake_turn(user_message: Message) -> TurnOutcome:
        await soul.context.append_message(user_message)
        final = Message(role="assistant", content=[TextPart(text="Decision: refused note")])
        await soul.context.append_message(final)
        return TurnOutcome(stop_reason="no_tool_calls", final_message=final, step_count=1)

    monkeypatch.setattr(soul, "_turn", fake_turn)

    await soul.run("safe refusal")

    append_note.assert_awaited_once()
    assert rearmed == []
