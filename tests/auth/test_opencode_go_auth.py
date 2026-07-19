from __future__ import annotations

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from pydantic import SecretStr
from yarl import URL

from pythinker_code.config import Config, LLMModel, LLMProvider


def _request_info(url: str) -> aiohttp.RequestInfo:
    return aiohttp.RequestInfo(
        url=URL(url),
        method="GET",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=URL(url),
    )


def test_opencode_go_model_catalog_contains_all_current_models():
    from pythinker_code.auth.opencode_go import OPENCODE_GO_MODELS

    aliases = {model.alias for model in OPENCODE_GO_MODELS}
    assert aliases == {
        "opencode-go/glm-5",
        "opencode-go/glm-5.1",
        "opencode-go/kimi-k2.5",
        "opencode-go/kimi-k2.6",
        "opencode-go/deepseek-v4-pro",
        "opencode-go/deepseek-v4-flash",
        "opencode-go/mimo-v2-pro",
        "opencode-go/mimo-v2-omni",
        "opencode-go/mimo-v2.5-pro",
        "opencode-go/mimo-v2.5",
        "opencode-go/qwen3.5-plus",
        "opencode-go/qwen3.6-plus",
        "opencode-go/qwen3.7-max",
        "opencode-go/minimax-m2.5",
        "opencode-go/minimax-m2.7",
    }

    # Anthropic-shaped models (models.dev @ai-sdk/anthropic): both MiniMax and
    # all three Qwen models. Everything else is OpenAI-compatible.
    anthropic_ids = {
        m.model_id for m in OPENCODE_GO_MODELS if m.provider_key == "managed:opencode-go-anthropic"
    }
    assert anthropic_ids == {
        "minimax-m2.5",
        "minimax-m2.7",
        "qwen3.5-plus",
        "qwen3.6-plus",
        "qwen3.7-max",
    }
    assert all(
        m.provider_key == "managed:opencode-go-openai"
        for m in OPENCODE_GO_MODELS
        if m.model_id not in anthropic_ids
    )


def test_opencode_go_env_key_precedence(monkeypatch):
    from pythinker_code.auth.opencode_go import get_opencode_go_api_key_from_env

    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)

    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "zen-key")
    assert get_opencode_go_api_key_from_env() == "zen-key"

    monkeypatch.setenv("OPENCODE_API_KEY", "api-key")
    assert get_opencode_go_api_key_from_env() == "api-key"

    monkeypatch.setenv("OPENCODE_GO_API_KEY", "go-key")
    assert get_opencode_go_api_key_from_env() == "go-key"


def test_apply_opencode_go_config_writes_two_providers_and_default():
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_ANTHROPIC_BASE_URL,
        OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
        OPENCODE_GO_BASE_URL,
        OPENCODE_GO_OPENAI_PROVIDER_KEY,
        _apply_opencode_go_config,
    )

    config = Config(is_from_default_location=True)

    _apply_opencode_go_config(config, SecretStr("ocgo-test"))

    assert set(config.providers) == {
        OPENCODE_GO_OPENAI_PROVIDER_KEY,
        OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
    }
    openai_provider = config.providers[OPENCODE_GO_OPENAI_PROVIDER_KEY]
    anthropic_provider = config.providers[OPENCODE_GO_ANTHROPIC_PROVIDER_KEY]
    assert openai_provider.type == "openai_legacy"
    assert anthropic_provider.type == "anthropic"
    assert openai_provider.base_url == OPENCODE_GO_BASE_URL
    # Anthropic base must omit "/v1" (the SDK appends "/v1/messages").
    assert anthropic_provider.base_url == OPENCODE_GO_ANTHROPIC_BASE_URL
    assert anthropic_provider.base_url == "https://opencode.ai/zen/go"
    assert openai_provider.api_key.get_secret_value() == "ocgo-test"
    assert anthropic_provider.api_key.get_secret_value() == "ocgo-test"
    assert config.models["opencode-go/kimi-k2.6"].provider == OPENCODE_GO_OPENAI_PROVIDER_KEY
    assert config.models["opencode-go/kimi-k2.6"].capabilities is None
    assert config.models["opencode-go/glm-5"].capabilities == {"always_thinking"}
    assert config.models["opencode-go/minimax-m2.7"].provider == OPENCODE_GO_ANTHROPIC_PROVIDER_KEY
    assert config.models["opencode-go/minimax-m2.7"].capabilities == {"always_thinking"}
    assert config.default_model == "opencode-go/kimi-k2.6"


