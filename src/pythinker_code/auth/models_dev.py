from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

import httpx

from pythinker_code.share import get_share_dir
from pythinker_code.utils.logging import logger

# Catalog design derived from opencode's models-dev.ts (MIT License,
# Copyright (c) 2025 opencode).

MODELS_DEV_BASE_URL = "https://models.dev"
MODELS_DEV_CACHE_TTL_SECONDS = 5 * 60
MODELS_DEV_TIMEOUT_SECONDS = 10.0
MODELS_DEV_USER_AGENT = "pythinker-code models.dev catalog"
_CACHE_FILENAME = "models_dev.json"
_LOCK_FILENAME = "models_dev.lock"
_FETCH_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class ModelsDevModel:
    provider_id: str
    model_id: str
    display_name: str
    context_length: int | None
    reasoning: bool
    supports_image_input: bool
    supports_video_input: bool
    npm: str | None = None


@dataclass(frozen=True, slots=True)
class ModelsDevProvider:
    provider_id: str
    display_name: str
    env: tuple[str, ...]
    npm: str | None
    api: str | None
    models: Mapping[str, ModelsDevModel]


ModelsDevCatalog = dict[str, ModelsDevProvider]


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_catalog(payload: object) -> ModelsDevCatalog:
    if not isinstance(payload, dict):
        raise ValueError("models.dev catalog must be a JSON object")

    catalog: ModelsDevCatalog = {}
    for provider_id, raw_provider in cast(dict[str, Any], payload).items():
        if not isinstance(raw_provider, dict):
            continue
        provider = cast(dict[str, Any], raw_provider)
        raw_models = provider.get("models")
        if not isinstance(raw_models, dict):
            continue

        provider_npm = _optional_string(provider.get("npm"))
        models: dict[str, ModelsDevModel] = {}
        for model_id, raw_model in cast(dict[str, Any], raw_models).items():
            if not model_id or not isinstance(raw_model, dict):
                continue
            model = cast(dict[str, Any], raw_model)
            display_name = _optional_string(model.get("name")) or model_id
            raw_limit = model.get("limit")
            raw_context = (
                cast(dict[str, Any], raw_limit).get("context")
                if isinstance(raw_limit, dict)
                else None
            )
            context_length = (
                raw_context
                if isinstance(raw_context, int)
                and not isinstance(raw_context, bool)
                and raw_context > 0
                else None
            )
            raw_modalities = model.get("modalities")
            raw_inputs = (
                cast(dict[str, Any], raw_modalities).get("input")
                if isinstance(raw_modalities, dict)
                else None
            )
            input_modalities: set[str] = set()
            if isinstance(raw_inputs, list):
                input_modalities = {
                    value for value in cast(list[object], raw_inputs) if isinstance(value, str)
                }
            raw_model_provider = model.get("provider")
            model_npm = (
                _optional_string(cast(dict[str, Any], raw_model_provider).get("npm"))
                if isinstance(raw_model_provider, dict)
                else None
            )
            models[model_id] = ModelsDevModel(
                provider_id=provider_id,
                model_id=model_id,
                display_name=display_name,
                context_length=context_length,
                reasoning=model.get("reasoning") is True,
                supports_image_input="image" in input_modalities,
                supports_video_input="video" in input_modalities,
                npm=model_npm or provider_npm,
            )

        raw_env = provider.get("env")
        env = (
            tuple(value for value in cast(list[object], raw_env) if isinstance(value, str))
            if isinstance(raw_env, list)
            else ()
        )
        catalog[provider_id] = ModelsDevProvider(
            provider_id=provider_id,
            display_name=_optional_string(provider.get("name")) or provider_id,
            env=env,
            npm=provider_npm,
            api=_optional_string(provider.get("api")),
            models=models,
        )
    return catalog


def parse_models_dev_catalog(payload: object) -> ModelsDevCatalog:
    """Normalize a models.dev JSON object, returning an empty catalog if invalid."""
    try:
        return _parse_catalog(payload)
    except (TypeError, ValueError):
        return {}


