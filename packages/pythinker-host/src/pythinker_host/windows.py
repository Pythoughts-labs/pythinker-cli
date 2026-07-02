from __future__ import annotations

import subprocess

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def windows_console_detach_flags(*, new_process_group: bool = True) -> int:
    """Build Windows ``creationflags`` that keep a spawned child off the console.

    ``CREATE_NO_WINDOW`` gives the child its own hidden console: without it, a
    child that touches the Win32 console API (``cls``, ``SetConsoleMode``, ...)
    bypasses redirected stdio and can blank the parent TUI until the terminal is
    restarted. ``CREATE_NEW_PROCESS_GROUP`` is included by default so existing
    ``kill()``/``taskkill`` semantics against the child are unaffected; pass
    ``new_process_group=False`` for callers that only need console detachment.

    Centralized here so the fallback constants (used when the real Win32
    constants aren't defined, e.g. off-Windows) cannot drift between the
    several process-spawn sites that need them.
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)
    if new_process_group:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", _CREATE_NEW_PROCESS_GROUP)
    return flags
