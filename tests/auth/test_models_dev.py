from __future__ import annotations

import json
import os
import time

import pytest

from pythinker_code.auth import models_dev


def _catalog_payload() -> dict[str, object]:
    return {
        "example": {
            "name": "Example Provider",
            "env": ["EXAMPLE_API_KEY"],
            "npm": "@ai-sdk/example",
            "api": "https://example.test/v1",
            "models": {
                "reasoner": {
                    "id": "reasoner",
                    "name": "Example Reasoner",
                    "reasoning": True,
                    "limit": {"context": 128_000, "output": 8_192},
                    "modalities": {"input": ["text", "image", "video"], "output": ["text"]},
                }
            },
        }
    }


def _write_cache(tmp_path, payload: object) -> None:
    (tmp_path / "models_dev.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_parse_models_dev_catalog_normalizes_model_metadata():
    catalog = models_dev.parse_models_dev_catalog(_catalog_payload())

    provider = catalog["example"]
    model = models_dev.get_provider_models(catalog, "example")["reasoner"]
    assert provider.display_name == "Example Provider"
    assert provider.env == ("EXAMPLE_API_KEY",)
    assert model.display_name == "Example Reasoner"
    assert model.context_length == 128_000
    assert model.reasoning is True
    assert model.supports_image_input is True
    assert model.supports_video_input is True
    assert model.npm == "@ai-sdk/example"


@pytest.mark.asyncio
async def test_fresh_cache_is_reused_without_network(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    monkeypatch.delenv("PYTHINKER_DISABLE_MODELS_FETCH", raising=False)
    _write_cache(tmp_path, _catalog_payload())

    async def unexpected_fetch():
        pytest.fail("fresh cache must not trigger a network fetch")

    monkeypatch.setattr(models_dev, "_fetch_catalog_payload", unexpected_fetch)

    result = await models_dev.get_models_dev_catalog()

    assert result.status is models_dev.CatalogStatus.CACHED
    assert result.source == "cache"
    assert result.catalog["example"].models["reasoner"].context_length == 128_000


@pytest.mark.asyncio
async def test_network_failure_fails_open_to_stale_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    monkeypatch.delenv("PYTHINKER_DISABLE_MODELS_FETCH", raising=False)
    _write_cache(tmp_path, _catalog_payload())
    stale_time = time.time() - models_dev.MODELS_DEV_CACHE_TTL_SECONDS - 1
    os.utime(tmp_path / "models_dev.json", (stale_time, stale_time))

    async def failed_fetch():
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(models_dev, "_fetch_catalog_payload", failed_fetch)

    result = await models_dev.get_models_dev_catalog()

    # A stale-cache fallback must announce that it is degraded, not masquerade
    # as an authoritative fresh load.
    assert result.status is models_dev.CatalogStatus.STALE
    assert result.is_authoritative is False
    assert result.catalog["example"].models["reasoner"].display_name == "Example Reasoner"


@pytest.mark.asyncio
async def test_disabled_fetch_without_cache_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    monkeypatch.setenv("PYTHINKER_DISABLE_MODELS_FETCH", "yes")

    async def unexpected_fetch():
        pytest.fail("disabled model fetching must not access the network")

    monkeypatch.setattr(models_dev, "_fetch_catalog_payload", unexpected_fetch)

    result = await models_dev.get_models_dev_catalog()

    assert result.status is models_dev.CatalogStatus.DISABLED
    assert result.catalog == {}


def _chat_catalog_payload() -> dict[str, object]:
    return {
        "demo": {
            "name": "Demo",
            "models": {
                "chat-small": {
                    "id": "chat-small",
                    "name": "Chat Small",
                    "limit": {"context": 64_000},
                    "modalities": {"input": ["text"], "output": ["text"]},
                },
                "image-gen": {
                    "id": "image-gen",
                    "name": "Image Gen",
                    "modalities": {"input": ["text"], "output": ["image"]},
                },
                "text-embedding-3": {
                    "id": "text-embedding-3",
                    "name": "Embedding",
                    "modalities": {"input": ["text"], "output": ["text"]},
                },
            },
        }
    }


def test_chat_models_excludes_non_chat_output_and_embeddings():
    catalog = models_dev.parse_models_dev_catalog(_chat_catalog_payload())

    ids = set(models_dev.chat_models(catalog, "demo"))

    # Image generator excluded by output modality; embedding excluded by id.
    assert ids == {"chat-small"}


def test_build_catalog_models_applies_default_context_and_ordering():
    catalog = models_dev.parse_models_dev_catalog(_chat_catalog_payload())

    built = models_dev.build_catalog_models(catalog, "demo", default_context=100_000)

    assert [model.model_id for model in built] == ["chat-small"]
    assert built[0].max_context_size == 64_000

    empty = models_dev.build_catalog_models(catalog, "missing", default_context=100_000)
    assert empty == ()
