from __future__ import annotations

from typing import TYPE_CHECKING

from pythinker_code.ui.shell.usage_adapters.base import UsageReport

if TYPE_CHECKING:
    from pythinker_code.auth.oauth import OAuthManager
    from pythinker_code.config import LLMProvider


class ZaiUsageAdapter:
    requires_admin_key = False

    def __init__(self, platform_id: str, provider_label: str) -> None:
        self.platform_id = platform_id
        self.provider_label = provider_label

    async def fetch(
        self,
        provider: LLMProvider,
        oauth_mgr: OAuthManager,
    ) -> UsageReport:
        del provider, oauth_mgr
        return UsageReport(
            provider_label=self.provider_label,
            summary=None,
            limits=[],
            notes=[
                "Z.AI does not expose a documented route-wide usage endpoint; "
                "live rate-limit headers are shown after a chat request when available."
            ],
        )
