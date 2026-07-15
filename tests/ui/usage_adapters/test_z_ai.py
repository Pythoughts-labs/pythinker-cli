from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from pydantic import SecretStr

from pythinker_code.auth.oauth import OAuthManager
from pythinker_code.config import LLMProvider
from pythinker_code.ui.shell.usage import (
    _enrich_with_ratelimit_fallback,
    _gather_reports,
    _select_providers,
)
from pythinker_code.ui.shell.usage_adapters import ADAPTERS
from pythinker_code.ui.shell.usage_adapters.base import UsageAdapter
from pythinker_code.ui.shell.usage_adapters.z_ai import ZaiUsageAdapter
from pythinker_code.usage_ratelimit_cache import get_cache


def _provider(base_url: str) -> LLMProvider:
    return LLMProvider(
        type="openai_legacy",
        base_url=base_url,
        api_key=SecretStr("route-key"),
    )


def test_zai_usage_adapters_are_distinct_and_route_labeled() -> None:
    coding = ADAPTERS["z-ai-coding"]
    api = ADAPTERS["z-ai-api"]

    assert isinstance(coding, ZaiUsageAdapter)
    assert isinstance(api, ZaiUsageAdapter)
    assert coding is not api
    assert coding.platform_id == "z-ai-coding"
    assert coding.provider_label == "Z.AI Coding Plan"
    assert api.platform_id == "z-ai-api"
    assert api.provider_label == "Z.AI API"


async def test_zai_usage_adapter_is_notes_only_and_never_reads_credentials() -> None:
    class _CredentialTrap:
        @property
        def api_key(self) -> None:
            raise AssertionError("Z.AI usage adapter read a route credential")

    adapters = (
        ZaiUsageAdapter("z-ai-coding", "Z.AI Coding Plan"),
        ZaiUsageAdapter("z-ai-api", "Z.AI API"),
    )
    for adapter in adapters:
        report = await adapter.fetch(
            cast(LLMProvider, _CredentialTrap()),
            cast(OAuthManager, MagicMock()),
        )
        assert report.provider_label == adapter.provider_label
        assert report.summary is None
        assert report.limits == []
        assert report.notes


def test_default_usage_selection_is_scoped_to_exact_zai_provider_key() -> None:
    providers = {
        "managed:z-ai-coding": _provider("https://api.z.ai/api/coding/paas/v4"),
        "managed:z-ai-api": _provider("https://api.z.ai/api/paas/v4"),
    }

    selected = _select_providers(
        providers,
        registered_platform_ids=set(ADAPTERS),
        filter_provider_key="managed:z-ai-coding",
    )

    assert selected == [("z-ai-coding", providers["managed:z-ai-coding"])]


async def test_usage_all_reports_both_zai_routes_with_distinct_labels() -> None:
    providers = {
        "managed:z-ai-coding": _provider("https://api.z.ai/api/coding/paas/v4"),
        "managed:z-ai-api": _provider("https://api.z.ai/api/paas/v4"),
    }
    selected = _select_providers(
        providers,
        registered_platform_ids=set(ADAPTERS),
        filter_provider_key=None,
    )
    pairs = [(ADAPTERS[platform_id], provider) for platform_id, provider in selected]

    reports = await _gather_reports(pairs, cast(OAuthManager, MagicMock()))

    assert {report.provider_label for report in reports} == {
        "Z.AI Coding Plan",
        "Z.AI API",
    }


async def test_zai_rate_limit_fallback_never_crosses_route_keys() -> None:
    cache = get_cache()
    cache.clear()
    cache.record(
        "managed:z-ai-coding",
        {
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-remaining-requests": "75",
        },
    )
    providers = {
        "managed:z-ai-coding": _provider("https://api.z.ai/api/coding/paas/v4"),
        "managed:z-ai-api": _provider("https://api.z.ai/api/paas/v4"),
    }
    selected = _select_providers(
        providers,
        registered_platform_ids=set(ADAPTERS),
        filter_provider_key=None,
    )
    pairs: list[tuple[UsageAdapter, LLMProvider]] = [
        (ADAPTERS[platform_id], provider) for platform_id, provider in selected
    ]

    reports = await _gather_reports(pairs, cast(OAuthManager, MagicMock()))
    enriched = _enrich_with_ratelimit_fallback(reports, selected)
    by_label = {report.provider_label: report for report in enriched}

    coding = next(report for report in enriched if report.provider_label.startswith("Z.AI Coding"))
    api = by_label["Z.AI API"]
    assert coding.summary is not None
    assert coding.summary.used == 25
    assert api.summary is None
    assert api.limits == []
    cache.clear()
