"""Lossless live-view rendering for media and future content parts."""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from pythinker_core.message import ContentPart
from rich.console import Console, Group, RenderableType

from pythinker_code.ui.shell.visualize import _live_view as live_view_module
from pythinker_code.ui.shell.visualize import _LiveView
from pythinker_code.wire.types import (
    AudioURLPart,
    ImageURLPart,
    StatusUpdate,
    TextPart,
    VideoURLPart,
)


class FuturePayloadPart(ContentPart):
    type: str = "future_payload"
    payload: str


def _render(renderables: Iterable[RenderableType], *, color: bool = False) -> str:
    console = Console(
        width=100,
        record=True,
        highlight=False,
        color_system="standard" if color else None,
    )
    console.print(Group(*renderables))
    return console.export_text()


def _capture_scrollback(
    monkeypatch: pytest.MonkeyPatch,
) -> list[RenderableType]:
    emitted: list[RenderableType] = []
    monkeypatch.setattr(
        live_view_module,
        "emit_scrollback_block",
        lambda _console, renderable: emitted.append(renderable),
    )
    return emitted


def test_text_media_text_flushes_at_stable_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted = _capture_scrollback(monkeypatch)
    view = _LiveView(StatusUpdate(context_tokens=1000))

    view.append_content(TextPart(text="before"))
    view.append_content(
        ImageURLPart(image_url=ImageURLPart.ImageURL(url="data:image/png;base64,SECRET_IMAGE"))
    )
    view.append_content(TextPart(text="after"))
    view.flush_content()

    assert len(emitted) == 3
    assert "before" in _render([emitted[0]])
    assert "[image]" in _render([emitted[1]])
    assert "after" in _render([emitted[2]])
    output = _render(emitted)
    assert output.index("before") < output.index("[image]") < output.index("after")
    assert "data:image" not in output
    assert "SECRET_IMAGE" not in output


@pytest.mark.parametrize("ascii_mode", [False, True])
def test_all_media_labels_survive_no_color_and_ascii_modes(
    monkeypatch: pytest.MonkeyPatch,
    ascii_mode: bool,
) -> None:
    emitted = _capture_scrollback(monkeypatch)
    if ascii_mode:
        monkeypatch.setenv("PYTHINKER_ASCII_UI", "1")
    view = _LiveView(StatusUpdate(context_tokens=1000))

    view.append_content(
        ImageURLPart(image_url=ImageURLPart.ImageURL(url="https://media.invalid/image-secret"))
    )
    view.append_content(
        AudioURLPart(
            audio_url=AudioURLPart.AudioURL(
                url="data:audio/aac;base64,SECRET_AUDIO",
                id="clip-7",
            )
        )
    )
    view.append_content(
        VideoURLPart(video_url=VideoURLPart.VideoURL(url="data:video/mp4;base64,SECRET_VIDEO"))
    )

    output = _render(emitted)
    assert "[image]" in output
    assert "[audio:clip-7]" in output
    assert "[video]" in output
    assert "media.invalid" not in output
    assert "SECRET_AUDIO" not in output
    assert "SECRET_VIDEO" not in output


def test_unknown_content_uses_muted_label_and_logs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted = _capture_scrollback(monkeypatch)
    debug_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def capture_debug(*args: object, **kwargs: object) -> None:
        debug_calls.append((args, kwargs))

    monkeypatch.setattr(live_view_module.logger, "debug", capture_debug)
    view = _LiveView(StatusUpdate(context_tokens=1000))

    view.append_content(FuturePayloadPart(payload="SECRET_FUTURE_PAYLOAD"))
    view.append_content(FuturePayloadPart(payload="ANOTHER_SECRET_PAYLOAD"))

    output = _render(emitted, color=True)
    assert output.count("[future_payload]") == 2
    assert "SECRET_FUTURE_PAYLOAD" not in output
    assert "ANOTHER_SECRET_PAYLOAD" not in output
    assert len(debug_calls) == 1
    assert debug_calls[0][1]["part_type"] == "future_payload"
