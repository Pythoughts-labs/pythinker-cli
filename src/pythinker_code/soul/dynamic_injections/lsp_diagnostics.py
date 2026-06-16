from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from pythinker_core.message import Message

from pythinker_code.lsp.diagnostics import render_diagnostics_block
from pythinker_code.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider

if TYPE_CHECKING:
    from pythinker_code.soul.agent import Runtime
    from pythinker_code.soul.pythinkersoul import PythinkerSoul

_INJECTION_TYPE = "lsp_diagnostics"
_REARM_KEY = "lsp_diagnostics"


class LspDiagnosticsInjectionProvider(DynamicInjectionProvider):
    """Inject bounded LSP diagnostics when language servers report issues."""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime
        self._armed = True

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        del history, soul
        lsp = self._runtime.lsp
        if lsp is None or not lsp.is_connected() or not self._armed:
            return []
        groups = lsp.diagnostics.check_for_diagnostics()
        if not groups:
            return []
        self._armed = False
        return [
            DynamicInjection(
                type=_INJECTION_TYPE,
                content=render_diagnostics_block(groups),
            )
        ]

    async def on_context_compacted(self) -> None:
        self._armed = True

    def rearm(self, key: str) -> bool:
        if key != _REARM_KEY:
            return False
        self._armed = True
        return True
