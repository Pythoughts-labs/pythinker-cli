"""Tests for managed-platform model listing and syncing."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from pydantic import SecretStr

from pythinker_code.auth.platforms import (
    ModelInfo,
    _apply_models,
    _list_models,
    refresh_managed_models,
)
from pythinker_code.config import Config, LLMModel, LLMProvider, OAuthRef, Services
from pythinker_code.llm import model_display_name


def _make_config_with_model(
    *,
    display_name: str | None = None,
    api_key: str = "",
) -> Config:
    provider = LLMProvider(
        type="pythinker",
        base_url="https://api.test/v1",
        api_key=SecretStr(api_key),
        oauth=OAuthRef(storage="file", key="oauth/pythinker-code"),
    )
    model = LLMModel(
        provider="managed:pythinker-code",
        model="pythinker-for-coding",
        max_context_size=100_000,
        display_name=display_name,
    )
    return Config(
        default_model="pythinker-code/pythinker-for-coding",
        providers={"managed:pythinker-code": provider},
        models={"pythinker-code/pythinker-for-coding": model},
        services=Services(),
    )


def test_digitalocean_platform_is_registered() -> None:
    from pythinker_code.auth import DIGITALOCEAN_PLATFORM_ID
    from pythinker_code.auth.platforms import get_platform_by_id, managed_provider_key

    platform = get_platform_by_id(DIGITALOCEAN_PLATFORM_ID)

    assert platform is not None
    assert platform.name == "DigitalOcean"
    assert platform.base_url == "https://inference.do-ai.run/v1"
    assert managed_provider_key(platform.id) == "managed:digitalocean"


def test_snowflake_platform_is_registered() -> None:
    from pythinker_code.auth import SNOWFLAKE_CORTEX_PLATFORM_ID
    from pythinker_code.auth.platforms import get_platform_by_id, managed_provider_key

    platform = get_platform_by_id(SNOWFLAKE_CORTEX_PLATFORM_ID)

    assert platform is not None
    assert platform.name == "Snowflake Cortex"
    assert platform.base_url == "https://app.snowflake.com"
    assert managed_provider_key(platform.id) == "managed:snowflake-cortex"


@pytest.mark.asyncio
async def test_refresh_managed_models_skips_snowflake_curated_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_key = "managed:snowflake-cortex"
    alias = "snowflake-cortex/claude-sonnet-4-5"
    config = Config(
        is_from_default_location=True,
        default_model=alias,
        providers={
            provider_key: LLMProvider(
                type="openai_legacy",
                base_url="https://myorg-acct.snowflakecomputing.com/api/v2/cortex/v1",
                api_key=SecretStr(""),
                oauth=OAuthRef(storage="file", key="oauth/snowflake-cortex/myorg-acct"),
            )
        },
        models={
            alias: LLMModel(
                provider=provider_key,
                model="claude-sonnet-4-5",
                max_context_size=200_000,
            )
        },
    )
    called = False

    async def should_not_list_models(*args: Any, **kwargs: Any) -> list[ModelInfo]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("pythinker_code.auth.platforms.list_models", should_not_list_models)

    changed = await refresh_managed_models(config)

    assert called is False
    assert changed is False
    assert alias in config.models


@pytest.mark.asyncio
async def test_refresh_managed_models_skips_digitalocean_router_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_key = "managed:digitalocean"
    alias = "digitalocean/router:production"
    config = Config(
        is_from_default_location=True,
        default_model=alias,
        providers={
            provider_key: LLMProvider(
                type="openai_legacy",
                base_url="https://inference.do-ai.run/v1",
                api_key=SecretStr("token"),
            )
        },
        models={
            alias: LLMModel(
                provider=provider_key,
                model="router:production",
                max_context_size=128_000,
            )
        },
    )
    called = False

    async def should_not_list_models(*args: Any, **kwargs: Any) -> list[ModelInfo]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("pythinker_code.auth.platforms.list_models", should_not_list_models)

    changed = await refresh_managed_models(config)

    assert called is False
    assert changed is False
    assert alias in config.models


# ── ModelInfo / _list_models: display_name parsing ─────────────────


@pytest.mark.asyncio
async def test_list_models_parses_display_name():
    """_list_models should capture display_name from the API response."""
    api_payload = {
        "data": [
            {
                "id": "pythinker-for-coding",
                "context_length": 262_144,
                "supports_reasoning": True,
                "supports_image_in": True,
                "supports_video_in": True,
                "display_name": "pythinker-ai-code-preview",
            }
        ]
    }

    mock_response = MagicMock()
    mock_response.json = AsyncMock(return_value=api_payload)

    class FakeCM:
        async def __aenter__(self):
            return mock_response

        async def __aexit__(self, *args):
            pass

    session = MagicMock()
    session.get = MagicMock(return_value=FakeCM())

    models = await _list_models(session, base_url="https://api.test/v1", api_key="k")
    assert len(models) == 1
    assert models[0].display_name == "pythinker-ai-code-preview"


@pytest.mark.asyncio
async def test_list_models_display_name_absent_is_none():
    """Missing display_name should become None on the ModelInfo."""
    api_payload = {
        "data": [
            {
                "id": "pythinker-for-coding",
                "context_length": 262_144,
                "supports_reasoning": False,
                "supports_image_in": False,
                "supports_video_in": False,
            }
        ]
    }

    mock_response = MagicMock()
    mock_response.json = AsyncMock(return_value=api_payload)

    class FakeCM:
        async def __aenter__(self):
            return mock_response

        async def __aexit__(self, *args):
            pass

    session = MagicMock()
    session.get = MagicMock(return_value=FakeCM())

    models = await _list_models(session, base_url="https://api.test/v1", api_key="k")
    assert models[0].display_name is None


# ── _apply_models: display_name sync ──────────────────────────────


def test_apply_models_writes_display_name_on_insert():
    """New model entries should carry display_name from the API."""
    config = Config(services=Services())
    models = [
        ModelInfo(
            id="pythinker-for-coding",
            context_length=262_144,
            supports_reasoning=True,
            supports_image_in=True,
            supports_video_in=True,
            display_name="pythinker-ai-code-preview",
        )
    ]

    changed = _apply_models(config, "managed:pythinker-code", "pythinker-code", models)

    assert changed is True
    entry = config.models["pythinker-code/pythinker-for-coding"]
    assert entry.display_name == "pythinker-ai-code-preview"


def test_apply_models_updates_display_name_on_change():
    """Existing model entries should have display_name updated to the latest API value."""
    config = _make_config_with_model(display_name="old-name")
    models = [
        ModelInfo(
            id="pythinker-for-coding",
            context_length=100_000,
            supports_reasoning=False,
            supports_image_in=False,
            supports_video_in=False,
            display_name="pythinker-ai-code-preview",
        )
    ]

    changed = _apply_models(config, "managed:pythinker-code", "pythinker-code", models)

    assert changed is True
    assert (
        config.models["pythinker-code/pythinker-for-coding"].display_name
        == "pythinker-ai-code-preview"
    )


def test_apply_models_clears_display_name_when_api_drops_it():
    """If API stops returning display_name, local entry should be cleared."""
    config = _make_config_with_model(display_name="old-name")
    models = [
        ModelInfo(
            id="pythinker-for-coding",
            context_length=100_000,
            supports_reasoning=False,
            supports_image_in=False,
            supports_video_in=False,
            display_name=None,
        )
    ]

    changed = _apply_models(config, "managed:pythinker-code", "pythinker-code", models)

    assert changed is True
    assert config.models["pythinker-code/pythinker-for-coding"].display_name is None


# ── model_display_name: prefers LLMModel.display_name ────────────


def test_model_display_name_prefers_config_display_name():
    """When LLMModel has a display_name, use it instead of hard-coded mapping."""
    model = LLMModel(
        provider="managed:pythinker-code",
        model="pythinker-for-coding",
        max_context_size=100_000,
        display_name="pythinker-ai-code-preview",
    )
    assert model_display_name("pythinker-for-coding", model) == "pythinker-ai-code-preview"


def test_model_display_name_falls_back_to_hardcoded_when_missing():
    """Without display_name, fall back to the legacy hard-coded mapping."""
    model = LLMModel(
        provider="managed:pythinker-code",
        model="pythinker-for-coding",
        max_context_size=100_000,
    )
    assert model_display_name("pythinker-for-coding", model) == "pythinker-for-coding"


def test_model_display_name_no_model_uses_raw_name():
    """When no LLMModel is provided, use the raw model name."""
    assert model_display_name("pythinker-ai") == "pythinker-ai"


def test_model_display_name_empty_returns_empty():
    assert model_display_name(None) == ""
    assert model_display_name("") == ""


@pytest.mark.asyncio
async def test_refresh_managed_models_retries_after_oauth_401():
    config = _make_config_with_model()
    config.is_from_default_location = True

    models = [
        ModelInfo(
            id="pythinker-for-coding",
            context_length=100_000,
            supports_reasoning=False,
            supports_image_in=False,
            supports_video_in=False,
            display_name=None,
        )
    ]
    unauthorized = aiohttp.ClientResponseError(
        request_info=MagicMock(real_url="https://api.test/v1/models"),
        history=(),
        status=401,
        message="Unauthorized",
    )

    with (
        patch(
            "pythinker_code.auth.platforms.list_models",
            AsyncMock(side_effect=[unauthorized, models]),
        ) as list_models_mock,
        patch(
            "pythinker_code.auth.oauth.OAuthManager.ensure_fresh",
            new=AsyncMock(),
        ) as ensure_fresh_mock,
        patch(
            "pythinker_code.auth.oauth.OAuthManager.resolve_api_key",
            side_effect=["stale-access-token", "fresh-access-token"],
        ),
    ):
        changed = await refresh_managed_models(config)

    assert changed is False
    assert list_models_mock.await_count == 2
    assert len(ensure_fresh_mock.await_args_list) == 2
    oauth_ref = config.providers["managed:pythinker-code"].oauth
    assert ensure_fresh_mock.await_args_list[0].kwargs == {"oauth_ref": oauth_ref}
    assert ensure_fresh_mock.await_args_list[1].kwargs == {
        "force": True,
        "oauth_ref": oauth_ref,
    }


@pytest.mark.asyncio
async def test_refresh_managed_models_401_falls_back_to_static_api_key_when_refresh_fails():
    config = _make_config_with_model(api_key="static-api-key")
    config.is_from_default_location = True

    models = [
        ModelInfo(
            id="pythinker-for-coding",
            context_length=100_000,
            supports_reasoning=False,
            supports_image_in=False,
            supports_video_in=False,
            display_name=None,
        )
    ]
    unauthorized = aiohttp.ClientResponseError(
        request_info=MagicMock(real_url="https://api.test/v1/models"),
        history=(),
        status=401,
        message="Unauthorized",
    )

    with (
        patch(
            "pythinker_code.auth.platforms.list_models",
            AsyncMock(side_effect=[unauthorized, models]),
        ) as list_models_mock,
        patch(
            "pythinker_code.auth.oauth.OAuthManager.ensure_fresh",
            new=AsyncMock(side_effect=[None, RuntimeError("refresh failed")]),
        ) as ensure_fresh_mock,
        patch(
            "pythinker_code.auth.oauth.OAuthManager.resolve_api_key",
            side_effect=["oauth-access-token", "oauth-access-token"],
        ),
    ):
        changed = await refresh_managed_models(config)

    assert changed is False
    assert list_models_mock.await_count == 2
    assert list_models_mock.await_args_list[0].args[1] == "oauth-access-token"
    assert list_models_mock.await_args_list[1].args[1] == "static-api-key"
    assert len(ensure_fresh_mock.await_args_list) == 2
    oauth_ref = config.providers["managed:pythinker-code"].oauth
    assert ensure_fresh_mock.await_args_list[0].kwargs == {"oauth_ref": oauth_ref}
    assert ensure_fresh_mock.await_args_list[1].kwargs == {
        "force": True,
        "oauth_ref": oauth_ref,
    }


def test_lm_studio_base_url_default(monkeypatch):
    monkeypatch.delenv("LM_STUDIO_BASE_URL", raising=False)
    from pythinker_code.auth.platforms import _lm_studio_base_url

    assert _lm_studio_base_url() == "http://localhost:1234/v1"


def test_lm_studio_base_url_env_override(monkeypatch):
    monkeypatch.setenv("LM_STUDIO_BASE_URL", "http://10.0.0.5:1234/v1")
    from pythinker_code.auth.platforms import _lm_studio_base_url

    assert _lm_studio_base_url() == "http://10.0.0.5:1234/v1"


def test_lm_studio_platform_registered():
    from pythinker_code.auth import LM_STUDIO_PLATFORM_ID
    from pythinker_code.auth.platforms import get_platform_by_id

    platform = get_platform_by_id(LM_STUDIO_PLATFORM_ID)
    assert platform is not None
    assert platform.name == "LM Studio"
    assert platform.allowed_prefixes is None


def test_ollama_base_url_default(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    from pythinker_code.auth.platforms import _ollama_base_url

    assert _ollama_base_url() == "http://localhost:11434/v1"


def test_ollama_base_url_env_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://192.168.0.5:11434/v1")
    from pythinker_code.auth.platforms import _ollama_base_url

    assert _ollama_base_url() == "http://192.168.0.5:11434/v1"


def test_ollama_platform_registered():
    from pythinker_code.auth import OLLAMA_PLATFORM_ID
    from pythinker_code.auth.platforms import get_platform_by_id

    platform = get_platform_by_id(OLLAMA_PLATFORM_ID)
    assert platform is not None
    assert platform.name == "Ollama"
    assert platform.allowed_prefixes is None


@pytest.mark.asyncio
async def test_refresh_managed_models_401_tries_static_api_key_after_refreshed_oauth_still_fails():
    config = _make_config_with_model(api_key="static-api-key")
    config.is_from_default_location = True

    models = [
        ModelInfo(
            id="pythinker-for-coding",
            context_length=100_000,
            supports_reasoning=False,
            supports_image_in=False,
            supports_video_in=False,
            display_name=None,
        )
    ]
    unauthorized = aiohttp.ClientResponseError(
        request_info=MagicMock(real_url="https://api.test/v1/models"),
        history=(),
        status=401,
        message="Unauthorized",
    )

    with (
        patch(
            "pythinker_code.auth.platforms.list_models",
            AsyncMock(side_effect=[unauthorized, unauthorized, models]),
        ) as list_models_mock,
        patch(
            "pythinker_code.auth.oauth.OAuthManager.ensure_fresh",
            new=AsyncMock(side_effect=[None, None]),
        ) as ensure_fresh_mock,
        patch(
            "pythinker_code.auth.oauth.OAuthManager.resolve_api_key",
            side_effect=["stale-oauth-token", "fresh-oauth-token"],
        ),
    ):
        changed = await refresh_managed_models(config)

    assert changed is False
    assert list_models_mock.await_count == 3
    assert list_models_mock.await_args_list[0].args[1] == "stale-oauth-token"
    assert list_models_mock.await_args_list[1].args[1] == "fresh-oauth-token"
    assert list_models_mock.await_args_list[2].args[1] == "static-api-key"
    assert len(ensure_fresh_mock.await_args_list) == 2
    oauth_ref = config.providers["managed:pythinker-code"].oauth
    assert ensure_fresh_mock.await_args_list[0].kwargs == {"oauth_ref": oauth_ref}
    assert ensure_fresh_mock.await_args_list[1].kwargs == {
        "force": True,
        "oauth_ref": oauth_ref,
    }


@pytest.mark.asyncio
async def test_list_models_omits_authorization_when_key_is_local(monkeypatch):
    from pythinker_code.auth.platforms import get_platform_by_id, list_models

    captured: dict[str, dict[str, str]] = {}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def json(self):
            return self._payload

    class _Sess:
        def get(self, url, *, headers, raise_for_status):
            captured["headers"] = dict(headers)
            return _Resp({"data": [{"id": "qwen2.5-coder", "context_length": 0}]})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    monkeypatch.setattr(
        "pythinker_code.auth.platforms.new_client_session",
        lambda: _Sess(),
    )

    platform = get_platform_by_id("lm-studio")
    assert platform is not None

    await list_models(platform, "local")
    assert "Authorization" not in captured["headers"]

    await list_models(platform, "real-key")
    assert captured["headers"]["Authorization"] == "Bearer real-key"


@pytest.mark.asyncio
async def test_refresh_managed_models_tolerates_unreachable_local_server(monkeypatch):
    import aiohttp

    from pythinker_code.auth.platforms import refresh_managed_models
    from pythinker_code.config import LLMModel, LLMProvider

    config = Config(is_from_default_location=True)
    config.providers["managed:ollama"] = LLMProvider(
        type="openai_legacy",
        base_url="http://localhost:11434/v1",
        api_key=SecretStr("local"),
    )
    config.models["ollama/llama3.1:8b"] = LLMModel(
        provider="managed:ollama",
        model="llama3.1:8b",
        max_context_size=131072,
    )
    config.default_model = "ollama/llama3.1:8b"

    async def _boom(*args, **kwargs):
        raise aiohttp.ClientConnectorError(  # type: ignore[arg-type]
            connection_key=cast(Any, None), os_error=OSError("no")
        )

    monkeypatch.setattr("pythinker_code.auth.platforms.list_models", _boom)

    # Should not raise; should not delete the saved model.
    changed = await refresh_managed_models(config)

    assert "ollama/llama3.1:8b" in config.models
    assert config.default_model == "ollama/llama3.1:8b"
    # No fallback list applies to local platforms, so no change is recorded.
    assert changed is False


@pytest.mark.asyncio
async def test_refresh_managed_models_uses_saved_provider_base_url(monkeypatch):
    """For non-local managed providers, refresh must hit the saved provider.base_url,
    not the localhost default baked into the Platform registry at import time.

    Local providers (lm-studio, ollama) are skipped entirely — verified separately."""
    from pythinker_code.auth.platforms import (
        _PLATFORM_BY_ID,
        PLATFORMS,
        Platform,
        refresh_managed_models,
    )
    from pythinker_code.config import LLMModel, LLMProvider

    # Register a synthetic non-local managed platform so the skip-local guard
    # in refresh_managed_models doesn't short-circuit us.
    fake_platform = Platform(
        id="synthetic-test",
        name="Synthetic Test",
        base_url="http://import-time-default/v1",
    )
    monkeypatch.setitem(_PLATFORM_BY_ID, "synthetic-test", fake_platform)
    monkeypatch.setattr(
        "pythinker_code.auth.platforms.PLATFORMS",
        [*PLATFORMS, fake_platform],
    )

    config = Config(is_from_default_location=True)
    config.providers["managed:synthetic-test"] = LLMProvider(
        type="openai_legacy",
        base_url="http://saved-remote-host/v1",
        api_key=SecretStr("k"),
    )
    config.models["synthetic-test/m"] = LLMModel(
        provider="managed:synthetic-test",
        model="m",
        max_context_size=32768,
    )
    config.default_model = "synthetic-test/m"

    captured: dict[str, object] = {}

    async def _fake_list(platform: Platform, api_key: str):
        captured["base_url"] = platform.base_url
        return []

    monkeypatch.setattr("pythinker_code.auth.platforms.list_models", _fake_list)

    await refresh_managed_models(config)
    assert captured["base_url"] == "http://saved-remote-host/v1"


@pytest.mark.asyncio
async def test_refresh_managed_models_skips_lm_studio_and_ollama(monkeypatch):
    """Local providers own their own discovery via native endpoints.

    refresh_managed_models must not call the OpenAI-compat /v1/models path
    on them — that path returns sparse data (no context_length) and includes
    embedding models, which would corrupt the saved config.
    """
    from pythinker_code.auth.platforms import refresh_managed_models

    config = Config(is_from_default_location=True)
    config.providers["managed:lm-studio"] = LLMProvider(
        type="openai_legacy",
        base_url="http://localhost:1234/v1",
        api_key=SecretStr("local"),
    )
    config.providers["managed:ollama"] = LLMProvider(
        type="openai_legacy",
        base_url="http://localhost:11434/v1",
        api_key=SecretStr("local"),
    )
    config.models["lm-studio/qwen"] = LLMModel(
        provider="managed:lm-studio",
        model="qwen",
        max_context_size=262144,
        display_name="qwen3 (Q4_K_M)",
    )
    config.models["ollama/llama3.1:8b"] = LLMModel(
        provider="managed:ollama",
        model="llama3.1:8b",
        max_context_size=131072,
    )
    config.default_model = "lm-studio/qwen"

    called = False

    async def _should_not_be_called(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr("pythinker_code.auth.platforms.list_models", _should_not_be_called)

    changed = await refresh_managed_models(config)

    assert called is False, "list_models must not be invoked for local providers"
    assert changed is False
    # Saved metadata is untouched.
    assert config.models["lm-studio/qwen"].max_context_size == 262144
    assert config.models["lm-studio/qwen"].display_name == "qwen3 (Q4_K_M)"
    assert config.models["ollama/llama3.1:8b"].max_context_size == 131072


def _make_opencode_go_config() -> Config:
    """A config with OpenCode Go configured but a stale model list: Qwen on the
    wrong (OpenAI) provider and no qwen3.7-max, as written by an older binary."""
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_ANTHROPIC_BASE_URL,
        OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
        OPENCODE_GO_BASE_URL,
        OPENCODE_GO_OPENAI_PROVIDER_KEY,
    )

    config = Config(
        default_model="opencode-go/kimi-k2.6",
        default_thinking=True,
        providers={
            OPENCODE_GO_OPENAI_PROVIDER_KEY: LLMProvider(
                type="openai_legacy",
                base_url=OPENCODE_GO_BASE_URL,
                api_key=SecretStr("ocgo-test"),
            ),
            OPENCODE_GO_ANTHROPIC_PROVIDER_KEY: LLMProvider(
                type="anthropic",
                base_url=OPENCODE_GO_ANTHROPIC_BASE_URL,
                api_key=SecretStr("ocgo-test"),
            ),
        },
        models={
            "opencode-go/kimi-k2.6": LLMModel(
                provider=OPENCODE_GO_OPENAI_PROVIDER_KEY,
                model="kimi-k2.6",
                max_context_size=262_000,
            ),
            "opencode-go/qwen3.5-plus": LLMModel(
                provider=OPENCODE_GO_OPENAI_PROVIDER_KEY,
                model="qwen3.5-plus",
                max_context_size=262_000,
            ),
        },
        services=Services(),
    )
    config.is_from_default_location = True
    return config


@pytest.mark.asyncio
async def test_refresh_managed_models_refreshes_opencode_go_without_relogin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """The every-startup refresh must update OpenCode Go's two-provider catalog
    via its own discovery — surfacing new models (qwen3.7-max) and correcting
    Qwen's shape — without a manual re-login and without resetting user prefs."""
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
        OPENCODE_GO_OPENAI_PROVIDER_KEY,
        OpenCodeGoModel,
    )
    from pythinker_code.config import load_config, save_config

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = _make_opencode_go_config()
    save_config(config)
    discovered = (
        OpenCodeGoModel("kimi-k2.6", "Kimi K2.6", OPENCODE_GO_OPENAI_PROVIDER_KEY, 262_000),
        OpenCodeGoModel(
            "qwen3.5-plus", "Qwen3.5 Plus", OPENCODE_GO_ANTHROPIC_PROVIDER_KEY, 262_000
        ),
        OpenCodeGoModel(
            "qwen3.7-max", "Qwen3.7 Max", OPENCODE_GO_ANTHROPIC_PROVIDER_KEY, 1_000_000
        ),
    )
    with (
        patch(
            "pythinker_code.auth.opencode_go._discover_opencode_go_models",
            new=AsyncMock(return_value=discovered),
        ),
        patch("pythinker_code.auth.platforms.list_models", new=AsyncMock()) as list_models_mock,
    ):
        changed = await refresh_managed_models(config)

    assert changed is True
    # OpenCode Go must not be routed through the generic single-provider path.
    assert list_models_mock.await_count == 0
    # New model now selectable on the Anthropic-shaped provider, no re-login.
    assert config.models["opencode-go/qwen3.7-max"].provider == OPENCODE_GO_ANTHROPIC_PROVIDER_KEY
    # Existing Qwen's shape corrected OpenAI → Anthropic.
    assert config.models["opencode-go/qwen3.5-plus"].provider == OPENCODE_GO_ANTHROPIC_PROVIDER_KEY
    # User preferences preserved across the refresh.
    assert config.default_model == "opencode-go/kimi-k2.6"
    assert config.default_thinking is True
    # Persisted to disk for subsequent launches.
    assert "opencode-go/qwen3.7-max" in load_config().models