@pytest.mark.asyncio
async def test_login_opencode_go_saves_static_models_when_discovery_fails(monkeypatch, tmp_path):
    from pythinker_code.auth.opencode_go import login_opencode_go_api_key

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)

    async def fake_discover(api_key):
        assert api_key == "ocgo-test"
        raise aiohttp.ClientConnectionError("models unavailable")

    monkeypatch.setattr(
        "pythinker_code.auth.opencode_go._discover_opencode_go_models", fake_discover
    )

    events = [event async for event in login_opencode_go_api_key(config, "ocgo-test")]

    assert [event.type for event in events] == ["info", "success"]
    assert "ocgo-test" not in "\n".join(event.json for event in events)
    assert config.default_model == "opencode-go/kimi-k2.6"
    assert "opencode-go/minimax-m2.5" in config.models
    assert (tmp_path / "config.toml").exists()


@pytest.mark.asyncio
async def test_login_opencode_go_rejects_401(monkeypatch, tmp_path):
    from pythinker_code.auth.opencode_go import login_opencode_go_api_key

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)

    async def fake_discover(api_key):
        raise aiohttp.ClientResponseError(
            _request_info("https://opencode.ai/zen/go/v1/models"),
            (),
            status=401,
            message="Unauthorized",
        )

    monkeypatch.setattr(
        "pythinker_code.auth.opencode_go._discover_opencode_go_models", fake_discover
    )

    events = [event async for event in login_opencode_go_api_key(config, "bad-key")]

    assert events[-1].type == "error"
    assert "Invalid OpenCode Go API key" in events[-1].message
    assert config.providers == {}
    assert config.models == {}


@pytest.mark.asyncio
async def test_login_opencode_go_uses_discovered_context_length(monkeypatch, tmp_path):
    from pythinker_code.auth.opencode_go import OpenCodeGoModel, login_opencode_go_api_key

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)

    async def fake_discover(api_key):
        return (
            OpenCodeGoModel(
                "kimi-k2.6",
                "Kimi K2.6",
                "managed:opencode-go-openai",
                512_000,
            ),
        )

    monkeypatch.setattr(
        "pythinker_code.auth.opencode_go._discover_opencode_go_models", fake_discover
    )

    events = [event async for event in login_opencode_go_api_key(config, "ocgo-test")]

    assert events[-1].type == "success"
    assert config.models["opencode-go/kimi-k2.6"].max_context_size == 512_000


@pytest.mark.asyncio
async def test_login_opencode_go_requires_key(monkeypatch, tmp_path):
    from pythinker_code.auth.opencode_go import login_opencode_go_api_key

    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_ZEN_API_KEY", raising=False)
    config = Config(is_from_default_location=True)

    events = [event async for event in login_opencode_go_api_key(config, "")]

    assert events[-1].type == "error"
    assert events[-1].message == "OpenCode Go API key is required."


@pytest.mark.asyncio
async def test_login_opencode_go_falls_back_on_non_auth_response_error(monkeypatch, tmp_path):
    from pythinker_code.auth.opencode_go import login_opencode_go_api_key

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)

    async def fake_discover(api_key):
        raise aiohttp.ClientResponseError(
            _request_info("https://opencode.ai/zen/go/v1/models"),
            (),
            status=503,
            message="Service Unavailable",
        )

    monkeypatch.setattr(
        "pythinker_code.auth.opencode_go._discover_opencode_go_models", fake_discover
    )

    events = [event async for event in login_opencode_go_api_key(config, "ocgo-test")]

    assert [event.type for event in events] == ["info", "success"]
    assert config.default_model == "opencode-go/kimi-k2.6"


