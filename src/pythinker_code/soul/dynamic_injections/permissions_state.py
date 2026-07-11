from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from pythinker_core.message import Message

from pythinker_code.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider
from pythinker_code.soul.permission import PermissionProfile, permission_profile_for_runtime

if TYPE_CHECKING:
    from pythinker_code.soul.pythinkersoul import PythinkerSoul

_INJECTION_TYPE = "permissions_state"


class PermissionsInjectionProvider(DynamicInjectionProvider):
    """Render the live permission posture into the prompt.

    Enforcement is rich (profiles, safe mode, yolo/auto flags, session
    approvals, shell-command classification) but was invisible to the model,
    which discovered policy through denied tool calls. Re-injects only when
    the posture fingerprint changes (covers /yolo, /auto, /trust toggles and
    new session approvals), after compaction, and after auto-mode toggles.
    Root-only: subagent overlays already document their profile constraints.
    """

    def __init__(self) -> None:
        self._last_fingerprint: tuple[object, ...] | None = None
        self._prepared_fingerprint: tuple[object, ...] | None = None

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        injection, fingerprint = self._candidate(soul)
        if injection is None:
            return []
        self._last_fingerprint = fingerprint
        return [injection]

    async def prepare_injections(
        self,
        history: Sequence[Message],
        soul: PythinkerSoul,
    ) -> list[DynamicInjection]:
        _ = history
        if self._prepared_injections:
            return list(self._prepared_injections)
        injection, fingerprint = self._candidate(soul)
        if injection is None:
            return []
        self._prepared_fingerprint = fingerprint
        self._prepared_injections = (injection,)
        return [injection]

    def _candidate(
        self, soul: PythinkerSoul
    ) -> tuple[DynamicInjection | None, tuple[object, ...] | None]:
        if soul.is_subagent:
            return None, None
        profile = permission_profile_for_runtime(soul.runtime)
        approval = soul.runtime.approval
        approved = tuple(sorted(approval.session_approved_actions()))
        fingerprint = (
            profile.name,
            soul.runtime.config.agent_execution_profile,
            approval.is_yolo(),
            approval.is_auto(),
            approval.is_safe_mode(),
            approved,
        )
        if fingerprint == self._last_fingerprint:
            return None, fingerprint
        return (
            DynamicInjection(
                type=_INJECTION_TYPE,
                content=_render(
                    profile,
                    approval.is_yolo(),
                    approval.is_auto(),
                    approval.is_safe_mode(),
                    approved,
                ),
            ),
            fingerprint,
        )

    def _on_injections_acknowledged(self, injections: Sequence[DynamicInjection]) -> None:
        if injections:
            self._last_fingerprint = self._prepared_fingerprint
        self._prepared_fingerprint = None

    async def on_context_compacted(self) -> None:
        self._last_fingerprint = None
        self._prepared_fingerprint = None
        self._prepared_injections = ()

    async def on_auto_changed(self, enabled: bool) -> None:
        _ = enabled
        self._last_fingerprint = None
        self._prepared_fingerprint = None
        self._prepared_injections = ()


def _render(
    profile: PermissionProfile,
    yolo: bool,
    auto: bool,
    safe_mode: bool,
    approved: tuple[str, ...],
) -> str:
    def onoff(flag: bool) -> str:
        return "on" if flag else "off"

    def allowed(flag: bool) -> str:
        return "allowed" if flag else "denied"

    approved_text = ", ".join(approved) if approved else "none"
    return (
        f"Permissions state: profile '{profile.name}' ({profile.description}). "
        f"Safe mode {onoff(safe_mode)}; yolo {onoff(yolo)}; auto {onoff(auto)}. "
        f"File mutation {allowed(profile.allow_file_mutation)}; shell mutation "
        f"{allowed(profile.allow_shell_mutation)}; network tools "
        f"{allowed(profile.allow_network)}.\n"
        f"Auto-approved without prompting: provably read-only commands "
        f"(ls, cat, grep, git status/log/diff, ...); session-approved actions: "
        f"{approved_text}.\n"
        "Command shaping: the shell gate classifies plain commands only — "
        "command substitution $(...), backticks, and operators glued to words "
        "are rejected as hidden commands. Write plain, separated commands so "
        "the classifier can see them."
    )
