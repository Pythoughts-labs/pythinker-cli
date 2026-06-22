"""Terminal capability helpers for portable shell UI rendering.

The shell UI has to work in modern terminals, PowerShell, CI logs, SSH panes,
and deliberately minimal terminals. Keep environment-based decisions here so
color, glyph, and motion fallbacks stay consistent across Rich and
prompt_toolkit renderers.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Literal, TextIO

_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "always"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "never"})

type ColorDepth = Literal["none", "16", "256", "truecolor"]


def _env(environ: Mapping[str, str] | None = None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _clean(value: str | None) -> str:
    return "" if value is None else value.strip().lower()


def env_flag(name: str, *, environ: Mapping[str, str] | None = None) -> bool:
    """Return true when *name* is set to a conventional truthy value."""
    return _clean(_env(environ).get(name)) in _TRUE_VALUES


def colors_disabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether terminal color should be suppressed.

    Follows the widely-used CLI conventions from clig.dev and no-color.org:
    ``NO_COLOR`` wins when non-empty, ``TERM=dumb`` means no formatting, and
    ``CLICOLOR=0`` disables color for tools that support the BSD convention.
    ``PYTHINKER_NO_COLOR`` is the app-specific override for users who want one.
    """
    env = _env(environ)
    if env.get("NO_COLOR"):
        return True
    if env_flag("PYTHINKER_NO_COLOR", environ=env):
        return True
    if _clean(env.get("TERM")) == "dumb":
        return True
    return _clean(env.get("CLICOLOR")) == "0"


def color_depth(environ: Mapping[str, str] | None = None) -> ColorDepth:
    """Classify the terminal's color support into three usable tiers.

    Detection order: an explicit ``FORCE_COLOR`` level
    wins, then ``COLORTERM`` truecolor advertising, then the Windows Terminal
    promotion (``WT_SESSION`` implies 24-bit support even when ``TERM`` is
    conservative), then the VS Code-family promotion (``TERM_PROGRAM=vscode``),
    then ``TERM`` itself. ``"none"`` mirrors
    :func:`colors_disabled`. Rich does its own downgrade for printing; this
    helper exists for UI decisions Rich can't make for us (e.g. skipping
    background tints that quantize badly on 16-color terminals).

    Terminals advertise color support inconsistently (many never set
    ``COLORTERM``, and it is not forwarded through ``sudo``/SSH/tmux), so
    hard-coding known 24-bit terminals is the standard workaround.
    """
    env = _env(environ)
    if colors_disabled(env):
        return "none"
    force = _clean(env.get("FORCE_COLOR"))
    if force == "3":
        return "truecolor"
    if force == "2":
        return "256"
    if force == "1":
        return "16"
    if _clean(env.get("COLORTERM")) in {"truecolor", "24bit"}:
        return "truecolor"
    if env.get("WT_SESSION"):
        return "truecolor"
    # VS Code's integrated terminal — and forks built on it, which keep
    # TERM_PROGRAM=vscode — is xterm.js-based with 24-bit color, but some builds
    # ship without COLORTERM. Promote it like Windows Terminal above so diff
    # tints and other backgrounds don't fall back to the 16-color path. Comes
    # after FORCE_COLOR so an explicit downgrade is still honored.
    if _clean(env.get("TERM_PROGRAM")) == "vscode":
        return "truecolor"
    term = _clean(env.get("TERM"))
    if "truecolor" in term or "direct" in term:
        return "truecolor"
    if "256color" in term:
        return "256"
    return "16"


def ascii_glyphs_enabled(
    environ: Mapping[str, str] | None = None, stdout: TextIO | None = None
) -> bool:
    """Return whether the TUI should use ASCII-only glyphs.

    Modern Windows Terminal and PowerShell are Unicode-capable, so Windows alone
    is not a reason to degrade. We fall back for explicit user requests,
    ``TERM=dumb``, or non-UTF stdout encodings such as legacy Windows code pages.
    """
    env = _env(environ)
    stdout = sys.stdout if stdout is None else stdout
    mode = _clean(env.get("PYTHINKER_TUI_GLYPHS"))
    if mode in {"ascii", "safe"}:
        return True
    if mode in {"unicode", "rich"}:
        return False
    if env_flag("PYTHINKER_ASCII_UI", environ=env) or env_flag(
        "PYTHINKER_SAFE_GLYPHS", environ=env
    ):
        return True
    if _clean(env.get("TERM")) == "dumb":
        return True

    encoding = _clean(getattr(stdout, "encoding", None))
    return bool(encoding and "utf" not in encoding and "65001" not in encoding)


def synchronized_output_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether redraws should use DEC mode 2026 synchronized updates.

    Supporting terminals apply a bracketed frame atomically, which removes
    streaming flicker; terminals without the mode ignore the marks. ``TERM=dumb``
    may echo unknown escapes raw, so it opts out, as does the explicit
    ``PYTHINKER_NO_SYNC_OUTPUT`` kill switch.
    """
    env = _env(environ)
    if env_flag("PYTHINKER_NO_SYNC_OUTPUT", environ=env):
        return False
    return _clean(env.get("TERM")) != "dumb"


def motion_disabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether animated terminal affordances should collapse to static."""
    env = _env(environ)
    if _clean(env.get("TERM")) == "dumb":
        return True
    return any(
        env_flag(name, environ=env)
        for name in (
            "PYTHINKER_REDUCED_MOTION",
            "PYTHINKER_NO_ANIMATION",
            "PYTHINKER_STATIC_OUTPUT",
        )
    )
