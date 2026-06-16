"""Blackbox-inspired motion helpers for the shell TUI."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Literal

from rich.color import Color
from rich.style import Style
from rich.text import Text

from pythinker_code.soul import format_token_count
from pythinker_code.ui.color_utils import blend, parse_hex_color, to_hex_color
from pythinker_code.ui.shell.components.render_utils import cell_width
from pythinker_code.ui.shell.design_system import ShellTone, shell_style
from pythinker_code.ui.shell.glyphs import (
    ACTIVE_MARKER_FRAME_INTERVAL_S,
    ACTIVE_MARKER_FRAMES,
    REDUCED_MOTION_GLYPH,
    SHAPE_FRAME_INTERVAL_S,
    SHAPE_FRAMES,
    SPINNER_FRAME_INTERVAL_S,
    SPINNER_FRAMES,
    TRANSCRIPT_ACTIVE_MARKER,
)
from pythinker_code.ui.terminal_capabilities import color_depth, colors_disabled, motion_disabled
from pythinker_code.ui.theme import get_tui_tokens, tui_rich_style
from pythinker_code.utils.datetime import format_elapsed

_FRAMES = SPINNER_FRAMES
_FRAME_INTERVAL_S = SPINNER_FRAME_INTERVAL_S


def _activity_color_tokens() -> tuple[str, str, str, str]:
    """Return theme-standardized activity colors.

    The active verb uses a coral ramp — muted coral resting (matching the
    robot's antenna accent), sweeping to a soft light-coral spark in dark mode,
    and a contrast-safe deep-coral ramp in light mode. Keep these in theme
    tokens so Rich and prompt_toolkit renderers stay visually aligned.
    """
    tokens = get_tui_tokens()
    return (
        tokens.activity_verb,
        tokens.activity_verb_mid,
        tokens.activity_verb_highlight,
        tokens.activity_spinner,
    )


def verb_spinner_style() -> Style:
    """Theme-standardized style for the active verb spinner word."""
    if colors_disabled():
        return Style()
    base, _mid, _highlight, _spinner = _activity_color_tokens()
    return Style(color=Color.parse(base))


# Dark-theme shimmer-ramp reference constants, sourced directly from the
# `activity_verb*` tokens so they can never drift out of lockstep with the theme.
_SHIMMER_BASE = get_tui_tokens("dark").activity_verb
_SHIMMER_MID = get_tui_tokens("dark").activity_verb_mid
_SHIMMER_HIGHLIGHT = get_tui_tokens("dark").activity_verb_highlight
_SHIMMER_INTERVAL_S = 0.15


def shimmer_spinner_style(elapsed_s: float, *, reduced_motion: bool = False) -> Style:
    """Clean shimmer color for active verb text.

    Reduced motion pins to the base activity color so the word stays calm.
    """
    if colors_disabled():
        return Style()
    base, mid, highlight, _spinner = _activity_color_tokens()
    if reduced_motion or reduced_motion_enabled():
        return Style(color=Color.parse(base))
    palette = (base, mid, highlight, mid)
    idx = int(max(0.0, elapsed_s) / _SHIMMER_INTERVAL_S) % len(palette)
    return Style(color=Color.parse(palette[idx]))


def _wave_colors(
    chars: list[str],
    local_phase: int,
    *,
    rightward: bool,
    base: str,
    mid: str,
    highlight: str,
    smooth: bool = False,
) -> list[str | None]:
    """Per-character colors for a single traveling-wave sweep.

    One bright highlight crosses the label with an asymmetric, slightly wider
    trail behind it — an angled sheen rather than a flat pulse. ``rightward``
    flips both the travel direction and the trailing side so the trail always
    lags behind the head. With ``smooth`` (truecolor terminals), the sheen is a
    continuous cosine-falloff blend from highlight into base
    instead of the discrete three-step ramp.
    """
    n = len(chars)
    if rightward:
        head = local_phase - 2
        trail = (1, -1, -2, -3)
    else:
        head = n + 2 - local_phase
        trail = (-1, 1, 2, 3)
    base_rgb = parse_hex_color(base) if smooth else None
    highlight_rgb = parse_hex_color(highlight) if smooth else None
    blend_smoothly = base_rgb is not None and highlight_rgb is not None
    colors: list[str | None] = []
    for i, char in enumerate(chars):
        if char.isspace():
            colors.append(None)
            continue
        offset = i - head
        if blend_smoothly:
            assert base_rgb is not None and highlight_rgb is not None
            behind = (offset < 0) if rightward else (offset > 0)
            falloff_width = 3.5 if behind else 1.5
            dist = abs(offset)
            if dist <= falloff_width:
                t = 0.5 * (1.0 + math.cos(math.pi * dist / falloff_width))
                colors.append(to_hex_color(blend(highlight_rgb, base_rgb, t)))
            else:
                colors.append(base)
        elif offset == 0:
            colors.append(highlight)
        elif offset in trail:
            colors.append(mid)
        else:
            colors.append(base)
    return colors


def _shimmer_segments(
    label: str, elapsed_s: float, *, reduced_motion: bool
) -> list[tuple[str | None, str]]:
    """Return coalesced ``(hex_color, text)`` shimmer segments.

    This is shared by Rich renderables and prompt_toolkit fragments so every
    active-work label uses the same visual language. The motion is a calm
    bidirectional sheen: a wave sweeps right-to-left, the word settles to its
    base color for a beat, the wave sweeps back left-to-right, settles, repeat.
    On truecolor terminals the sheen is a continuous cosine blend; lower color
    tiers keep the discrete three-step ramp.
    """
    if not label:
        return []
    if colors_disabled():
        return [(None, label)]
    base, mid, highlight, _spinner = _activity_color_tokens()
    if reduced_motion or reduced_motion_enabled():
        return [(base, label)]

    chars = list(label)
    n = len(chars)
    smooth = color_depth() == "truecolor"
    wave_len = n + 6
    settle_len = 4  # calm beat between sweeps so the motion never feels busy
    cycle_len = 2 * (wave_len + settle_len)
    frame = int(max(0.0, elapsed_s) / _SHIMMER_INTERVAL_S) % cycle_len
    if frame < wave_len:
        colors = _wave_colors(
            chars, frame, rightward=False, base=base, mid=mid, highlight=highlight, smooth=smooth
        )
    elif frame < wave_len + settle_len:
        colors = [None if char.isspace() else base for char in chars]
    elif frame < 2 * wave_len + settle_len:
        colors = _wave_colors(
            chars,
            frame - wave_len - settle_len,
            rightward=True,
            base=base,
            mid=mid,
            highlight=highlight,
            smooth=smooth,
        )
    else:
        colors = [None if char.isspace() else base for char in chars]

    segments: list[tuple[str | None, str]] = []
    for char, color in zip(chars, colors, strict=True):
        if segments and segments[-1][0] == color:
            segments[-1] = (color, segments[-1][1] + char)
        else:
            segments.append((color, char))
    return segments


def shimmer_text(label: str, elapsed_s: float, *, reduced_motion: bool = False) -> Text:
    """Return subtle per-character shimmer text for any active work label."""
    rendered = Text()
    for color, text in _shimmer_segments(label, elapsed_s, reduced_motion=reduced_motion):
        if color is None:
            rendered.append(text)
        else:
            rendered.append(text, style=Style(color=Color.parse(color)))
    return rendered


def shimmer_prompt_fragments(
    label: str, elapsed_s: float, *, reduced_motion: bool = False
) -> list[tuple[str, str]]:
    """Return prompt_toolkit fragments using the same shimmer as ``shimmer_text``."""
    return [
        (f"fg:{color}" if color is not None else "", text)
        for color, text in _shimmer_segments(label, elapsed_s, reduced_motion=reduced_motion)
    ]


# Backwards-compatible private name used by older callers/tests.
def _shimmer_label_text(label: str, elapsed_s: float, *, reduced_motion: bool) -> Text:
    return shimmer_text(label, elapsed_s, reduced_motion=reduced_motion)


@dataclass(frozen=True, slots=True)
class ActivitySnapshot:
    label: str
    elapsed_s: float
    tokens: int = 0
    token_rate: int | None = None
    stalled: bool = False
    interrupt_hint: str = ""
    reduced_motion: bool = False
    label_style: Style | None = None
    # "braille" = dotted spinner (default); "shape" = morphing filled shape.
    spinner: Literal["braille", "shape"] = "braille"


def reduced_motion_enabled() -> bool:
    return motion_disabled()


_BLINK_PERIOD_S = 0.8
GUTTER_PULSE_INTERVAL_S = _BLINK_PERIOD_S
STREAM_FPS = 25
STREAM_FRAME_INTERVAL_S = 1 / STREAM_FPS
CARET_BLINK_INTERVAL_S = 0.5
REDUCED_MOTION_REVEAL_HZ = 5
STREAMING_CARET_GLYPH = "\u258d"  # ▍ left five eighths block


def stream_reveal_interval_s() -> float:
    """Frame interval for paced reveal ticks (slower under reduced motion)."""
    if reduced_motion_enabled():
        return 1 / REDUCED_MOTION_REVEAL_HZ
    return STREAM_FRAME_INTERVAL_S


def streaming_caret_visible(now: float | None = None) -> bool:
    """Return whether the streaming caret is in its visible half-cycle."""
    if reduced_motion_enabled():
        return True
    t = time.monotonic() if now is None else now
    return int(t / CARET_BLINK_INTERVAL_S) % 2 == 0


def append_streaming_caret(text: Text, *, now: float | None = None) -> None:
    """Append a fixed-width caret slot so blink does not reflow the preview."""
    if streaming_caret_visible(now):
        text.append(STREAMING_CARET_GLYPH, style=tui_rich_style("muted"))
    else:
        # ponytail: reserve the column; removing the glyph shifts wrapped lines.
        text.append(" ")


def blink_visible(now: float | None = None) -> bool:
    """Shared blink phase: True during the visible half of the 0.8s cycle.

    Reduced motion pins the element visible. Pass *now* when the caller has
    already sampled ``time.monotonic()`` so co-rendered elements blink in phase.
    """
    if reduced_motion_enabled():
        return True
    t = time.monotonic() if now is None else now
    return int(t / _BLINK_PERIOD_S) % 2 == 0


def spinner_frame_at(
    elapsed_s: float,
    *,
    reduced_motion: bool = False,
    frames: tuple[str, ...] = _FRAMES,
    interval_s: float = _FRAME_INTERVAL_S,
) -> str:
    if reduced_motion:
        return REDUCED_MOTION_GLYPH
    index = int(max(0.0, elapsed_s) / interval_s) % len(frames)
    return frames[index]


def active_marker_frame(elapsed_s: float, *, reduced_motion: bool = False) -> str:
    """Return the current braille dotted frame for the active task marker.

    Reduced motion pins to the static dot so the marker stays calm.
    """
    if reduced_motion or reduced_motion_enabled():
        return TRANSCRIPT_ACTIVE_MARKER
    return spinner_frame_at(
        elapsed_s,
        frames=ACTIVE_MARKER_FRAMES,
        interval_s=ACTIVE_MARKER_FRAME_INTERVAL_S,
    )


def _candidate_parts(snapshot: ActivitySnapshot) -> list[str]:
    parts = [format_elapsed(snapshot.elapsed_s)]
    if snapshot.tokens:
        parts.append(f"↓ {format_token_count(snapshot.tokens)} tokens")
    if snapshot.token_rate:
        parts.append(f"{snapshot.token_rate} t/s")
    if snapshot.interrupt_hint:
        hint = "esc" if snapshot.interrupt_hint == "esc to interrupt" else snapshot.interrupt_hint
        parts.append(hint)
    return parts


def _activity_label(label: str) -> str:
    stripped = label.rstrip()
    if stripped.endswith(("…", "...")):
        return stripped
    return f"{stripped}…"


def activity_status_line(snapshot: ActivitySnapshot, *, width: int | None = None) -> Text:
    reduced = snapshot.reduced_motion or reduced_motion_enabled()
    thinking_style = tui_rich_style("thinking_text")
    if snapshot.stalled:
        glyph_style = shell_style(ShellTone.WARNING)
    elif snapshot.spinner == "shape":
        # Composing / Thinking: neutral muted grey, not the bright coral verb accent.
        glyph_style = thinking_style
    else:
        # The dotted braille spinner is a marker; keep it platinum while the verb shimmers.
        if colors_disabled():
            glyph_style = Style()
        else:
            _base, _mid, _highlight, spinner = _activity_color_tokens()
            glyph_style = Style(color=Color.parse(spinner))
    if snapshot.label_style is not None:
        label_style = snapshot.label_style
    elif snapshot.spinner == "shape":
        label_style = thinking_style
    else:
        label_style = shimmer_spinner_style(snapshot.elapsed_s, reduced_motion=reduced)
    if snapshot.label.lower() == "thinking":
        label_style += Style(italic=True)
    if snapshot.spinner == "shape":
        frames, interval_s = SHAPE_FRAMES, SHAPE_FRAME_INTERVAL_S
    else:
        frames, interval_s = _FRAMES, _FRAME_INTERVAL_S
    text = Text(
        spinner_frame_at(
            snapshot.elapsed_s, reduced_motion=reduced, frames=frames, interval_s=interval_s
        ),
        style=glyph_style,
    )
    text.append(" ")
    label_text = _activity_label(snapshot.label)
    if snapshot.label_style is None and snapshot.spinner != "shape":
        shimmered = _shimmer_label_text(label_text, snapshot.elapsed_s, reduced_motion=reduced)
        if snapshot.label.lower() == "thinking":
            shimmered.stylize(Style(italic=True))
        text.append_text(shimmered)
    else:
        text.append(label_text, style=label_style)

    parts = _candidate_parts(snapshot)
    if width is not None:
        base_width = cell_width(text.plain)
        kept: list[str] = []
        for part in parts:
            candidate = ", ".join([*kept, part])
            if base_width + 3 + cell_width(candidate) <= width:
                kept.append(part)
        parts = kept
    if parts:
        # Parenthesized metadata matches the pinned-todo activity line so the
        # working and todo headers share one visual language.
        text.append(f" ({', '.join(parts)})", style=thinking_style)
    return text