@pytest.mark.asyncio
async def test_refresh_managed_models_isolates_opencode_go_discovery_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """An OpenCode Go discovery failure must not abort other providers' refresh
    or mangle the saved OpenCode Go list."""
    from pythinker_code.auth.opencode_go import OPENCODE_GO_OPENAI_PROVIDER_KEY

    def _config_with_generic_provider() -> Config:
        cfg = _make_opencode_go_config()
        cfg.providers["managed:pythinker-code"] = LLMProvider(
            type="pythinker", base_url="https://api.test/v1", api_key=SecretStr("k")
        )
        cfg.models["pythinker-code/pythinker-for-coding"] = LLMModel(
            provider="managed:pythinker-code",
            model="pythinker-for-coding",
            max_context_size=100_000,
        )
        return cfg

    from pythinker_code.config import load_config, save_config

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = _config_with_generic_provider()
    save_config(config)
    generic_models = [
        ModelInfo(
            id="pythinker-for-coding",
            context_length=200_000,  # differs from saved 100_000 → real update
            supports_reasoning=False,
            supports_image_in=False,
            supports_video_in=False,
            display_name=None,
        )
    ]
    with (
        patch(
            "pythinker_code.auth.opencode_go._discover_opencode_go_models",
            new=AsyncMock(side_effect=aiohttp.ClientConnectionError("offline")),
        ),
        patch(
            "pythinker_code.auth.platforms.list_models",
            new=AsyncMock(return_value=generic_models),
        ),
    ):
        changed = await refresh_managed_models(config)

    # The generic provider's refresh still succeeds and persists.
    assert changed is True
    saved = load_config()
    assert saved.models["pythinker-code/pythinker-for-coding"].max_context_size == 200_000
    # OpenCode Go list is left exactly as-is (not wiped, not half-applied).
    assert "opencode-go/qwen3.7-max" not in config.models
    assert config.models["opencode-go/qwen3.5-plus"].provider == OPENCODE_GO_OPENAI_PROVIDER_KEY


