"""Atomicity guarantees for the shared OAuth persistence helpers.

These pin the rollback contract of ``persist_login`` — in particular that a
re-login whose config save fails must not destroy a still-valid existing
credential (only a fresh login with no prior token deletes on failure).
"""

from __future__ import annotations

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


def test_persist_login_restores_previous_token_on_config_save_failure(
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
        oauth.persist_login(config, ref, _token("new-access", "new-refresh"), apply_config)

    # The previously valid credential must survive an unrelated config-save
    # failure — the re-login attempt failed, but the working token is intact.
    restored = oauth.load_tokens(ref)
    assert restored is not None
    assert restored.access_token == "old-access"
    assert restored.refresh_token == "old-refresh"
    # In-memory config is rolled back to before the mutation.
    assert config.default_model == ""


def test_persist_login_deletes_token_on_fresh_login_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="file", key="oauth/test-provider")

    def boom(_config: Config) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(oauth, "save_config", boom)

    config = Config(is_from_default_location=True)

    with pytest.raises(OSError):
        oauth.persist_login(config, ref, _token("new-access", "new-refresh"), lambda cfg: None)

    # No prior credential existed, so the failed login leaves no orphan token.
    assert oauth.load_tokens(ref) is None


def test_persist_login_persists_both_on_success(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="file", key="oauth/test-provider")

    saved: list[Config] = []
    monkeypatch.setattr(oauth, "save_config", lambda cfg: saved.append(cfg))

    config = Config(is_from_default_location=True)
    oauth.persist_login(
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
