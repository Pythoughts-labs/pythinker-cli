"""uxsteer-2: non-blocking Suggestion affordance."""

from __future__ import annotations

from typing import Any

from pythinker_code.tools.suggest import Suggest
from pythinker_code.wire.types import Suggestion


class _Wire:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send(self, event: Any) -> None:
        self.sent.append(event)


async def test_suggest_posts_non_blocking_suggestion(monkeypatch) -> None:
    wire = _Wire()
    monkeypatch.setattr("pythinker_code.tools.suggest.wire_send", wire.send)

    result = await Suggest()(Suggest.params(label="Review my changes", prefill="/review"))

    # Returns immediately (non-blocking) with no model-facing output.
    assert not result.is_error
    assert result.output == ""
    # Emitted exactly one Suggestion carrying the label + prefill.
    assert len(wire.sent) == 1
    event = wire.sent[0]
    assert isinstance(event, Suggestion)
    assert event.label == "Review my changes"
    assert event.prefill == "/review"


async def test_suggest_defaults_blank_prefill_and_category(monkeypatch) -> None:
    wire = _Wire()
    monkeypatch.setattr("pythinker_code.tools.suggest.wire_send", wire.send)

    await Suggest()(Suggest.params(label="Run the tests"))

    event = wire.sent[0]
    assert event.prefill == ""
    assert event.category == ""


def test_suggestion_block_renders_label_and_prefill() -> None:
    from pythinker_code.ui.shell.visualize._blocks import _SuggestionBlock

    block = _SuggestionBlock(Suggestion(label="Review my changes", prefill="/review"))
    rendered = block.compose()
    assert rendered is not None


def test_accept_staged_suggestion_prefill_inserts_text() -> None:
    from unittest.mock import MagicMock

    from pythinker_code.ui.shell.prompt import CustomPromptSession

    session = CustomPromptSession.__new__(CustomPromptSession)
    buffer = MagicMock()
    buffer.text = ""
    session._session = MagicMock(default_buffer=buffer)
    session.stage_suggestion_prefill("/review")

    assert session.accept_staged_suggestion_prefill() is True
    buffer.insert_text.assert_called_once_with("/review")
    assert session.accept_staged_suggestion_prefill() is False
