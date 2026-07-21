"""Session-owned toast queues with a compatibility facade."""

from __future__ import annotations

import time
from collections import deque
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Literal

ToastPosition = Literal["left", "right"]

_MINIMUM_DURATION_SECONDS = 1.0
_MAX_BOOTSTRAP_TOASTS = 100


@dataclass(frozen=True, slots=True)
class ToastSnapshot:
    """One immutable toast entry owned by a prompt session."""

    message: str
    position: ToastPosition
    style: str
    topic: str | None
    expires_at: float


class ToastManager:
    """Own, expire, and isolate toast entries for one prompt session."""

    def __init__(self, *, adopt_bootstrap: bool = True) -> None:
        self._queues: dict[ToastPosition, deque[ToastSnapshot]] = {
            "left": deque(),
            "right": deque(),
        }
        self._closed = False
        if adopt_bootstrap:
            self._adopt_bootstrap()

    def add(
        self,
        message: str,
        *,
        duration: float = 5.0,
        topic: str | None = None,
        immediate: bool = False,
        position: ToastPosition = "left",
        style: str = "",
    ) -> None:
        if self._closed:
            return
        entry = ToastSnapshot(
            message=message,
            position=position,
            style=style,
            topic=topic,
            expires_at=time.monotonic() + max(duration, _MINIMUM_DURATION_SECONDS),
        )
        self._add_entry(entry, immediate=immediate)

    def current(self, position: ToastPosition = "left") -> ToastSnapshot | None:
        """Return the current unexpired toast for ``position``."""
        queue = self._queues[position]
        now = time.monotonic()
        while queue and queue[0].expires_at <= now:
            queue.popleft()
        return queue[0] if queue else None

    def snapshots(self, position: ToastPosition) -> tuple[ToastSnapshot, ...]:
        """Return all unexpired entries in display order."""
        self.current(position)
        return tuple(self._queues[position])

    async def aclose(self) -> None:
        """Close this manager without affecting another session's entries."""
        if self._closed:
            return
        self._closed = True
        for queue in self._queues.values():
            queue.clear()

    def _add_entry(self, entry: ToastSnapshot, *, immediate: bool) -> None:
        queue = self._queues[entry.position]
        if entry.topic is not None:
            self._queues[entry.position] = queue = deque(
                existing for existing in queue if existing.topic != entry.topic
            )
        if immediate:
            queue.appendleft(entry)
        else:
            queue.append(entry)

    def _adopt_bootstrap(self) -> None:
        for queue in bootstrap_toast_queues.values():
            while queue:
                self._add_entry(queue.popleft(), immediate=False)


bootstrap_toast_queues: dict[ToastPosition, deque[ToastSnapshot]] = {
    "left": deque(maxlen=_MAX_BOOTSTRAP_TOASTS),
    "right": deque(maxlen=_MAX_BOOTSTRAP_TOASTS),
}

_active_manager: ContextVar[ToastManager | None] = ContextVar(
    "prompt_toast_manager",
    default=None,
)


def bind_toast_manager(manager: ToastManager) -> Token[ToastManager | None]:
    """Bind ``manager`` to the current prompt-session context."""
    return _active_manager.set(manager)


def reset_toast_manager(token: Token[ToastManager | None]) -> None:
    """Restore the toast binding that preceded ``token``."""
    _active_manager.reset(token)


def toast(
    message: str,
    duration: float = 5.0,
    topic: str | None = None,
    immediate: bool = False,
    position: ToastPosition = "left",
    style: str = "",
) -> None:
    """Route a toast to the bound session, or retain it for the next session."""
    manager = _active_manager.get()
    if manager is not None:
        manager.add(
            message,
            duration=duration,
            topic=topic,
            immediate=immediate,
            position=position,
            style=style,
        )
        return

    queue = bootstrap_toast_queues[position]
    if topic is not None:
        retained = (entry for entry in queue if entry.topic != topic)
        bootstrap_toast_queues[position] = queue = deque(
            retained,
            maxlen=_MAX_BOOTSTRAP_TOASTS,
        )
    entry = ToastSnapshot(
        message=message,
        position=position,
        style=style,
        topic=topic,
        expires_at=time.monotonic() + max(duration, _MINIMUM_DURATION_SECONDS),
    )
    if immediate:
        queue.appendleft(entry)
    else:
        queue.append(entry)


def current_toast(position: ToastPosition = "left") -> ToastSnapshot | None:
    """Read the current toast from the active session or bootstrap queue."""
    manager = _active_manager.get()
    if manager is not None:
        return manager.current(position)
    queue = bootstrap_toast_queues[position]
    now = time.monotonic()
    while queue and queue[0].expires_at <= now:
        queue.popleft()
    return queue[0] if queue else None


__all__ = (
    "ToastManager",
    "ToastPosition",
    "ToastSnapshot",
    "bind_toast_manager",
    "bootstrap_toast_queues",
    "current_toast",
    "reset_toast_manager",
    "toast",
)
