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

    catalog = await models_dev.get_models_dev_catalog()

    assert catalog["example"].models["reasoner"].context_length == 128_000


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

    catalog = await models_dev.get_models_dev_catalog()

    assert catalog["example"].models["reasoner"].display_name == "Example Reasoner"


@pytest.mark.asyncio
async def test_disabled_fetch_without_cache_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    monkeypatch.setenv("PYTHINKER_DISABLE_MODELS_FETCH", "yes")

    async def unexpected_fetch():
        pytest.fail("disabled model fetching must not access the network")

    monkeypatch.setattr(models_dev, "_fetch_catalog_payload", unexpected_fetch)

    assert await models_dev.get_models_dev_catalog() == {}