@pytest.mark.asyncio
async def test_fetch_models_dev_metadata_degrades_when_catalog_unavailable(monkeypatch):
    """Best-effort enrichment degrades to empty metadata (the curated catalog)
    when the shared models.dev catalog is unavailable, so login never blocks on
    it. The fetch timeout and fail-open behavior now live in and are tested by
    ``auth/models_dev.py``; this layer only delegates to it."""
    from pythinker_code.auth import opencode_go
    from pythinker_code.auth.models_dev import CatalogResult, CatalogStatus

    async def empty_catalog():
        return CatalogResult({}, CatalogStatus.UNAVAILABLE, "none")

    monkeypatch.setattr(opencode_go, "get_models_dev_catalog", empty_catalog)

    result = await opencode_go._fetch_models_dev_metadata()

    assert result == {}  # degrades gracefully to the curated catalog


@pytest.mark.parametrize(
    "payload, expected_ids",
    [
        (None, []),
        ({}, []),
        ({"data": "not a list"}, []),
        ({"data": [{"context_length": 1000}]}, []),  # missing id
        ({"data": [{"id": ""}]}, []),  # empty id skipped
        ({"data": [{"id": "kimi-k2.6"}, "bogus", {"id": "new-model"}]}, ["kimi-k2.6", "new-model"]),
    ],
)
def test_extract_model_ids_handles_malformed_payloads(payload, expected_ids):
    from pythinker_code.auth.opencode_go import _extract_model_ids

    assert _extract_model_ids(payload) == expected_ids


def test_build_models_uses_models_dev_shape_for_known_ids():
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
        OPENCODE_GO_OPENAI_PROVIDER_KEY,
        _build_models,
        _ModelsDevMeta,
    )

    # models.dev is authoritative for shape + context, even for known ids:
    # the Qwen catalog drift that routed them to OpenAI must self-correct.
    result = _build_models(
        ["kimi-k2.6", "qwen3.6-plus"],
        {
            "kimi-k2.6": _ModelsDevMeta("Kimi K2.6", 262_144, False),
            "qwen3.6-plus": _ModelsDevMeta("Qwen3.6 Plus", 262_144, True),
        },
    )
    by_id = {m.model_id: m for m in result}
    assert by_id["kimi-k2.6"].provider_key == OPENCODE_GO_OPENAI_PROVIDER_KEY
    assert by_id["qwen3.6-plus"].provider_key == OPENCODE_GO_ANTHROPIC_PROVIDER_KEY
    assert by_id["qwen3.6-plus"].max_context_size == 262_144


def test_build_models_surfaces_unknown_id_enriched_from_models_dev():
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
        _build_models,
        _ModelsDevMeta,
    )

    # A brand-new model the catalog has never heard of must still appear,
    # carrying the context + Anthropic shape models.dev reports for it.
    result = _build_models(
        ["qwen3.8-max"], {"qwen3.8-max": _ModelsDevMeta("Qwen3.8 Max", 1_000_000, True)}
    )
    by_id = {m.model_id: m for m in result}
    assert by_id["qwen3.8-max"].alias == "opencode-go/qwen3.8-max"
    assert by_id["qwen3.8-max"].display_name == "Qwen3.8 Max"
    assert by_id["qwen3.8-max"].max_context_size == 1_000_000
    assert by_id["qwen3.8-max"].provider_key == OPENCODE_GO_ANTHROPIC_PROVIDER_KEY


