from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import IO, Any, cast

import httpx

from pythinker_code.share import get_share_dir
from pythinker_code.utils.logging import logger

# Provider-agnostic model catalog backed by the public models.dev API. It
# supplies dynamic per-model metadata (display name, context window, reasoning
# and modality flags) for any provider, with on-disk caching and fail-open
# behavior so a slow or unreachable endpoint never blocks startup. Callers get
# a CatalogResult that names the outcome (fresh, cached, stale, disabled, or
# unavailable) so degraded data is never mistaken for authoritative success.

MODELS_DEV_BASE_URL = "https://models.dev"
MODELS_DEV_CACHE_TTL_SECONDS = 5 * 60
MODELS_DEV_TIMEOUT_SECONDS = 10.0
MODELS_DEV_USER_AGENT = "pythinker-code models.dev catalog"
_CACHE_FILENAME = "models_dev.json"
_LOCK_FILENAME = "models_dev.lock"
_FETCH_ATTEMPTS = 2
_RETRY_BACKOFF_SECONDS = 0.25
_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_INTERVAL_SECONDS = 0.05

# models.dev has no explicit "chat" flag; a text-only output modality is the
# discriminator that excludes image/video/audio/embedding generators. A few
# text-output-but-non-chat models (rerankers, embeddings, moderation) slip past
# that test, so they are additionally excluded by id substring.
_NON_CHAT_ID_SUBSTRINGS = (
    "embedding",
    "embed",
    "reranker",
    "rerank",
    "moderation",
    "guard",
    "whisper",
    "tts",
    "stt",
    "ocr",
)


class CatalogStatus(str, Enum):
    """Outcome of a catalog load, so callers/logs can tell states apart."""

    OK = "ok"  # fresh data fetched from the network this call
    CACHED = "cached"  # served from a fresh on-disk cache
    STALE = "stale"  # served from a stale cache (fetch failed or disabled)
    DISABLED = "disabled"  # fetching disabled and no cache exists
    UNAVAILABLE = "unavailable"  # no data at all (fetch failed, no cache)


@dataclass(frozen=True, slots=True)
class ModelsDevModel:
    provider_id: str
    model_id: str
    display_name: str
    context_length: int | None
    reasoning: bool
    supports_image_input: bool
    supports_video_input: bool
    output_modalities: tuple[str, ...] = ()
    npm: str | None = None

    @property
    def is_chat_model(self) -> bool:
        """True when the model produces only text output (usable as a chat model)."""
        return bool(self.output_modalities) and all(
            modality == "text" for modality in self.output_modalities
        )


@dataclass(frozen=True, slots=True)
class ModelsDevProvider:
    provider_id: str
    display_name: str
    env: tuple[str, ...]
    npm: str | None
    api: str | None
    models: Mapping[str, ModelsDevModel]


ModelsDevCatalog = dict[str, ModelsDevProvider]


@dataclass(frozen=True, slots=True)
class CatalogResult:
    """A catalog load paired with its outcome and origin."""

    catalog: ModelsDevCatalog
    status: CatalogStatus
    source: str  # "network" | "cache" | "none"

    @property
    def is_authoritative(self) -> bool:
        """True only for a fresh network fetch or a fresh cache hit."""
        return self.status in (CatalogStatus.OK, CatalogStatus.CACHED)


@dataclass(frozen=True, slots=True)
class CatalogModel:
    """A provider-neutral chat model resolved from the catalog."""

    model_id: str
    display_name: str
    max_context_size: int
    reasoning: bool


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in cast(list[object], value) if isinstance(item, str))


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
            modalities = (
                cast(dict[str, Any], raw_modalities) if isinstance(raw_modalities, dict) else {}
            )
            input_modalities = set(_string_tuple(modalities.get("input")))
            output_modalities = _string_tuple(modalities.get("output"))
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
                output_modalities=output_modalities,
                npm=model_npm or provider_npm,
            )

        env = _string_tuple(provider.get("env"))
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


def _is_non_chat_id(model_id: str) -> bool:
    normalized = model_id.lower()
    return any(token in normalized for token in _NON_CHAT_ID_SUBSTRINGS)


def chat_models(
    catalog: Mapping[str, ModelsDevProvider], provider_id: str
) -> dict[str, ModelsDevModel]:
    """Return the provider's chat-capable models (text output, non-embedding)."""
    return {
        model_id: model
        for model_id, model in get_provider_models(catalog, provider_id).items()
        if model.is_chat_model and not _is_non_chat_id(model_id)
    }