def get_provider_models(
    catalog: Mapping[str, ModelsDevProvider], provider_id: str
) -> Mapping[str, ModelsDevModel]:
    """Return normalized models for one provider, or an empty mapping."""
    provider = catalog.get(provider_id)
    return provider.models if provider is not None else {}


def _cache_is_fresh(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime < MODELS_DEV_CACHE_TTL_SECONDS
    except OSError:
        return False


def _read_cache(path: Path) -> ModelsDevCatalog | None:
    try:
        with path.open(encoding="utf-8") as cache_file:
            payload = json.load(cache_file)
        return _parse_catalog(payload)
    except (OSError, TypeError, ValueError):
        return None


def _write_cache(path: Path, payload: object) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(payload, temporary_file, ensure_ascii=False, separators=(",", ":"))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def _fetch_disabled() -> bool:
    value = os.getenv("PYTHINKER_DISABLE_MODELS_FETCH", "").strip().lower()
    return value not in {"", "0", "false", "no", "off"}


def _catalog_url() -> str:
    base_url = os.getenv("PYTHINKER_MODELS_URL", MODELS_DEV_BASE_URL).rstrip("/")
    return f"{base_url}/api.json"


async def _fetch_catalog_payload() -> object:
    last_error: Exception | None = None
    async with httpx.AsyncClient(
        headers={"User-Agent": MODELS_DEV_USER_AGENT},
        timeout=MODELS_DEV_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as client:
        for attempt in range(_FETCH_ATTEMPTS):
            try:
                response = await client.get(_catalog_url())
                response.raise_for_status()
                payload = response.json()
                _parse_catalog(payload)
                return payload
            except (httpx.HTTPError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < _FETCH_ATTEMPTS:
                    await asyncio.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
    if last_error is None:
        raise RuntimeError("models.dev fetch failed without an error")
    raise last_error


def _acquire_lock(path: Path) -> IO[str]:
    lock_file = path.open("a", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    except Exception:
        lock_file.close()
        raise
    return lock_file


def _release_lock(lock_file: IO[str]) -> None:
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


async def _get_models_dev_catalog() -> ModelsDevCatalog:
    share_dir = get_share_dir()
    cache_path = share_dir / _CACHE_FILENAME
    cached = _read_cache(cache_path)
    if cached is not None and _cache_is_fresh(cache_path):
        return cached
    if _fetch_disabled():
        return cached or {}

    try:
        lock_file = await asyncio.to_thread(_acquire_lock, share_dir / _LOCK_FILENAME)
    except Exception as exc:
        logger.debug("models.dev catalog lock unavailable: {error}", error=exc)
        return cached or {}
    try:
        locked_cache = _read_cache(cache_path)
        if locked_cache is not None and _cache_is_fresh(cache_path):
            return locked_cache
        fallback = locked_cache if locked_cache is not None else cached
        try:
            payload = await _fetch_catalog_payload()
            catalog = _parse_catalog(payload)
        except Exception as exc:
            logger.debug("models.dev catalog fetch failed: {error}", error=exc)
            return fallback or {}
        try:
            _write_cache(cache_path, payload)
        except OSError as exc:
            logger.debug("models.dev catalog cache write failed: {error}", error=exc)
        return catalog
    finally:
        _release_lock(lock_file)


async def get_models_dev_catalog() -> ModelsDevCatalog:
    """Return the normalized catalog, failing open to cached or empty data."""
    try:
        return await _get_models_dev_catalog()
    except Exception as exc:
        logger.debug("models.dev catalog unavailable: {error}", error=exc)
        return {}


__all__ = [
    "ModelsDevCatalog",
    "ModelsDevModel",
    "ModelsDevProvider",
    "get_models_dev_catalog",
    "get_provider_models",
    "parse_models_dev_catalog",
]
