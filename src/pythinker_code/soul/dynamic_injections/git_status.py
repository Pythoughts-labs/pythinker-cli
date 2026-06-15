from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from pythinker_core.message import Message

from pythinker_code.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider
from pythinker_code.subagents.git_context import collect_git_context

if TYPE_CHECKING:
    from pythinker_code.soul.pythinkersoul import PythinkerSoul

_INJECTION_TYPE = "git_status"
_STALE_PREAMBLE = (
    "Git snapshot (point-in-time, may be stale by the time you act — re-run "
    "`git status` / `git diff` before relying on it for irreversible decisions):\n"
)


class GitStatusInjectionProvider(DynamicInjectionProvider):
    """Root-only injection of a bounded git working-tree snapshot.

    Reuses :func:`collect_git_context` so collection stays consistent with
    read-oriented subagents. Re-injects when the snapshot changes or after compaction.
    """

    def __init__(self) -> None:
        self._last_fingerprint: str | None = None

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        if soul.is_subagent or not soul.runtime.config.git_status_injection:
            return []

        raw = await collect_git_context(soul.runtime.work_dir)
        if not raw:
            return []

        if raw == self._last_fingerprint:
            return []
        self._last_fingerprint = raw

        inner = raw
        prefix = "<git-context>\n"
        suffix = "\n</git-context>"
        if inner.startswith(prefix) and inner.endswith(suffix):
            inner = inner[len(prefix) : -len(suffix)]

        return [
            DynamicInjection(
                type=_INJECTION_TYPE,
                content=_STALE_PREAMBLE + inner,
            )
        ]

    async def on_context_compacted(self) -> None:
        self._last_fingerprint = None
