"""Session-owned adapter for prompt clipboard capabilities and paste operations."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token

from prompt_toolkit.clipboard import Clipboard
from prompt_toolkit.clipboard.pyperclip import PyperclipClipboard

from pythinker_code.utils.clipboard import (
    ClipboardResult,
)
from pythinker_code.utils.clipboard import (
    grab_media_from_clipboard as utility_grab_media_from_clipboard,
)
from pythinker_code.utils.clipboard import (
    is_clipboard_available as utility_is_clipboard_available,
)
from pythinker_code.utils.clipboard import (
    is_media_clipboard_available as utility_is_media_clipboard_available,
)
from pythinker_code.utils.logging import logger


class ClipboardAdapter:
    """Wrap clipboard probing and paste operations for one prompt session."""

    def __init__(
        self,
        *,
        text_capability: Callable[[], bool] = utility_is_clipboard_available,
        media_capability: Callable[[], bool] = utility_is_media_clipboard_available,
        media_grabber: Callable[[], ClipboardResult | None] = utility_grab_media_from_clipboard,
        text_clipboard_factory: Callable[[], Clipboard] = PyperclipClipboard,
    ) -> None:
        self._text_capability = text_capability
        self._media_capability = media_capability
        self._media_grabber = media_grabber
        self._text_clipboard_factory = text_clipboard_factory

    def is_text_available(self) -> bool:
        return self._text_capability()

    def is_media_available(self) -> bool:
        return self._media_capability()

    def create_text_clipboard(self, *, available: bool | None = None) -> Clipboard | None:
        """Create the prompt-toolkit clipboard only after a successful probe."""
        if not (self.is_text_available() if available is None else available):
            return None
        return self._text_clipboard_factory()

    def paste_text(self, clipboard: Clipboard) -> str | None:
        """Return pasted text, preserving the prompt's silent failure behavior."""
        try:
            data = clipboard.get_data()
        except Exception as exc:
            logger.debug("Clipboard text read failed: error={!r}", exc)
            return None
        return data.text

    def paste_media(self) -> ClipboardResult | None:
        """Read images and file paths from the platform clipboard once."""
        return self._media_grabber()

    async def aclose(self) -> None:
        """Close hook for uniform prompt lifecycle ownership."""


_active_adapter: ContextVar[ClipboardAdapter | None] = ContextVar(
    "prompt_clipboard_adapter",
    default=None,
)


def bind_clipboard_adapter(adapter: ClipboardAdapter) -> Token[ClipboardAdapter | None]:
    """Bind ``adapter`` to the current prompt-session context."""
    return _active_adapter.set(adapter)


def reset_clipboard_adapter(token: Token[ClipboardAdapter | None]) -> None:
    """Restore the clipboard binding that preceded ``token``."""
    _active_adapter.reset(token)


def is_clipboard_available() -> bool:
    adapter = _active_adapter.get()
    return adapter.is_text_available() if adapter is not None else utility_is_clipboard_available()


def is_media_clipboard_available() -> bool:
    adapter = _active_adapter.get()
    return (
        adapter.is_media_available()
        if adapter is not None
        else utility_is_media_clipboard_available()
    )


def grab_media_from_clipboard() -> ClipboardResult | None:
    adapter = _active_adapter.get()
    return adapter.paste_media() if adapter is not None else utility_grab_media_from_clipboard()


__all__ = (
    "ClipboardAdapter",
    "bind_clipboard_adapter",
    "grab_media_from_clipboard",
    "is_clipboard_available",
    "is_media_clipboard_available",
    "reset_clipboard_adapter",
)