def _make_minimax_config() -> Config:
    from pythinker_code.auth.minimax import (
        MINIMAX_ANTHROPIC_BASE_URL,
        MINIMAX_ANTHROPIC_PROVIDER_KEY,
    )

    config = Config(
        default_model="minimax/m3",
        default_thinking=True,
        providers={
            MINIMAX_ANTHROPIC_PROVIDER_KEY: LLMProvider(
                type="anthropic",
                base_url=MINIMAX_ANTHROPIC_BASE_URL,
                api_key=SecretStr("sk-cp-test"),
            )
        },
        models={
            "minimax/m3": LLMModel(
                provider=MINIMAX_ANTHROPIC_PROVIDER_KEY,
                model="MiniMax-M3",
                max_context_size=1_000_000,
                capabilities={"always_thinking", "image_in", "video_in"},
            ),
            "minimax/m2.7": LLMModel(
                provider=MINIMAX_ANTHROPIC_PROVIDER_KEY,
                model="MiniMax-M2.7",
                max_context_size=204_800,
            ),
            "minimax/m2.7-highspeed": LLMModel(
                provider=MINIMAX_ANTHROPIC_PROVIDER_KEY,
                model="MiniMax-M2.7-highspeed",
                max_context_size=204_800,
            ),
        },
        services=Services(),
    )
    config.is_from_default_location = True
    return config


