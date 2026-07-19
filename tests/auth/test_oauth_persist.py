"""Atomicity guarantees for the shared OAuth persistence helpers.

These pin the rollback contract of ``persist_login`` — in particular that a
re-login whose config save fails must not destroy a still-valid existing
credential (only a fresh login with no prior token deletes on failure).
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from pythinker_code.auth import oauth
from pythinker_code.auth.oauth import OAuthToken
from pythinker_code.config import Config, OAuthRef


def _token(access: str, refresh: str) -> OAuthToken:
    return OAuthToken(
        access_token=access,
        refresh_token=refresh,
        expires_at=9_999_999_999.0,
        scope="test-scope",
        token_type="Bearer",
    )


@pytest.mark.asyncio
async def test_persist_login_restores_previous_token_on_config_save_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="file", key="oauth/test-provider")
    oauth.save_tokens(ref, _token("old-access", "old-refresh"))

    def boom(_config: Config) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(oauth, "save_config", boom)

    config = Config(is_from_default_location=True)

    def apply_config(cfg: Config) -> None:
        cfg.default_model = "provider/new-model"

    with pytest.raises(OSError):
        await oauth.persist_login(config, ref, _token("new-access", "new-refresh"), apply_config)

    # The previously valid credential must survive an unrelated config-save
    # failure — the re-login attempt failed, but the working token is intact.
    restored = oauth.load_tokens(ref)
    assert restored is not None
    assert restored.access_token == "old-access"
    assert restored.refresh_token == "old-refresh"
    # In-memory config is rolled back to before the mutation.
    assert config.default_model == ""


@pytest.mark.asyncio
async def test_persist_login_deletes_token_on_fresh_login_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="file", key="oauth/test-provider")

    def boom(_config: Config) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(oauth, "save_config", boom)

    config = Config(is_from_default_location=True)

    with pytest.raises(OSError):
        await oauth.persist_login(
            config, ref, _token("new-access", "new-refresh"), lambda cfg: None
        )

    # No prior credential existed, so the failed login leaves no orphan token.
    assert oauth.load_tokens(ref) is None


@pytest.mark.asyncio
async def test_persist_login_persists_both_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="file", key="oauth/test-provider")

    saved: list[Config] = []
    monkeypatch.setattr(oauth, "save_config", lambda cfg: saved.append(cfg))

    config = Config(is_from_default_location=True)
    await oauth.persist_login(
        config,
        ref,
        _token("new-access", "new-refresh"),
        lambda cfg: setattr(cfg, "default_model", "provider/new-model"),
    )

    assert saved == [config]
    assert config.default_model == "provider/new-model"
    stored = oauth.load_tokens(ref)
    assert stored is not None
    assert stored.access_token == "new-access"


@pytest.mark.asyncio
async def test_persist_login_runs_complete_unit_off_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="file", key="oauth/thread-probe")
    event_loop_thread = threading.get_ident()
    observed_threads: list[int] = []

    def record_save(_config: Config) -> None:
        observed_threads.append(threading.get_ident())

    monkeypatch.setattr(oauth, "save_config", record_save)

    await oauth.persist_login(
        Config(is_from_default_location=True),
        ref,
        _token("access", "refresh"),
        lambda _cfg: observed_threads.append(threading.get_ident()),
    )

    assert observed_threads
    assert all(thread_id != event_loop_thread for thread_id in observed_threads)


@pytest.mark.asyncio
async def test_failed_concurrent_login_cannot_clobber_successful_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="file", key="oauth/concurrent-provider")
    oauth.save_tokens(ref, _token("old-access", "old-refresh"))

    failed_apply_entered = threading.Event()
    allow_failed_save = threading.Event()
    successful_apply_entered = threading.Event()

    def save_config(config: Config) -> None:
        if config.default_model == "provider/failed":
            raise OSError("disk full")

    monkeypatch.setattr(oauth, "save_config", save_config)

    def apply_failed(config: Config) -> None:
        config.default_model = "provider/failed"
        failed_apply_entered.set()
        if not allow_failed_save.wait(timeout=5):
            raise TimeoutError("failed-login test barrier timed out")

    def apply_successful(config: Config) -> None:
        config.default_model = "provider/successful"
        successful_apply_entered.set()

    failed_task = asyncio.create_task(
        oauth.persist_login(
            Config(is_from_default_location=True),
            ref,
            _token("failed-access", "failed-refresh"),
            apply_failed,
        )
    )
    assert await asyncio.to_thread(failed_apply_entered.wait, 5)

    successful_task = asyncio.create_task(
        oauth.persist_login(
            Config(is_from_default_location=True),
            ref,
            _token("successful-access", "successful-refresh"),
            apply_successful,
        )
    )
    _ = await asyncio.to_thread(successful_apply_entered.wait, 1)
    allow_failed_save.set()

    results = await asyncio.gather(failed_task, successful_task, return_exceptions=True)
    assert any(isinstance(result, OSError) for result in results)
    assert any(result is None for result in results)

    stored = oauth.load_tokens(ref)
    assert stored is not None
    assert stored.access_token == "successful-access"
    assert stored.refresh_token == "successful-refresh"


@pytest.mark.asyncio
async def test_persist_login_surfaces_credential_rollback_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="file", key="oauth/rollback-provider")
    oauth.save_tokens(ref, _token("old-access", "old-refresh"))

    original_save_tokens = oauth.save_tokens
    save_calls = 0

    def fail_rollback(target_ref: OAuthRef, token: OAuthToken) -> OAuthRef:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise OSError("rollback denied")
        return original_save_tokens(target_ref, token)

    def fail_config_save(_config: Config) -> None:
        raise OSError("disk full")

    errors: list[str] = []
    monkeypatch.setattr(oauth, "save_tokens", fail_rollback)
    monkeypatch.setattr(oauth, "save_config", fail_config_save)
    monkeypatch.setattr(
        oauth,
        "logger",
        SimpleNamespace(error=lambda message, **_kwargs: errors.append(message)),
    )

    with pytest.raises(
        oauth.OAuthPersistenceError,
        match="credential rollback also failed",
    ):
        await oauth.persist_login(
            Config(is_from_default_location=True),
            ref,
            _token("new-access", "new-refresh"),
            lambda _config: None,
        )

    assert errors == ["Failed to roll back OAuth login persistence: {error}"]


@pytest.mark.asyncio
async def test_persist_logout_surfaces_delete_failure_and_restores_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="file", key="oauth/logout-provider")
    oauth.save_tokens(ref, _token("access", "refresh"))
    config = Config(is_from_default_location=True)
    config.default_model = "provider/model"
    saved_defaults: list[str] = []

    monkeypatch.setattr(oauth, "save_config", lambda cfg: saved_defaults.append(cfg.default_model))

    def fail_delete(_ref: OAuthRef) -> None:
        raise OSError("delete denied")

    monkeypatch.setattr(oauth, "delete_tokens", fail_delete)

    with pytest.raises(oauth.OAuthPersistenceError, match="logout was rolled back"):
        await oauth.persist_logout(
            config,
            ref,
            lambda cfg: setattr(cfg, "default_model", ""),
        )

    assert config.default_model == "provider/model"
    assert saved_defaults == ["", "provider/model"]


def test_nested_oauth_ref_has_distinct_credential_and_lock_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))

    flat_key = "oauth/xai"
    nested_key = "oauth/snowflake-cortex/xai"

    assert oauth._credentials_path(flat_key).name == "xai.json"
    assert oauth._credentials_lock_path(flat_key).name == "xai.lock"
    assert oauth._credentials_path(nested_key) != oauth._credentials_path(flat_key)
    assert oauth._credentials_lock_path(nested_key) != oauth._credentials_lock_path(flat_key)
