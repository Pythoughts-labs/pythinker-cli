"""memory-1 / ctxmgmt-3: cross-session Recall tool."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pythinker_core.message import Message, TextPart

import pythinker_code.tools.recall as recall_mod
from pythinker_code.session import Session
from pythinker_code.tools.recall import Recall, _rank_sessions, _render_transcript


def _session(sid: str, *, title: str = "", custom_title: str = "", updated_at: float = 0.0) -> Any:
    return SimpleNamespace(
        id=sid,
        title=title,
        updated_at=updated_at,
        state=SimpleNamespace(custom_title=custom_title, plan_slug=None),
    )


def _ranked_ids(sessions: list[Any], *, query: str, current_id: str) -> list[str]:
    ranked = _rank_sessions(cast(Any, sessions), query=query, current_id=current_id, limit=10)
    return [s.id for s in ranked]


def test_rank_matches_session_id_and_plan_slug() -> None:
    sessions = [
        _session("sess-auth-1", custom_title="misc", updated_at=1.0),
        _session(
            "other",
            custom_title="plan work",
            updated_at=2.0,
        ),
    ]
    sessions[1].state.plan_slug = "auth-migration"
    ids = _ranked_ids(sessions, query="auth-migration", current_id="cur")
    assert ids == ["other"]
    ids_by_id = _ranked_ids(sessions, query="sess-auth", current_id="cur")
    assert ids_by_id == ["sess-auth-1"]


def test_rank_excludes_current_and_filters_non_matches() -> None:
    sessions = [
        _session("a", custom_title="auth migration", updated_at=1.0),
        _session("b", custom_title="docs cleanup", updated_at=2.0),
        _session("cur", custom_title="auth current", updated_at=3.0),
    ]
    ids = _ranked_ids(sessions, query="auth", current_id="cur")
    assert ids == ["a"]  # 'b' filtered (no match), 'cur' excluded


def test_rank_orders_by_keyword_then_recency() -> None:
    sessions = [
        _session("old", custom_title="auth tweak", updated_at=1.0),
        _session("new", custom_title="auth tweak", updated_at=5.0),
        _session("strong", custom_title="auth login fix", updated_at=2.0),
    ]
    # "auth login" — 'strong' matches 2 terms, the others 1; ties break by recency.
    ids = _ranked_ids(sessions, query="auth login", current_id="cur")
    assert ids[0] == "strong"
    assert ids[1:] == ["new", "old"]


def test_rank_empty_query_lists_recent() -> None:
    sessions = [
        _session("a", custom_title="x", updated_at=1.0),
        _session("b", custom_title="y", updated_at=2.0),
    ]
    assert _ranked_ids(sessions, query="", current_id="cur") == ["b", "a"]


def _write_log(path: Path, messages: list[Message]) -> None:
    path.write_text("\n".join(m.model_dump_json() for m in messages), encoding="utf-8")


def test_render_transcript_skips_internal_and_renders_roles(tmp_path: Path) -> None:
    log = tmp_path / "context.jsonl"
    lines = [
        Message(role="user", content=[TextPart(text="fix the auth bug")]).model_dump_json(),
        Message(role="assistant", content=[TextPart(text="found it in auth.py")]).model_dump_json(),
        # Internal/non-Message lines on disk (e.g. checkpoint markers) must be skipped.
        '{"role": "_checkpoint", "content": [{"type": "text", "text": "should be skipped"}]}',
    ]
    log.write_text("\n".join(lines), encoding="utf-8")
    rendered = _render_transcript(log, budget=10_000)
    assert "[user] fix the auth bug" in rendered
    assert "[assistant] found it in auth.py" in rendered
    assert "should be skipped" not in rendered


def test_render_transcript_strips_private_spans(tmp_path: Path) -> None:
    log = tmp_path / "context.jsonl"
    _write_log(
        log, [Message(role="user", content=[TextPart(text="keep <private>SECRET</private>this")])]
    )
    rendered = _render_transcript(log, budget=10_000)
    assert "SECRET" not in rendered
    assert "keep" in rendered and "this" in rendered


def test_render_transcript_redacts_blocked_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        recall_mod, "sanitize_candidate_block", lambda text: None if "leak" in text else text
    )
    log = tmp_path / "context.jsonl"
    _write_log(
        log,
        [
            Message(role="user", content=[TextPart(text="safe line")]),
            Message(role="assistant", content=[TextPart(text="leak the key abc")]),
        ],
    )
    rendered = _render_transcript(log, budget=10_000)
    assert "[user] safe line" in rendered
    assert "[assistant] [redacted]" in rendered
    assert "abc" not in rendered


def test_render_transcript_budget_truncates(tmp_path: Path) -> None:
    log = tmp_path / "context.jsonl"
    _write_log(
        log,
        [Message(role="user", content=[TextPart(text="x" * 200)]) for _ in range(20)],
    )
    rendered = _render_transcript(log, budget=300)
    assert "truncated to fit" in rendered
    assert len(rendered) < 700


def test_render_transcript_supports_message_window(tmp_path: Path) -> None:
    log = tmp_path / "context.jsonl"
    log.write_text(
        "\n".join(
            [
                '{"role": "_checkpoint", "content": [{"type": "text", "text": "internal"}]}',
                Message(role="user", content=[TextPart(text="first")]).model_dump_json(),
                Message(role="assistant", content=[TextPart(text="second")]).model_dump_json(),
                Message(role="user", content=[TextPart(text="third")]).model_dump_json(),
            ]
        ),
        encoding="utf-8",
    )

    rendered = _render_transcript(log, budget=10_000, message_offset=1, max_messages=1)

    assert "[assistant] second" in rendered
    assert "first" not in rendered
    assert "third" not in rendered
    assert "internal" not in rendered


async def test_recall_search_lists_matching_sessions(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list(work_dir: Any) -> list[Any]:
        return [
            _session("s1", custom_title="auth migration", updated_at=2.0),
            _session("s2", custom_title="ui polish", updated_at=1.0),
        ]

    monkeypatch.setattr(Session, "list", staticmethod(fake_list))
    result = await Recall(runtime)(Recall.params(mode="search", query="auth"))

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "s1" in result.output
    assert "s2" not in result.output


async def test_recall_search_includes_plan_slug_when_present(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list(work_dir: Any) -> list[Any]:
        session = _session("s1", custom_title="plan work", updated_at=2.0)
        session.state.plan_slug = "auth-migration"
        return [session]

    monkeypatch.setattr(Session, "list", staticmethod(fake_list))
    result = await Recall(runtime)(Recall.params(mode="search", query="auth-migration"))

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "plan_slug: auth-migration" in result.output


async def test_recall_read_returns_untrusted_transcript(
    runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "context.jsonl"
    _write_log(log, [Message(role="user", content=[TextPart(text="prior decision: use JWT")])])
    fake = SimpleNamespace(id="s1", context_file=log)

    async def fake_find(work_dir: Any, session_id: str) -> Any:
        return fake if session_id == "s1" else None

    monkeypatch.setattr(Session, "find", staticmethod(fake_find))
    result = await Recall(runtime)(Recall.params(mode="read", session_id="s1"))

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "prior decision: use JWT" in result.output
    assert "untrusted_data" in result.output


async def test_recall_read_passes_message_window(
    runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "context.jsonl"
    _write_log(
        log,
        [
            Message(role="user", content=[TextPart(text="first")]),
            Message(role="assistant", content=[TextPart(text="second")]),
        ],
    )
    fake = SimpleNamespace(id="s1", context_file=log)

    async def fake_find(work_dir: Any, session_id: str) -> Any:
        return fake if session_id == "s1" else None

    monkeypatch.setattr(Session, "find", staticmethod(fake_find))
    result = await Recall(runtime)(
        Recall.params(mode="read", session_id="s1", message_offset=1, max_messages=1)
    )

    assert not result.is_error
    assert isinstance(result.output, str)
    assert "second" in result.output
    assert "first" not in result.output


async def test_recall_read_unknown_session_errors(runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_find(work_dir: Any, session_id: str) -> Any:
        return None

    monkeypatch.setattr(Session, "find", staticmethod(fake_find))
    result = await Recall(runtime)(Recall.params(mode="read", session_id="nope"))
    assert result.is_error
    assert result.brief == "Unknown session"


async def test_recall_read_without_session_id_errors(runtime) -> None:
    result = await Recall(runtime)(Recall.params(mode="read"))
    assert result.is_error
    assert result.brief == "Missing session_id"


async def test_recall_read_rejects_traversal_session_id(runtime) -> None:
    # A crafted session_id must not be able to traverse out of the workspace.
    result = await Recall(runtime)(Recall.params(mode="read", session_id="../other-workspace"))
    assert result.is_error
    assert result.brief == "Invalid session_id"


async def test_recall_read_rejects_current_session(runtime) -> None:
    result = await Recall(runtime)(Recall.params(mode="read", session_id=runtime.session.id))
    assert result.is_error
    assert result.brief == "Current session"


def test_render_transcript_tolerates_invalid_utf8(tmp_path: Path) -> None:
    log = tmp_path / "context.jsonl"
    good = Message(role="user", content=[TextPart(text="hello")]).model_dump_json()
    # A valid line, then a line with invalid UTF-8 bytes — must not crash.
    log.write_bytes(good.encode("utf-8") + b"\n\xff\xfe not valid utf8\n")
    rendered = _render_transcript(log, budget=10_000)
    assert "[user] hello" in rendered
