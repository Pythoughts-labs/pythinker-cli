"""Lossless live-view rendering for media and future content parts."""

from __future__ import annotations

from collections.abc import Iterable

import pytest
from pythinker_core.message import ContentPart, ThinkPart
from rich.console import Console, Group, RenderableType
from rich.style import Style

from pythinker_code.ui.shell.glyphs import TRANSCRIPT_ASSISTANT_MARKER
from pythinker_code.ui.shell.visualize import _live_view as live_view_module
from pythinker_code.ui.shell.visualize import _LiveView
from pythinker_code.ui.theme import tui_rich_style
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


def _segment_styles_for_text(renderable: RenderableType, text: str) -> list[Style]:
    console = Console(record=True, width=100, color_system=None)
    styles: list[Style] = []
    for segment in console.render(renderable):
        if segment.control is not None or text not in segment.text:
            continue
        segment_style = segment.style
        if isinstance(segment_style, str):
            styles.append(Style.parse(segment_style))
        elif segment_style is not None:
            styles.append(segment_style)
    if not styles:
        raise AssertionError(f"Text {text!r} not found in rendered segments")
    return styles


def test_live_view_dispatch_preserves_reasoning_summary_boundaries_and_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emitted = _capture_scrollback(monkeypatch)
    view = _LiveView(StatusUpdate(context_tokens=1000), show_thinking_stream=True)

    view.dispatch_wire_message(ThinkPart(think="**Planning summary**", summary_index=0))
    view.dispatch_wire_message(ThinkPart(think="**Checking summary**", summary_index=1))
    view.flush_content()

    assert len(emitted) == 1
    output = _render(emitted)
    lines = [line for line in output.splitlines() if line.strip()]
    assert output.count(TRANSCRIPT_ASSISTANT_MARKER) == 1
    assert len(lines) == 2
    assert "Planning summary" in lines[0]
    assert "Checking summary" in lines[1]
    assert "**" not in output

    thinking_style = tui_rich_style("thinking_text")
    for text in ("Planning summary", "Checking summary"):
        styles = _segment_styles_for_text(emitted[0], text)
        assert all(style.color == thinking_style.color for style in styles)
        assert all(style.italic for style in styles)


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