@pytest.mark.asyncio
async def test_refresh_managed_models_refreshes_minimax_token_plan_without_relogin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """MiniMax startup refresh must use the authenticated live catalog.

    Token Plan availability is key-specific, so stale aliases from an older
    login must be pruned and newly available models surfaced without routing
    through the generic managed-provider path.
    """
    from pythinker_code.auth.minimax import (
        MINIMAX_ANTHROPIC_PROVIDER_KEY,
        MiniMaxModel,
    )
    from pythinker_code.config import load_config, save_config

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = _make_minimax_config()
    save_config(config)
    discovered = (
        MiniMaxModel("MiniMax-M2.7", "m2.7", "MiniMax M2.7", max_context_size=205_000),
        MiniMaxModel("MiniMax-M3", "m3", "MiniMax M3", max_context_size=512_000),
    )
    with (
        patch(
            "pythinker_code.auth.minimax.refresh_minimax_models",
            new=AsyncMock(return_value=discovered),
        ),
        patch("pythinker_code.auth.platforms.list_models", new=AsyncMock()) as list_models_mock,
    ):
        changed = await refresh_managed_models(config)

    assert changed is True
    assert list_models_mock.await_count == 0
    assert "minimax/m2.7-highspeed" not in config.models
    assert config.models["minimax/m2.7"].max_context_size == 205_000
    assert config.models["minimax/m3"].provider == MINIMAX_ANTHROPIC_PROVIDER_KEY
    assert config.default_model == "minimax/m3"
    assert config.default_thinking is True
    saved = load_config()
    assert "minimax/m3" in saved.models
    assert "minimax/m2.7-highspeed" not in saved.models