def build_catalog_models(
    catalog: Mapping[str, ModelsDevProvider],
    provider_id: str,
    *,
    default_context: int,
) -> tuple[CatalogModel, ...]:
    """Resolve a provider's chat models into provider-neutral entries.

    Filters to text-output chat models, applies ``default_context`` when the
    catalog has no context window, and returns a deterministically ordered
    tuple so provider config stays stable across runs.
    """
    resolved = [
        CatalogModel(
            model_id=model_id,
            display_name=model.display_name,
            max_context_size=model.context_length or default_context,
            reasoning=model.reasoning,
        )
        for model_id, model in sorted(chat_models(catalog, provider_id).items())
    ]
    return tuple(resolved)


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


def _try_acquire_lock(lock_file: IO[str]) -> bool:
    """Attempt a non-blocking exclusive lock; True if acquired, False if held."""
    if os.name == "nt":
        import msvcrt

        try:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_lock(lock_file: IO[str]) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


async def _acquire_lock_bounded(path: Path) -> IO[str] | None:
    """Acquire the cross-process lock with a bounded, cancellable wait.

    Returns the locked handle, or None if the deadline elapsed or the lock file
    could not be opened. The lock is polled non-blockingly with an ``asyncio``
    sleep between attempts, so cancellation is honored promptly and the handle
    is always closed on exit — no descriptor or cross-process lock is orphaned.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _LOCK_TIMEOUT_SECONDS
    try:
        lock_file = path.open("a", encoding="utf-8")
    except OSError:
        return None
    try:
        while True:
            if _try_acquire_lock(lock_file):
                return lock_file
            if loop.time() >= deadline:
                lock_file.close()
                return None
            await asyncio.sleep(_LOCK_POLL_INTERVAL_SECONDS)
    except BaseException:
        lock_file.close()
        raise


def _fallback_result(cached: ModelsDevCatalog | None) -> CatalogResult:
    if cached is not None:
        return CatalogResult(cached, CatalogStatus.STALE, "cache")
    return CatalogResult({}, CatalogStatus.UNAVAILABLE, "none")


async def _load_catalog() -> CatalogResult:
    share_dir = get_share_dir()
    cache_path = share_dir / _CACHE_FILENAME
    cached = _read_cache(cache_path)
    if cached is not None and _cache_is_fresh(cache_path):
        return CatalogResult(cached, CatalogStatus.CACHED, "cache")
    if _fetch_disabled():
        if cached is not None:
            return CatalogResult(cached, CatalogStatus.STALE, "cache")
        return CatalogResult({}, CatalogStatus.DISABLED, "none")

    lock_file = await _acquire_lock_bounded(share_dir / _LOCK_FILENAME)
    if lock_file is None:
        logger.debug("models.dev catalog lock unavailable; using fallback data")
        return _fallback_result(cached)
    try:
        locked_cache = _read_cache(cache_path)
        if locked_cache is not None and _cache_is_fresh(cache_path):
            return CatalogResult(locked_cache, CatalogStatus.CACHED, "cache")
        fallback = locked_cache if locked_cache is not None else cached
        try:
            payload = await _fetch_catalog_payload()
            catalog = _parse_catalog(payload)
        except Exception as exc:
            logger.debug("models.dev catalog fetch failed: {error}", error=exc)
            return _fallback_result(fallback)
        try:
            _write_cache(cache_path, payload)
        except OSError as exc:
            logger.debug("models.dev catalog cache write failed: {error}", error=exc)
        return CatalogResult(catalog, CatalogStatus.OK, "network")
    finally:
        _release_lock(lock_file)


async def get_models_dev_catalog() -> CatalogResult:
    """Return the catalog with its status/source, failing open to empty data."""
    try:
        return await _load_catalog()
    except Exception as exc:
        logger.debug("models.dev catalog unavailable: {error}", error=exc)
        return CatalogResult({}, CatalogStatus.UNAVAILABLE, "none")


__all__ = [
    "CatalogModel",
    "CatalogResult",
    "CatalogStatus",
    "ModelsDevCatalog",
    "ModelsDevModel",
    "ModelsDevProvider",
    "build_catalog_models",
    "chat_models",
    "get_models_dev_catalog",
    "get_provider_models",
    "parse_models_dev_catalog",
]