def test_build_models_falls_back_to_catalog_then_heuristic_without_metadata():
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
        OPENCODE_GO_DEFAULT_CONTEXT,
        OPENCODE_GO_OPENAI_PROVIDER_KEY,
        _build_models,
    )

    # No models.dev metadata at all (e.g. unreachable): known ids fall back to
    # the corrected catalog, unknown ids to the name heuristic.
    result = _build_models(["qwen3.7-max", "future-model", "minimax-m9.9"], {})
    by_id = {m.model_id: m for m in result}
    # Known Qwen keeps its (corrected) catalog Anthropic shape.
    assert by_id["qwen3.7-max"].provider_key == OPENCODE_GO_ANTHROPIC_PROVIDER_KEY
    # Unknown, unguessable → OpenAI default + derived name + default context.
    assert by_id["future-model"].display_name == "Future Model"
    assert by_id["future-model"].max_context_size == OPENCODE_GO_DEFAULT_CONTEXT
    assert by_id["future-model"].provider_key == OPENCODE_GO_OPENAI_PROVIDER_KEY
    # Unknown minimax-* heuristically routed to the Anthropic-shaped provider.
    assert by_id["minimax-m9.9"].provider_key == OPENCODE_GO_ANTHROPIC_PROVIDER_KEY


def test_native_thinking_capabilities_by_family():
    from pythinker_code.auth.opencode_go import _native_thinking_capabilities

    # GLM / MiniMax / Qwen reason natively — always_thinking, no user dial.
    assert _native_thinking_capabilities("glm-5") == {"always_thinking"}
    assert _native_thinking_capabilities("minimax-m2.7") == {"always_thinking"}
    assert _native_thinking_capabilities("qwen3.7-max") == {"always_thinking"}
    assert _native_thinking_capabilities("qwen3.5-plus") == {"always_thinking"}
    # Other families carry no native-thinking capability here.
    assert _native_thinking_capabilities("kimi-k2.6") is None


def test_parse_models_dev_metadata_extracts_name_context_and_shape():
    from pythinker_code.auth.opencode_go import _ModelsDevMeta, _parse_models_dev_metadata

    payload = {
        "opencode-go": {
            "npm": "@ai-sdk/openai-compatible",
            "models": {
                # Anthropic shape via per-model provider override.
                "qwen3.7-max": {
                    "name": "Qwen3.7 Max",
                    "limit": {"context": 1_000_000},
                    "provider": {"npm": "@ai-sdk/anthropic"},
                },
                # Inherits the OpenAI-compatible provider default.
                "kimi-k2.6": {"name": "Kimi K2.6", "limit": {"context": 262_144}},
                "glm-5": {"name": "GLM-5", "limit": {"context": -1}},  # bad context dropped
                "bad": "not a dict",
            },
        },
        "opencode": {"models": {"gpt-5": {"name": "GPT-5"}}},  # other provider ignored
    }
    result = _parse_models_dev_metadata(payload)
    assert result == {
        "qwen3.7-max": _ModelsDevMeta("Qwen3.7 Max", 1_000_000, True),
        "kimi-k2.6": _ModelsDevMeta("Kimi K2.6", 262_144, False),
        "glm-5": _ModelsDevMeta("GLM-5", None, False),
    }


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"opencode-go": "not a dict"}, {"opencode-go": {"models": "not a dict"}}],
)
def test_parse_models_dev_metadata_handles_malformed_payloads(payload):
    from pythinker_code.auth.opencode_go import _parse_models_dev_metadata

    assert _parse_models_dev_metadata(payload) == {}


@pytest.mark.asyncio
async def test_logout_opencode_go_removes_only_opencode_go(monkeypatch, tmp_path):
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
        OPENCODE_GO_OPENAI_PROVIDER_KEY,
        _apply_opencode_go_config,
        logout_opencode_go,
    )
    from pythinker_code.config import LLMModel, LLMProvider

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)
    config.providers["managed:openai"] = LLMProvider(
        type="openai_responses",
        base_url="https://api.openai.com/v1",
        api_key=SecretStr("sk-test"),
    )
    config.models["openai/gpt-5.2"] = LLMModel(
        provider="managed:openai",
        model="gpt-5.2",
        max_context_size=400_000,
    )
    _apply_opencode_go_config(config, SecretStr("ocgo-test"))

    events = [event async for event in logout_opencode_go(config)]

    assert events[-1].type == "success"
    assert OPENCODE_GO_OPENAI_PROVIDER_KEY not in config.providers
    assert OPENCODE_GO_ANTHROPIC_PROVIDER_KEY not in config.providers
    assert "opencode-go/kimi-k2.6" not in config.models
    assert "managed:openai" in config.providers
    assert "openai/gpt-5.2" in config.models
    assert config.default_model == "openai/gpt-5.2"