@pytest.mark.asyncio
async def test_refresh_managed_models_applies_empty_minimax_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """An authenticated empty MiniMax catalog is authoritative and prunes stale models."""
    from pythinker_code.auth.minimax import MINIMAX_ANTHROPIC_PROVIDER_KEY
    from pythinker_code.config import load_config, save_config

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = _make_minimax_config()
    save_config(config)

    with (
        patch(
            "pythinker_code.auth.minimax.refresh_minimax_models",
            new=AsyncMock(return_value=()),
        ) as refresh_mock,
        patch("pythinker_code.auth.platforms.list_models", new=AsyncMock()) as list_models_mock,
    ):
        changed = await refresh_managed_models(config)

    assert changed is True
    assert refresh_mock.await_count == 1
    assert list_models_mock.await_count == 0
    assert not any(
        model.provider == MINIMAX_ANTHROPIC_PROVIDER_KEY for model in config.models.values()
    )
    assert config.default_model == ""
    saved = load_config()
    assert not any(
        model.provider == MINIMAX_ANTHROPIC_PROVIDER_KEY for model in saved.models.values()
    )


@pytest.mark.asyncio
async def test_refresh_managed_models_isolates_minimax_discovery_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """MiniMax refresh failure must not abort other managed-provider refreshes."""

    def _config_with_generic_provider() -> Config:
        cfg = _make_minimax_config()
        cfg.providers["managed:pythinker-code"] = LLMProvider(
            type="pythinker",
            base_url="https://api.test/v1",
            api_key=SecretStr("k"),
        )
        cfg.models["pythinker-code/pythinker-for-coding"] = LLMModel(
            provider="managed:pythinker-code",
            model="pythinker-for-coding",
            max_context_size=100_000,
        )
        return cfg

    from pythinker_code.config import load_config, save_config

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = _config_with_generic_provider()
    save_config(config)
    generic_models = [
        ModelInfo(
            id="pythinker-for-coding",
            context_length=200_000,
            supports_reasoning=False,
            supports_image_in=False,
            supports_video_in=False,
            display_name=None,
        )
    ]
    with (
        patch(
            "pythinker_code.auth.minimax.refresh_minimax_models",
            new=AsyncMock(side_effect=aiohttp.ClientConnectionError("offline")),
        ),
        patch(
            "pythinker_code.auth.platforms.list_models",
            new=AsyncMock(return_value=generic_models),
        ),
    ):
        changed = await refresh_managed_models(config)

    assert changed is True
    saved = load_config()
    assert saved.models["pythinker-code/pythinker-for-coding"].max_context_size == 200_000
    assert "minimax/m2.7-highspeed" in saved.models
    assert saved.models["minimax/m2.7-highspeed"].provider == "managed:minimax-anthropic"
    assert "minimax/m2.7-highspeed" in config.models


