"""Model-switch context continuity.

/model used to discard the whole conversation ("Starting fresh session").
The switch now seeds the new session with a summary produced by the
OUTGOING model — only plain text crosses the model boundary, sidestepping
provider-specific message formats — and falls back to a fresh session on
any failure.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
import pythinker_core
from pythinker_core.chat_provider import APIStatusError
from pythinker_core.message import Message

from pythinker_code.config import Config
from pythinker_code.llm import LLM
from pythinker_code.provider_compatibility import default_provider_compatibility
from pythinker_code.soul.compaction import SimpleCompaction
from pythinker_code.wire.types import TextPart


class _FakeChatProvider:
    """Minimal provider double supporting `with_generation_kwargs`."""

    def with_generation_kwargs(self, **kwargs: object) -> _FakeChatProvider:
        return self


def _fake_llm() -> LLM:
    return cast(
        LLM,
        SimpleNamespace(
            chat_provider=_FakeChatProvider(),
            provider_config=None,
            compatibility=default_provider_compatibility(),
        ),
    )


def _history(n_pairs: int = 3) -> list[Message]:
    messages: list[Message] = []
    for index in range(n_pairs):
        messages.append(Message(role="user", content=[TextPart(text=f"request {index}")]))
        messages.append(Message(role="assistant", content=[TextPart(text=f"reply {index}")]))
    return messages


def _summary_step(summary: str = "carried summary"):
    async def _step(**kwargs):
        return SimpleNamespace(
            message=Message(role="assistant", content=[TextPart(text=summary)]),
            usage=None,
        )

    return _step


class TestSummarizeAll:
    @pytest.mark.asyncio
    async def test_returns_plain_text_summary(self, monkeypatch) -> None:
        monkeypatch.setattr(pythinker_core, "step", _summary_step())

        summary = await SimpleCompaction().summarize_all(_history(), llm=_fake_llm())

        assert summary == "carried summary"

    @pytest.mark.asyncio
    async def test_empty_history_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(pythinker_core, "step", _summary_step())

        assert await SimpleCompaction().summarize_all([], llm=_fake_llm()) is None

    @pytest.mark.asyncio
    async def test_overflow_halving_applies(self, monkeypatch) -> None:
        calls: list[int] = []

        async def _step(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise APIStatusError(400, "maximum context length exceeded")
            return SimpleNamespace(
                message=Message(role="assistant", content=[TextPart(text="short summary")]),
                usage=None,
            )

        monkeypatch.setattr(pythinker_core, "step", _step)

        summary = await SimpleCompaction().summarize_all(_history(8), llm=_fake_llm())

        assert summary == "short summary"
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_exhausted_overflow_returns_none(self, monkeypatch) -> None:
        async def _step(**kwargs):
            raise APIStatusError(400, "maximum context length exceeded")

        monkeypatch.setattr(pythinker_core, "step", _step)

        assert await SimpleCompaction().summarize_all(_history(), llm=_fake_llm()) is None


class TestCarryoverConfig:
    def test_carryover_defaults_on(self) -> None:
        assert Config(default_model="", models={}, providers={}).model_switch_carryover is True


class TestCarryContextToSession:
    @pytest.mark.asyncio
    async def test_seeds_new_session_context(self, runtime, tmp_path, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from pythinker_core.tooling.simple import SimpleToolset

        from pythinker_code.soul.agent import Agent
        from pythinker_code.soul.context import Context
        from pythinker_code.soul.pythinkersoul import PythinkerSoul
        from pythinker_code.ui.shell.slash import _carry_context_to_session

        agent = Agent(name="Carry", system_prompt="sys", toolset=SimpleToolset(), runtime=runtime)
        context = Context(file_backend=tmp_path / "old.jsonl")
        await context.append_message(_history())
        soul = PythinkerSoul(agent, context=context)
        monkeypatch.setattr(
            SimpleCompaction, "summarize_all", AsyncMock(return_value="THE SUMMARY")
        )
        new_session = SimpleNamespace(context_file=tmp_path / "new" / "context.jsonl")
        new_session.context_file.parent.mkdir(parents=True)

        carried = await _carry_context_to_session(soul, new_session)

        assert carried is True
        seeded = Context(file_backend=new_session.context_file)
        assert await seeded.restore()
        assert "THE SUMMARY" in seeded.history[0].extract_text(" ")

    @pytest.mark.asyncio
    async def test_empty_history_carries_nothing(self, runtime, tmp_path) -> None:
        from pythinker_core.tooling.simple import SimpleToolset

        from pythinker_code.soul.agent import Agent
        from pythinker_code.soul.context import Context
        from pythinker_code.soul.pythinkersoul import PythinkerSoul
        from pythinker_code.ui.shell.slash import _carry_context_to_session

        agent = Agent(name="Carry", system_prompt="sys", toolset=SimpleToolset(), runtime=runtime)
        soul = PythinkerSoul(agent, context=Context(file_backend=tmp_path / "old.jsonl"))
        new_session = SimpleNamespace(context_file=tmp_path / "new" / "context.jsonl")
        new_session.context_file.parent.mkdir(parents=True)

        carried = await _carry_context_to_session(soul, new_session)

        assert carried is False
        assert not new_session.context_file.exists()

    @pytest.mark.asyncio
    async def test_summarization_failure_falls_back_to_fresh(
        self, runtime, tmp_path, monkeypatch
    ) -> None:
        from unittest.mock import AsyncMock

        from pythinker_core.tooling.simple import SimpleToolset

        from pythinker_code.soul.agent import Agent
        from pythinker_code.soul.context import Context
        from pythinker_code.soul.pythinkersoul import PythinkerSoul
        from pythinker_code.ui.shell.slash import _carry_context_to_session

        agent = Agent(name="Carry", system_prompt="sys", toolset=SimpleToolset(), runtime=runtime)
        context = Context(file_backend=tmp_path / "old.jsonl")
        await context.append_message(_history())
        soul = PythinkerSoul(agent, context=context)
        monkeypatch.setattr(
            SimpleCompaction, "summarize_all", AsyncMock(side_effect=RuntimeError("boom"))
        )
        new_session = SimpleNamespace(context_file=tmp_path / "new" / "context.jsonl")
        new_session.context_file.parent.mkdir(parents=True)

        carried = await _carry_context_to_session(soul, new_session)

        assert carried is False
        assert not new_session.context_file.exists()
