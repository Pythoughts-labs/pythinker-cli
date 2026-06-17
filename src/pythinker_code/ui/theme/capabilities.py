"""Terminal capability detection for theme resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass

from pythinker_code.ui.terminal_capabilities import color_depth, colors_disabled


@dataclass(frozen=True, slots=True)
class TerminalCapabilities:
    color_enabled: bool
    truecolor: bool
    color_256: bool
    dumb: bool


def get_terminal_capabilities() -> TerminalCapabilities:
    depth = color_depth()
    term = (os.environ.get("TERM") or "").strip().lower()
    return TerminalCapabilities(
        color_enabled=not colors_disabled(),
        truecolor=depth == "truecolor",
        color_256=depth in ("256", "truecolor"),
        dumb=term == "dumb",
    )