@pytest.mark.parametrize(
    "api_result",
    [
        pytest.param(("degraded", "transport"), id="degraded"),
        pytest.param(("unauthorized", "unauthorized"), id="unauthorized"),
        pytest.param(("unconfigured", "unconfigured"), id="unconfigured"),
    ],
)
async def test_refresh_zai_routes_apply_live_catalog_independently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    api_result: tuple[str, str],
) -> None:
    from pythinker_code.auth import kimi, minimax, opencode_go, z_ai
    from pythinker_code.auth.z_ai import (
        ZAI_API_ROUTE,
        ZAI_CODING_ROUTE,
        ZaiCatalogResult,
        ZaiModel,
        ZaiRoute,
        _apply_z_ai_config,
    )
    from pythinker_code.config import load_config, save_config

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = Config(is_from_default_location=True)
    _apply_z_ai_config(config, ZAI_CODING_ROUTE, SecretStr("coding"))
    _apply_z_ai_config(config, ZAI_API_ROUTE, SecretStr("api"))
    save_config(config)
    api_before = {
        key: value.model_copy(deep=True)
        for key, value in config.models.items()
        if value.provider == ZAI_API_ROUTE.provider_key
    }

    async def fake_refresh_zai(_config: Config, route: ZaiRoute) -> ZaiCatalogResult:
        if route == ZAI_CODING_ROUTE:
            return ZaiCatalogResult(
                status="live",
                models=(ZaiModel("glm-5.1", "GLM-5.1 Live", 333_000),),
            )
        status, failure = api_result
        return ZaiCatalogResult(
            status=cast(Any, status),
            models=None,
            failure=cast(Any, failure),
        )

    monkeypatch.setattr(z_ai, "refresh_z_ai_models", fake_refresh_zai)
    monkeypatch.setattr(opencode_go, "refresh_opencode_go_models", AsyncMock(return_value=None))
    monkeypatch.setattr(minimax, "refresh_minimax_models", AsyncMock(return_value=None))
    monkeypatch.setattr(kimi, "refresh_kimi_models", AsyncMock(return_value=None))

    changed = await refresh_managed_models(config)

    assert changed is True
    assert config.models["z-ai-coding/glm-5.1"].max_context_size == 333_000
    api_after = {
        key: value
        for key, value in config.models.items()
        if value.provider == ZAI_API_ROUTE.provider_key
    }
    assert api_after == api_before

    reloaded = load_config()
    assert reloaded.models["z-ai-coding/glm-5.1"].max_context_size == 333_000
    reloaded_api = {
        key: value
        for key, value in reloaded.models.items()
        if value.provider == ZAI_API_ROUTE.provider_key
    }
    assert reloaded_api == api_before


