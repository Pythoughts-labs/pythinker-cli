"""Atomic frame delivery for the interactive prompt renderer.

prompt_toolkit buffers each redraw and emits it in a single ``Output.flush()``.
Without bracketing, the terminal may paint while a frame is still arriving,
which shows as flicker during fast streaming redraws. DEC private mode 2026
("synchronized update") tells supporting terminals (iTerm2 3.5+, Ghostty,
Kitty, WezTerm, Alacritty, VS Code, Windows Terminal) to apply the whole
bracketed write atomically; terminals without the mode ignore the marks.

The patch is installed on the *instance* of the session-shared output, so
every consumer — renderer frames, ``patch_stdout`` scrollback prints, and the
erase/redraw around them — delivers atomically. prompt_toolkit is pinned
(``==3.0.52``); the ``_buffer`` access is guarded so an internals change
degrades to a no-op rather than a crash.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from prompt_toolkit.output import Output

BEGIN_SYNCHRONIZED_UPDATE = "\x1b[?2026h"
END_SYNCHRONIZED_UPDATE = "\x1b[?2026l"

_INSTALLED_MARKER = "_pythinker_synchronized_flush"


def install_synchronized_output(output: Output) -> bool:
    """Bracket every flushed frame of *output* in synchronized-update marks.

    Returns ``True`` when installed (or already installed). Outputs without
    the vt100 list buffer (legacy ``Win32Output``, dummy outputs) are left
    untouched. Note that on VT-capable Windows consoles prompt_toolkit uses
    ``Windows10_Output``, which delegates ``_buffer`` to its inner vt100
    output — so those ARE patched, same as Unix. Safe either way: Windows
    Terminal expires an unmatched begin-mark after 100ms, xterm.js after 5s.
    """
    if getattr(output, _INSTALLED_MARKER, False):
        return True
    if not isinstance(getattr(output, "_buffer", None), list):
        return False
    original_flush = output.flush

    def _synchronized_flush() -> None:
        buffer = getattr(output, "_buffer", None)
        if isinstance(buffer, list) and buffer:
            frame = cast("list[str]", buffer)
            frame.insert(0, BEGIN_SYNCHRONIZED_UPDATE)
            frame.append(END_SYNCHRONIZED_UPDATE)
        original_flush()

    output.flush = _synchronized_flush  # type: ignore[method-assign]
    setattr(output, _INSTALLED_MARKER, True)
    return True