@pytest.mark.asyncio
async def test_logout_opencode_go_preserves_non_opencode_go_default(monkeypatch, tmp_path):
    from pythinker_code.auth.opencode_go import (
        _apply_opencode_go_config,
        logout_opencode_go,
    )
    from pythinker_code.config import LLMModel, LLMProvider

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)
    config.providers["managed:openai"] = LLMProvider(
        type="openai_responses",
        base_url="https://api.openai.com/v1",
        api_key=SecretStr("sk-test"),
    )
    config.models["openai/gpt-5.2"] = LLMModel(
        provider="managed:openai",
        model="gpt-5.2",
        max_context_size=400_000,
    )
    _apply_opencode_go_config(config, SecretStr("ocgo-test"))
    # The user explicitly set their default to a non-OpenCode-Go model.
    config.default_model = "openai/gpt-5.2"

    events = [event async for event in logout_opencode_go(config)]

    assert events[-1].type == "success"
    assert config.default_model == "openai/gpt-5.2"


@pytest.mark.asyncio
async def test_logout_opencode_go_rejects_non_default_config_location():
    from pythinker_code.auth.opencode_go import logout_opencode_go

    config = Config(is_from_default_location=False)

    events = [event async for event in logout_opencode_go(config)]

    assert events[-1].type == "error"
    assert "default config file" in events[-1].message
    # No mutation must have occurred.
    assert config.providers == {}
    assert config.models == {}


# ── refresh-on-update: apply_opencode_go_models / refresh_opencode_go_models ──


def _stale_opencode_go_config() -> Config:
    """A config resembling an OpenCode Go login from an older binary: Qwen on the
    wrong (OpenAI) provider, no qwen3.7-max, kimi as the default, and the user's
    own thinking preference set to True."""
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_OPENAI_PROVIDER_KEY,
        OpenCodeGoModel,
        _apply_opencode_go_config,
    )

    stale_models = (
        OpenCodeGoModel("kimi-k2.6", "Kimi K2.6", OPENCODE_GO_OPENAI_PROVIDER_KEY),
        # Pre-fix login put Qwen on the OpenAI-shaped provider.
        OpenCodeGoModel("qwen3.5-plus", "Qwen3.5 Plus", OPENCODE_GO_OPENAI_PROVIDER_KEY, 262_000),
    )
    config = Config(is_from_default_location=True)
    _apply_opencode_go_config(config, SecretStr("ocgo-test"), models=stale_models)
    # User's own preferences that a refresh must not clobber.
    config.default_model = "opencode-go/kimi-k2.6"
    config.default_thinking = True
    return config


def test_apply_opencode_go_models_adds_new_and_corrects_shape_preserving_user_prefs():
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
        OPENCODE_GO_OPENAI_PROVIDER_KEY,
        OpenCodeGoModel,
        apply_opencode_go_models,
    )

    config = _stale_opencode_go_config()
    assert "opencode-go/qwen3.7-max" not in config.models

    discovered = (
        OpenCodeGoModel("kimi-k2.6", "Kimi K2.6", OPENCODE_GO_OPENAI_PROVIDER_KEY),
        OpenCodeGoModel(
            "qwen3.5-plus", "Qwen3.5 Plus", OPENCODE_GO_ANTHROPIC_PROVIDER_KEY, 262_000
        ),
        OpenCodeGoModel(
            "qwen3.7-max", "Qwen3.7 Max", OPENCODE_GO_ANTHROPIC_PROVIDER_KEY, 1_000_000
        ),
    )

    changed = apply_opencode_go_models(config, discovered)

    assert changed is True
    # New model now present on the Anthropic-shaped provider.
    assert config.models["opencode-go/qwen3.7-max"].provider == OPENCODE_GO_ANTHROPIC_PROVIDER_KEY
    assert config.models["opencode-go/qwen3.7-max"].max_context_size == 1_000_000
    # Qwen reasons natively: always_thinking, no user dial.
    assert config.models["opencode-go/qwen3.7-max"].capabilities == {"always_thinking"}
    # Existing Qwen corrected from OpenAI → Anthropic shape.
    assert config.models["opencode-go/qwen3.5-plus"].provider == OPENCODE_GO_ANTHROPIC_PROVIDER_KEY
    # User preferences untouched.
    assert config.default_model == "opencode-go/kimi-k2.6"
    assert config.default_thinking is True