@pytest.mark.asyncio
async def test_refresh_managed_models_merges_stale_discovery_into_authoritative_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from pythinker_code.auth import platforms
    from pythinker_code.config import load_config, save_config

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    stale = _make_config_with_model(api_key="static-api-key")
    stale.is_from_default_location = True
    stale.providers["managed:pythinker-code"].oauth = None
    authoritative = stale.model_copy(deep=True)
    authoritative.providers["managed:concurrent-login"] = LLMProvider(
        type="openai_legacy",
        base_url="https://concurrent.example/v1",
        api_key=SecretStr("concurrent-key"),
    )
    save_config(authoritative)
    discovered = [
        ModelInfo(
            id="pythinker-for-coding",
            context_length=200_000,
            supports_reasoning=False,
            supports_image_in=False,
            supports_video_in=False,
        )
    ]

    monkeypatch.setattr(platforms, "list_models", AsyncMock(return_value=discovered))
    # Model the background writer having read before the concurrent login committed.
    monkeypatch.setattr(
        platforms,
        "load_config",
        lambda: stale.model_copy(deep=True),
        raising=False,
    )

    changed = await refresh_managed_models(stale)

    committed = load_config()
    assert changed is True
    assert "managed:concurrent-login" in committed.providers
    assert committed.models["pythinker-code/pythinker-for-coding"].max_context_size == 200_000
    assert "managed:concurrent-login" in stale.providers
    assert stale.models["pythinker-code/pythinker-for-coding"].max_context_size == 200_000


@pytest.mark.asyncio
async def test_refresh_managed_models_does_not_mutate_caller_during_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from pythinker_code.auth import opencode_go, platforms
    from pythinker_code.config import save_config

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = _make_config_with_model(api_key="static-api-key")
    config.is_from_default_location = True
    config.providers["managed:pythinker-code"].oauth = None
    before = config.model_copy(deep=True)
    save_config(config)
    discovered = [
        ModelInfo(
            id="pythinker-for-coding",
            context_length=200_000,
            supports_reasoning=False,
            supports_image_in=False,
            supports_video_in=False,
        )
    ]
    observations: list[bool] = []

    async def observe_later_discovery(candidate: Config) -> None:
        observations.append(candidate is not config and config == before)
        return None

    monkeypatch.setattr(platforms, "list_models", AsyncMock(return_value=discovered))
    monkeypatch.setattr(opencode_go, "refresh_opencode_go_models", observe_later_discovery)

    changed = await refresh_managed_models(config)

    assert changed is True
    assert observations == [True]
    assert config.models["pythinker-code/pythinker-for-coding"].max_context_size == 200_000


@pytest.mark.asyncio
async def test_refresh_managed_models_persists_collected_noop_to_authoritative_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from pythinker_code.auth import platforms
    from pythinker_code.config import load_config, save_config

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    caller = _make_config_with_model(api_key="static-api-key")
    caller.is_from_default_location = True
    caller.providers["managed:pythinker-code"].oauth = None
    authoritative = caller.model_copy(deep=True)
    authoritative.models["pythinker-code/pythinker-for-coding"].max_context_size = 50_000
    save_config(authoritative)
    discovered = [
        ModelInfo(
            id="pythinker-for-coding",
            context_length=100_000,
            supports_reasoning=False,
            supports_image_in=False,
            supports_video_in=False,
        )
    ]
    monkeypatch.setattr(platforms, "list_models", AsyncMock(return_value=discovered))

    changed = await refresh_managed_models(caller)

    committed = load_config()
    assert changed is False
    assert committed.models["pythinker-code/pythinker-for-coding"].max_context_size == 100_000
    assert caller.models["pythinker-code/pythinker-for-coding"].max_context_size == 100_000


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_change", ["logout", "replacement"])
async def test_refresh_managed_models_drops_results_after_provider_identity_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    concurrent_change: str,
) -> None:
    from pythinker_code.auth import platforms
    from pythinker_code.auth.oauth import persist_config_change
    from pythinker_code.config import load_config, save_config

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    provider_key = "managed:pythinker-code"
    old_alias = "pythinker-code/old-model"
    new_alias = "pythinker-code/new-account-model"
    config = Config(
        is_from_default_location=True,
        default_model=old_alias,
        providers={
            provider_key: LLMProvider(
                type="pythinker",
                base_url="https://old.example/v1",
                api_key=SecretStr("old-key"),
            )
        },
        models={
            old_alias: LLMModel(
                provider=provider_key,
                model="old-model",
                max_context_size=100_000,
            )
        },
    )
    save_config(config)
    concurrent_caller = config.model_copy(deep=True)

    async def discover_then_change_provider(*_args: Any, **_kwargs: Any) -> list[ModelInfo]:
        def change_provider(authoritative: Config) -> None:
            authoritative.providers.pop(provider_key, None)
            authoritative.models = {
                alias: model
                for alias, model in authoritative.models.items()
                if model.provider != provider_key
            }
            authoritative.default_model = ""
            if concurrent_change == "replacement":
                authoritative.providers[provider_key] = LLMProvider(
                    type="pythinker",
                    base_url="https://new.example/v1",
                    api_key=SecretStr("new-key"),
                )
                authoritative.models[new_alias] = LLMModel(
                    provider=provider_key,
                    model="new-account-model",
                    max_context_size=300_000,
                )
                authoritative.default_model = new_alias

        await persist_config_change(concurrent_caller, change_provider)
        return [
            ModelInfo(
                id="old-model",
                context_length=200_000,
                supports_reasoning=False,
                supports_image_in=False,
                supports_video_in=False,
            )
        ]

    monkeypatch.setattr(platforms, "list_models", discover_then_change_provider)

    changed = await refresh_managed_models(config)

    committed = load_config()
    assert changed is True
    if concurrent_change == "logout":
        assert provider_key not in committed.providers
        assert not any(model.provider == provider_key for model in committed.models.values())
    else:
        assert committed.providers[provider_key].base_url == "https://new.example/v1"
        assert new_alias in committed.models
        assert old_alias not in committed.models
    assert config.providers == committed.providers
    assert config.models == committed.models


@pytest.mark.asyncio
async def test_refresh_managed_models_ignores_empty_opencode_go_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from pythinker_code.auth import opencode_go, platforms
    from pythinker_code.config import load_config, save_config

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = _make_opencode_go_config()
    before = config.model_copy(deep=True)
    save_config(config)
    monkeypatch.setattr(opencode_go, "refresh_opencode_go_models", AsyncMock(return_value=()))
    monkeypatch.setattr(platforms, "list_models", AsyncMock())

    changed = await refresh_managed_models(config)

    assert changed is False
    assert config.models == before.models
    assert load_config().models == before.models


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_change", ["logout", "replacement"])
async def test_refresh_managed_models_drops_opencode_results_after_provider_group_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    concurrent_change: str,
) -> None:
    from pythinker_code.auth import opencode_go, platforms
    from pythinker_code.auth.oauth import persist_config_change
    from pythinker_code.auth.opencode_go import (
        OPENCODE_GO_ANTHROPIC_BASE_URL,
        OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
        OPENCODE_GO_BASE_URL,
        OPENCODE_GO_OPENAI_PROVIDER_KEY,
        OpenCodeGoModel,
    )
    from pythinker_code.config import load_config, save_config

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    config = _make_opencode_go_config()
    save_config(config)
    concurrent_caller = config.model_copy(deep=True)
    replacement_alias = "opencode-go/replacement-model"

    async def discover_then_change_provider_group(
        _working_config: Config,
    ) -> tuple[OpenCodeGoModel, ...]:
        def change_provider_group(authoritative: Config) -> None:
            for provider_key in (
                OPENCODE_GO_OPENAI_PROVIDER_KEY,
                OPENCODE_GO_ANTHROPIC_PROVIDER_KEY,
            ):
                authoritative.providers.pop(provider_key, None)
            authoritative.models = {
                alias: model
                for alias, model in authoritative.models.items()
                if model.provider
                not in {OPENCODE_GO_OPENAI_PROVIDER_KEY, OPENCODE_GO_ANTHROPIC_PROVIDER_KEY}
            }
            authoritative.default_model = ""
            if concurrent_change == "replacement":
                authoritative.providers[OPENCODE_GO_OPENAI_PROVIDER_KEY] = LLMProvider(
                    type="openai_legacy",
                    base_url=OPENCODE_GO_BASE_URL,
                    api_key=SecretStr("replacement-key"),
                )
                authoritative.providers[OPENCODE_GO_ANTHROPIC_PROVIDER_KEY] = LLMProvider(
                    type="anthropic",
                    base_url=OPENCODE_GO_ANTHROPIC_BASE_URL,
                    api_key=SecretStr("replacement-key"),
                )
                authoritative.models[replacement_alias] = LLMModel(
                    provider=OPENCODE_GO_OPENAI_PROVIDER_KEY,
                    model="replacement-model",
                    max_context_size=400_000,
                )
                authoritative.default_model = replacement_alias

        await persist_config_change(concurrent_caller, change_provider_group)
        return (
            OpenCodeGoModel(
                "stale-discovery-model",
                "Stale Discovery Model",
                OPENCODE_GO_OPENAI_PROVIDER_KEY,
                200_000,
            ),
        )

    monkeypatch.setattr(
        opencode_go, "refresh_opencode_go_models", discover_then_change_provider_group
    )
    monkeypatch.setattr(platforms, "list_models", AsyncMock())

    changed = await refresh_managed_models(config)

    committed = load_config()
    assert changed is True
    assert "opencode-go/stale-discovery-model" not in committed.models
    if concurrent_change == "logout":
        assert OPENCODE_GO_OPENAI_PROVIDER_KEY not in committed.providers
        assert OPENCODE_GO_ANTHROPIC_PROVIDER_KEY not in committed.providers
    else:
        assert replacement_alias in committed.models
        assert (
            committed.providers[OPENCODE_GO_OPENAI_PROVIDER_KEY].api_key.get_secret_value()
            == "replacement-key"
        )
    assert config.providers == committed.providers
    assert config.models == committed.models