def test_apply_opencode_go_models_prunes_stale_only_and_reassigns_default_when_removed():
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
        OPENCODE_GO_OPENAI_PROVIDER_KEY,
        OpenCodeGoModel,
        apply_opencode_go_models,
    )

    config = _stale_opencode_go_config()
    # A non-OpenCode-Go model the refresh must never touch.
    config.providers["managed:other"] = LLMProvider(
        type="openai_legacy", base_url="https://x/v1", api_key=SecretStr("k")
    )
    config.models["other/keep-me"] = LLMModel(
        provider="managed:other", model="keep-me", max_context_size=100_000
    )
    # Default points at a model that discovery will no longer return.
    config.default_model = "opencode-go/qwen3.5-plus"

    discovered = (
        OpenCodeGoModel("kimi-k2.6", "Kimi K2.6", OPENCODE_GO_OPENAI_PROVIDER_KEY),
        OpenCodeGoModel(
            "qwen3.7-max", "Qwen3.7 Max", OPENCODE_GO_ANTHROPIC_PROVIDER_KEY, 1_000_000
        ),
    )

    changed = apply_opencode_go_models(config, discovered)

    assert changed is True
    # Stale OpenCode Go model pruned.
    assert "opencode-go/qwen3.5-plus" not in config.models
    # Unrelated provider's model preserved.
    assert "other/keep-me" in config.models
    # Default was pruned → reassigned to a still-present OpenCode Go model.
    assert config.default_model in {"opencode-go/kimi-k2.6", "opencode-go/qwen3.7-max"}


def test_apply_opencode_go_models_no_change_returns_false():
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
        OPENCODE_GO_OPENAI_PROVIDER_KEY,
        OpenCodeGoModel,
        apply_opencode_go_models,
    )

    config = Config(is_from_default_location=True)
    from pythinker_code.auth.opencode_go import _apply_opencode_go_config

    current = (
        OpenCodeGoModel("kimi-k2.6", "Kimi K2.6", OPENCODE_GO_OPENAI_PROVIDER_KEY),
        OpenCodeGoModel(
            "qwen3.7-max", "Qwen3.7 Max", OPENCODE_GO_ANTHROPIC_PROVIDER_KEY, 1_000_000
        ),
    )
    _apply_opencode_go_config(config, SecretStr("ocgo-test"), models=current)

    # Applying the identical discovered set is a no-op.
    assert apply_opencode_go_models(config, current) is False


@pytest.mark.asyncio
async def test_refresh_opencode_go_models_returns_none_when_not_configured():
    from pythinker_code.auth.opencode_go import refresh_opencode_go_models

    config = Config(is_from_default_location=True)
    assert await refresh_opencode_go_models(config) is None


@pytest.mark.asyncio
async def test_refresh_opencode_go_models_discovers_using_saved_key(monkeypatch):
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_OPENAI_PROVIDER_KEY,
        OpenCodeGoModel,
        refresh_opencode_go_models,
    )

    config = _stale_opencode_go_config()
    seen_keys: list[str] = []

    async def fake_discover(api_key):
        seen_keys.append(api_key)
        return (OpenCodeGoModel("kimi-k2.6", "Kimi K2.6", OPENCODE_GO_OPENAI_PROVIDER_KEY),)

    monkeypatch.setattr(
        "pythinker_code.auth.opencode_go._discover_opencode_go_models", fake_discover
    )

    result = await refresh_opencode_go_models(config)

    assert seen_keys == ["ocgo-test"]
    assert result is not None
    assert result[0].model_id == "kimi-k2.6"
