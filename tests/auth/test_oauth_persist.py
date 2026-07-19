"""Atomicity guarantees for the shared OAuth persistence helpers.

These pin the rollback contract of ``persist_login`` — in particular that a
re-login whose config save fails must not destroy a still-valid existing
credential (only a fresh login with no prior token deletes on failure).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import keyring.errors
import pytest
from pydantic import SecretStr

from pythinker_code.auth import oauth
from pythinker_code.auth.oauth import OAuthToken
from pythinker_code.config import (
    Config,
    LLMProvider,
    OAuthRef,
    get_config_file,
    load_config,
    save_config,
)
from pythinker_code.utils.io import file_lock


def _token(access: str, refresh: str) -> OAuthToken:
    return OAuthToken(
        access_token=access,
        refresh_token=refresh,
        expires_at=9_999_999_999.0,
        scope="test-scope",
        token_type="Bearer",
    )


def _provider(name: str, *, oauth_ref: OAuthRef | None = None) -> LLMProvider:
    return LLMProvider(
        type="openai_legacy",
        base_url=f"https://{name}.example/v1",
        api_key=SecretStr(""),
        oauth=oauth_ref,
    )


def _stale_caller_with_authoritative_provider() -> tuple[Config, Config, Config]:
    save_config(Config(is_from_default_location=True))
    caller = load_config(get_config_file())
    caller_before = caller.model_copy(deep=True)
    authoritative = load_config(get_config_file())
    authoritative.providers["managed:authoritative"] = _provider("authoritative")
    save_config(authoritative)
    return caller, caller_before, authoritative


@pytest.mark.asyncio
async def test_persistence_config_lock_contention_is_bounded_and_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    save_config(Config(is_from_default_location=True))
    ref = OAuthRef(storage="file", key="oauth/contended-provider")
    lock_entered = threading.Event()
    release_lock = threading.Event()

    def hold_config_lock() -> None:
        with file_lock(get_config_file()):
            lock_entered.set()
            if not release_lock.wait(timeout=5):
                raise TimeoutError("config-lock test barrier timed out")

    holder = threading.Thread(target=hold_config_lock)
    holder.start()
    assert lock_entered.wait(timeout=5)
    monkeypatch.setattr(oauth, "_PERSISTENCE_LOCK_TIMEOUT_SECONDS", 0.05, raising=False)
    timer = threading.Timer(0.5, release_lock.set)
    timer.start()

    started = time.monotonic()
    try:
        with pytest.raises(oauth.OAuthPersistenceError) as caught:
            await oauth.persist_login(
                Config(is_from_default_location=True),
                ref,
                _token("new-access", "new-refresh"),
                lambda cfg: cfg.providers.__setitem__(
                    "managed:contended-provider", _provider("contended", oauth_ref=ref)
                ),
            )
    finally:
        release_lock.set()
        timer.cancel()
        holder.join(timeout=5)

    assert time.monotonic() - started < 0.4
    assert str(tmp_path) not in str(caught.value)
    assert oauth.load_tokens(ref) is None
    assert "managed:contended-provider" not in load_config(get_config_file()).providers


@pytest.mark.asyncio
async def test_cancel_before_persistence_mutation_aborts_worker_without_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    save_config(Config(is_from_default_location=True))
    ref = OAuthRef(storage="file", key="oauth/pre-mutation-cancel")
    lock_entered = threading.Event()
    release_lock = threading.Event()
    apply_called = threading.Event()

    def hold_config_lock() -> None:
        with file_lock(get_config_file()):
            lock_entered.set()
            if not release_lock.wait(timeout=5):
                raise TimeoutError("config-lock test barrier timed out")

    holder = threading.Thread(target=hold_config_lock)
    holder.start()
    assert lock_entered.wait(timeout=5)

    def apply_config(cfg: Config) -> None:
        apply_called.set()
        cfg.providers["managed:cancelled"] = _provider("cancelled", oauth_ref=ref)

    task = asyncio.create_task(
        oauth.persist_login(
            Config(is_from_default_location=True),
            ref,
            _token("cancelled-access", "cancelled-refresh"),
            apply_config,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)
    completed_while_blocked = task.done()
    release_lock.set()
    holder.join(timeout=5)

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.to_thread(apply_called.wait, 0.5)

    assert not completed_while_blocked
    assert not apply_called.is_set()
    assert oauth.load_tokens(ref) is None
    assert "managed:cancelled" not in load_config(get_config_file()).providers


@pytest.mark.asyncio
async def test_cancel_after_persistence_mutation_waits_for_atomic_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    save_config(Config(is_from_default_location=True))
    ref = OAuthRef(storage="file", key="oauth/post-mutation-cancel")
    mutation_entered = threading.Event()
    release_mutation = threading.Event()

    def apply_config(cfg: Config) -> None:
        cfg.providers["managed:completed"] = _provider("completed", oauth_ref=ref)
        mutation_entered.set()
        if not release_mutation.wait(timeout=5):
            raise TimeoutError("mutation test barrier timed out")

    task = asyncio.create_task(
        oauth.persist_login(
            Config(is_from_default_location=True),
            ref,
            _token("completed-access", "completed-refresh"),
            apply_config,
        )
    )
    assert await asyncio.to_thread(mutation_entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.05)
    completed_while_mutating = task.done()
    release_mutation.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    committed = load_config(get_config_file())
    stored = oauth.load_tokens(ref)
    assert not completed_while_mutating
    assert "managed:completed" in committed.providers
    assert stored is not None
    assert stored.access_token == "completed-access"


@pytest.mark.asyncio
async def test_cancelled_persistence_surfaces_worker_failure_with_both_causes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    save_config(Config(is_from_default_location=True))
    ref = OAuthRef(storage="file", key="oauth/cancelled-failure")
    mutation_entered = threading.Event()
    release_mutation = threading.Event()
    sensitive_detail = f"disk failure at {tmp_path}"

    def fail_apply(_config: Config) -> None:
        mutation_entered.set()
        if not release_mutation.wait(timeout=5):
            raise TimeoutError("mutation test barrier timed out")
        raise OSError(sensitive_detail)

    task = asyncio.create_task(
        oauth.persist_login(
            Config(is_from_default_location=True),
            ref,
            _token("cancelled-access", "cancelled-refresh"),
            fail_apply,
        )
    )
    assert await asyncio.to_thread(mutation_entered.wait, 5)
    task.cancel()
    release_mutation.set()

    with pytest.raises(oauth.OAuthPersistenceError, match="cancellation") as caught:
        await task

    assert sensitive_detail not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    causes = caught.value.__cause__
    assert isinstance(causes, BaseExceptionGroup)
    assert any(isinstance(exc, asyncio.CancelledError) for exc in causes.exceptions)
    assert any(isinstance(exc, OSError) for exc in causes.exceptions)
    assert oauth.load_tokens(ref) is None
    assert load_config(get_config_file()).providers == {}


@pytest.mark.asyncio
async def test_login_and_logout_fail_closed_on_shared_credential_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(oauth, "_CROSS_PROCESS_LOCK_RETRIES", 0)
    ref = OAuthRef(storage="file", key="oauth/shared-lock")
    provider_key = "managed:shared-lock"
    initial = Config(
        is_from_default_location=True,
        providers={provider_key: _provider("shared", oauth_ref=ref)},
    )
    save_config(initial)
    oauth.save_tokens(ref, _token("old-access", "old-refresh"))
    lock = oauth._CrossProcessLock(ref.key)
    assert lock._acquire()

    try:
        with pytest.raises(oauth.OAuthPersistenceError, match="credential lock"):
            await oauth.persist_login(
                load_config(get_config_file()),
                ref,
                _token("new-access", "new-refresh"),
                lambda cfg: cfg.providers.__setitem__(
                    provider_key, _provider("new", oauth_ref=ref)
                ),
            )
        with pytest.raises(oauth.OAuthPersistenceError, match="credential lock"):
            await oauth.persist_logout(
                load_config(get_config_file()),
                ref,
                lambda cfg: cfg.providers.__delitem__(provider_key),
            )
    finally:
        lock.release()

    committed = load_config(get_config_file())
    stored = oauth.load_tokens(ref)
    assert provider_key in committed.providers
    assert stored is not None
    assert stored.access_token == "old-access"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_key", "new_key", "expected_order"),
    [
        ("oauth/z-old", "oauth/a-new", ["oauth/a-new", "oauth/z-old"]),
        ("oauth/shared", "oauth/shared", ["oauth/shared"]),
    ],
    ids=["sorted", "deduplicated"],
)
async def test_replacement_locks_old_and_new_credential_keys_in_sorted_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    old_key: str,
    new_key: str,
    expected_order: list[str],
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    provider_key = "managed:replacement-order"
    old_ref = OAuthRef(storage="file", key=old_key)
    new_ref = OAuthRef(storage="file", key=new_key)
    config = Config(
        is_from_default_location=True,
        providers={provider_key: _provider("old", oauth_ref=old_ref)},
    )
    save_config(config)
    oauth.save_tokens(old_ref, _token("old-access", "old-refresh"))
    events: list[tuple[str, str]] = []

    class RecordingLock:
        def __init__(self, key: str) -> None:
            self.key = key

        def acquire_with_retry_sync(self) -> bool:
            events.append(("acquire", self.key))
            return True

        def release(self) -> None:
            events.append(("release", self.key))

    monkeypatch.setattr(oauth, "_CrossProcessLock", RecordingLock)

    await oauth.persist_login(
        load_config(get_config_file()),
        new_ref,
        _token("new-access", "new-refresh"),
        lambda cfg: cfg.providers.__setitem__(provider_key, _provider("new", oauth_ref=new_ref)),
        replace_provider_key=provider_key,
    )

    assert [key for event, key in events if event == "acquire"] == expected_order
    assert [key for event, key in events if event == "release"] == list(reversed(expected_order))


@pytest.mark.asyncio
async def test_persist_login_merges_stale_config_under_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    save_config(Config(is_from_default_location=True))
    first_config = load_config(get_config_file())
    second_config = load_config(get_config_file())
    first_ref = OAuthRef(storage="file", key="oauth/first-provider")
    second_ref = OAuthRef(storage="file", key="oauth/second-provider")

    await oauth.persist_login(
        first_config,
        first_ref,
        _token("first-access", "first-refresh"),
        lambda cfg: cfg.providers.__setitem__(
            "managed:first-provider", _provider("first", oauth_ref=first_ref)
        ),
    )
    await oauth.persist_login(
        second_config,
        second_ref,
        _token("second-access", "second-refresh"),
        lambda cfg: cfg.providers.__setitem__(
            "managed:second-provider", _provider("second", oauth_ref=second_ref)
        ),
    )

    committed = load_config(get_config_file())
    assert set(committed.providers) == {
        "managed:first-provider",
        "managed:second-provider",
    }
    assert second_config == committed


@pytest.mark.asyncio
async def test_persist_config_change_merges_stale_config_under_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    save_config(Config(is_from_default_location=True))
    first_config = load_config(get_config_file())
    second_config = load_config(get_config_file())

    await oauth.persist_config_change(
        first_config,
        lambda cfg: cfg.providers.__setitem__("managed:first-provider", _provider("first")),
    )
    await oauth.persist_config_change(
        second_config,
        lambda cfg: cfg.providers.__setitem__("managed:second-provider", _provider("second")),
    )

    committed = load_config(get_config_file())
    assert set(committed.providers) == {
        "managed:first-provider",
        "managed:second-provider",
    }
    assert second_config == committed


@pytest.mark.asyncio
async def test_failed_login_leaves_stale_caller_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    caller, caller_before, authoritative = _stale_caller_with_authoritative_provider()
    ref = OAuthRef(storage="file", key="oauth/failed-provider")

    def fail_config_save(_config: Config) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(oauth, "save_config", fail_config_save)

    with pytest.raises(OSError, match="disk full"):
        await oauth.persist_login(
            caller,
            ref,
            _token("failed-access", "failed-refresh"),
            lambda cfg: cfg.providers.__setitem__(
                "managed:failed-provider", _provider("failed", oauth_ref=ref)
            ),
        )

    assert caller == caller_before
    assert load_config(get_config_file()) == authoritative


@pytest.mark.asyncio
async def test_replaced_credential_cleanup_failure_rolls_back_config_and_both_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    provider_key = "managed:account-scoped"
    old_ref = OAuthRef(storage="file", key="oauth/account-scoped/old")
    new_ref = OAuthRef(storage="file", key="oauth/account-scoped/new")

    save_config(Config(is_from_default_location=True))
    caller = load_config(get_config_file())
    caller_before = caller.model_copy(deep=True)
    authoritative = load_config(get_config_file())
    authoritative.providers[provider_key] = _provider("old", oauth_ref=old_ref)
    save_config(authoritative)
    oauth.save_tokens(old_ref, _token("old-access", "old-refresh"))
    oauth.save_tokens(new_ref, _token("previous-new-access", "previous-new-refresh"))

    original_delete_tokens = oauth.delete_tokens
    deleted: list[OAuthRef] = []

    def delete_then_fail(ref: OAuthRef) -> None:
        original_delete_tokens(ref)
        deleted.append(ref)
        raise OSError("cleanup denied")

    monkeypatch.setattr(oauth, "delete_tokens", delete_then_fail)

    with pytest.raises(
        oauth.OAuthPersistenceError,
        match="credential cleanup failed; login was rolled back",
    ):
        await oauth.persist_login(
            caller,
            new_ref,
            _token("attempted-access", "attempted-refresh"),
            lambda cfg: cfg.providers.__setitem__(
                provider_key, _provider("new", oauth_ref=new_ref)
            ),
            replace_provider_key=provider_key,
        )

    assert deleted == [old_ref]
    assert load_config(get_config_file()) == authoritative
    restored_old = oauth.load_tokens(old_ref)
    restored_new = oauth.load_tokens(new_ref)
    assert restored_old is not None
    assert restored_old.access_token == "old-access"
    assert restored_new is not None
    assert restored_new.access_token == "previous-new-access"
    assert caller == caller_before


@pytest.mark.asyncio
async def test_replacement_with_same_credential_key_keeps_new_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(oauth, "_delete_from_keyring", lambda _key: None)
    provider_key = "managed:account-scoped"
    credential_key = "oauth/account-scoped/shared"
    old_ref = OAuthRef(storage="keyring", key=credential_key)
    new_ref = OAuthRef(storage="file", key=credential_key)
    oauth.save_tokens(new_ref, _token("old-access", "old-refresh"))
    authoritative = Config(
        is_from_default_location=True,
        providers={provider_key: _provider("old", oauth_ref=old_ref)},
    )
    save_config(authoritative)
    caller = load_config(get_config_file())

    await oauth.persist_login(
        caller,
        new_ref,
        _token("new-access", "new-refresh"),
        lambda cfg: cfg.providers.__setitem__(provider_key, _provider("new", oauth_ref=new_ref)),
        replace_provider_key=provider_key,
    )

    committed = load_config(get_config_file())
    committed_ref = committed.providers[provider_key].oauth
    assert committed_ref is not None
    assert committed_ref == new_ref
    stored = oauth.load_tokens(committed_ref)
    assert stored is not None
    assert stored.access_token == "new-access"
    assert caller == committed


def test_credential_transaction_locks_are_reentrant_same_thread(tmp_path, monkeypatch) -> None:
    """A thread already holding a credential key's lock can re-enter it without
    deadlocking — fcntl.flock / msvcrt.locking deny a second same-file
    acquisition within one process, so reentrancy is tracked in Python. Guards
    the keyring-migration self-deadlock (load_tokens inside a persist transaction).
    """
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    key = "oauth/reentrant-provider"
    counts = oauth._held_credential_key_counts()
    assert counts.get(key, 0) == 0
    with oauth._credential_transaction_locks([key]):
        assert counts.get(key, 0) == 1
        with oauth._credential_transaction_locks([key]):
            # Reentrant no-op acquisition; must not block on the same file lock.
            assert counts.get(key, 0) == 2
        # Inner exit decrements but the outer hold (and OS lock) remains.
        assert counts.get(key, 0) == 1
    assert counts.get(key, 0) == 0


def test_load_tokens_keyring_migration_reentrant_under_held_lock(tmp_path, monkeypatch) -> None:
    """The keyring read-copy-delete migration in load_tokens() is serialized under
    the credential lock (so it cannot race persist), and re-enters safely when the
    caller already holds that key's lock (the persist_login -> load_tokens path).
    Pre-fix this path self-deadlocked on the non-reentrant cross-process lock.
    """
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    key = "oauth/keyring-migrate"
    migrated_token = _token("keyring-access", "keyring-refresh")
    monkeypatch.setattr(oauth, "_load_from_keyring", lambda _k: migrated_token)
    deleted: list[str] = []
    monkeypatch.setattr(oauth, "_delete_from_keyring", deleted.append)
    ref = OAuthRef(storage="keyring", key=key)

    with oauth._credential_transaction_locks([key]):
        result = oauth.load_tokens(ref)  # would self-deadlock without reentrancy

    assert result == migrated_token
    assert deleted == [key]  # keyring entry removed as part of the migration
    # Token now lives on the authoritative file copy.
    assert oauth.load_tokens(OAuthRef(storage="file", key=key)) == migrated_token


@pytest.mark.asyncio
async def test_old_token_rollback_failure_preserves_committed_new_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    provider_key = "managed:account-scoped"
    old_ref = OAuthRef(storage="file", key="oauth/account-scoped/old")
    new_ref = OAuthRef(storage="file", key="oauth/account-scoped/new")
    save_config(Config(is_from_default_location=True))
    caller = load_config(get_config_file())
    caller_before = caller.model_copy(deep=True)
    authoritative = load_config(get_config_file())
    authoritative.providers[provider_key] = _provider("old", oauth_ref=old_ref)
    save_config(authoritative)
    oauth.save_tokens(old_ref, _token("old-access", "old-refresh"))

    cleanup_diagnostic = "cleanup denied for old-secret"
    rollback_diagnostic = "old token restore denied for old-secret"
    original_delete_tokens = oauth.delete_tokens
    original_save_tokens = oauth.save_tokens

    def delete_old_then_fail(ref: OAuthRef) -> None:
        original_delete_tokens(ref)
        if ref.key == old_ref.key:
            raise OSError(cleanup_diagnostic)

    def fail_old_token_restore(ref: OAuthRef, token: OAuthToken) -> OAuthRef:
        if ref.key == old_ref.key:
            raise OSError(rollback_diagnostic)
        return original_save_tokens(ref, token)

    monkeypatch.setattr(oauth, "delete_tokens", delete_old_then_fail)
    monkeypatch.setattr(oauth, "save_tokens", fail_old_token_restore)

    with pytest.raises(oauth.OAuthPersistenceError) as caught:
        await oauth.persist_login(
            caller,
            new_ref,
            _token("new-access", "new-refresh"),
            lambda cfg: cfg.providers.__setitem__(
                provider_key, _provider("new", oauth_ref=new_ref)
            ),
            replace_provider_key=provider_key,
        )

    assert cleanup_diagnostic not in str(caught.value)
    assert rollback_diagnostic not in str(caught.value)
    committed = load_config(get_config_file())
    committed_ref = committed.providers[provider_key].oauth
    assert committed_ref is not None
    assert committed_ref == new_ref
    stored = oauth.load_tokens(committed_ref)
    assert stored is not None
    assert stored.access_token == "new-access"
    assert caller == caller_before
    assert "replaced credential rollback also failed" in str(caught.value)


@pytest.mark.asyncio
async def test_config_rollback_failure_preserves_committed_new_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    provider_key = "managed:account-scoped"
    old_ref = OAuthRef(storage="file", key="oauth/account-scoped/old")
    new_ref = OAuthRef(storage="file", key="oauth/account-scoped/new")
    save_config(Config(is_from_default_location=True))
    caller = load_config(get_config_file())
    caller_before = caller.model_copy(deep=True)
    authoritative = load_config(get_config_file())
    authoritative.providers[provider_key] = _provider("old", oauth_ref=old_ref)
    save_config(authoritative)
    oauth.save_tokens(old_ref, _token("old-access", "old-refresh"))

    cleanup_diagnostic = "cleanup denied for old-secret"
    rollback_diagnostic = "config restore denied for old-secret"
    original_delete_tokens = oauth.delete_tokens
    original_save_config = oauth.save_config
    save_calls = 0

    def delete_old_then_fail(ref: OAuthRef) -> None:
        original_delete_tokens(ref)
        if ref.key == old_ref.key:
            raise OSError(cleanup_diagnostic)

    def fail_config_rollback(config: Config) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise OSError(rollback_diagnostic)
        original_save_config(config)

    monkeypatch.setattr(oauth, "delete_tokens", delete_old_then_fail)
    monkeypatch.setattr(oauth, "save_config", fail_config_rollback)

    with pytest.raises(oauth.OAuthPersistenceError) as caught:
        await oauth.persist_login(
            caller,
            new_ref,
            _token("new-access", "new-refresh"),
            lambda cfg: cfg.providers.__setitem__(
                provider_key, _provider("new", oauth_ref=new_ref)
            ),
            replace_provider_key=provider_key,
        )

    assert cleanup_diagnostic not in str(caught.value)
    assert rollback_diagnostic not in str(caught.value)
    committed = load_config(get_config_file())
    committed_ref = committed.providers[provider_key].oauth
    assert committed_ref is not None
    assert committed_ref == new_ref
    stored = oauth.load_tokens(committed_ref)
    assert stored is not None
    assert stored.access_token == "new-access"
    restored_old = oauth.load_tokens(old_ref)
    assert restored_old is not None
    assert restored_old.access_token == "old-access"
    assert caller == caller_before
    assert "configuration rollback also failed" in str(caught.value)


@pytest.mark.asyncio
async def test_failed_config_change_leaves_stale_caller_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    caller, caller_before, authoritative = _stale_caller_with_authoritative_provider()

    def fail_config_save(_config: Config) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(oauth, "save_config", fail_config_save)

    with pytest.raises(OSError, match="disk full"):
        await oauth.persist_config_change(
            caller,
            lambda cfg: cfg.providers.__setitem__("managed:failed", _provider("failed")),
        )

    assert caller == caller_before
    assert load_config(get_config_file()) == authoritative


@pytest.mark.asyncio
async def test_failed_logout_rollback_leaves_stale_caller_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    caller, caller_before, _authoritative = _stale_caller_with_authoritative_provider()
    ref = OAuthRef(storage="file", key="oauth/authoritative")
    oauth.save_tokens(ref, _token("access", "refresh"))
    original_save_config = oauth.save_config
    save_calls = 0

    def fail_config_rollback(config: Config) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise OSError("rollback denied")
        original_save_config(config)

    def fail_delete(_ref: OAuthRef) -> None:
        raise OSError("delete denied")

    def remove_authoritative(config: Config) -> None:
        del config.providers["managed:authoritative"]

    monkeypatch.setattr(oauth, "save_config", fail_config_rollback)
    monkeypatch.setattr(oauth, "delete_tokens", fail_delete)

    with pytest.raises(
        oauth.OAuthPersistenceError,
        match="configuration rollback also failed",
    ):
        await oauth.persist_logout(
            caller,
            ref,
            remove_authoritative,
        )

    assert caller == caller_before


@pytest.mark.asyncio
async def test_persist_logout_resolves_authoritative_provider_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    provider_key = "managed:account-scoped"
    stale_ref = OAuthRef(storage="file", key="oauth/account-scoped/stale")
    authoritative_ref = OAuthRef(storage="file", key="oauth/account-scoped/authoritative")
    caller = Config(
        is_from_default_location=True,
        providers={provider_key: _provider("stale", oauth_ref=stale_ref)},
    )
    authoritative = Config(
        is_from_default_location=True,
        providers={provider_key: _provider("authoritative", oauth_ref=authoritative_ref)},
    )
    save_config(authoritative)
    oauth.save_tokens(stale_ref, _token("stale-access", "stale-refresh"))
    oauth.save_tokens(authoritative_ref, _token("authoritative-access", "authoritative-refresh"))

    def remove_provider(config: Config) -> None:
        config.providers.pop(provider_key, None)

    await oauth.persist_logout(
        caller,
        stale_ref,
        remove_provider,
        provider_key=provider_key,
    )

    assert provider_key not in load_config(get_config_file()).providers
    assert oauth.load_tokens(authoritative_ref) is None
    stale_token = oauth.load_tokens(stale_ref)
    assert stale_token is not None
    assert stale_token.access_token == "stale-access"
    assert caller == load_config(get_config_file())


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


@pytest.mark.asyncio
async def test_persist_logout_restores_deleted_credential_before_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    provider_key = "managed:logout-rollback"
    ref = OAuthRef(storage="file", key="oauth/logout-rollback")
    original_token = _token("original-access", "original-refresh")
    config = Config(
        is_from_default_location=True,
        providers={provider_key: _provider("logout", oauth_ref=ref)},
    )
    save_config(config)
    oauth.save_tokens(ref, original_token)
    original_save_config = oauth.save_config
    rollback_order_verified = False
    sensitive_detail = f"delete backend failed at {tmp_path}"

    def delete_then_raise(target_ref: OAuthRef) -> None:
        oauth._delete_from_file(target_ref.key)
        raise OSError(sensitive_detail)

    def observe_config_save(candidate: Config) -> None:
        nonlocal rollback_order_verified
        if provider_key in candidate.providers:
            assert oauth._load_from_file(ref.key) == original_token
            rollback_order_verified = True
        original_save_config(candidate)

    monkeypatch.setattr(oauth, "delete_tokens", delete_then_raise)
    monkeypatch.setattr(oauth, "save_config", observe_config_save)

    with pytest.raises(oauth.OAuthPersistenceError, match="logout was rolled back") as caught:
        await oauth.persist_logout(
            config,
            ref,
            lambda cfg: cfg.providers.__delitem__(provider_key),
        )

    assert sensitive_detail not in str(caught.value)
    assert provider_key in load_config(get_config_file()).providers
    assert oauth._load_from_file(ref.key) == original_token
    assert provider_key in config.providers
    assert rollback_order_verified


@pytest.mark.asyncio
async def test_persist_logout_credential_restore_failure_keeps_logout_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    provider_key = "managed:partial-logout"
    ref = OAuthRef(storage="file", key="oauth/partial-logout")
    config = Config(
        is_from_default_location=True,
        providers={provider_key: _provider("partial", oauth_ref=ref)},
    )
    save_config(config)
    oauth.save_tokens(ref, _token("original-access", "original-refresh"))
    delete_detail = f"delete backend failed at {tmp_path}"
    restore_detail = f"restore backend failed at {tmp_path}"

    def delete_then_raise(target_ref: OAuthRef) -> None:
        oauth._delete_from_file(target_ref.key)
        raise OSError(delete_detail)

    def fail_restore(_ref: OAuthRef, _token: OAuthToken | None) -> None:
        raise OSError(restore_detail)

    monkeypatch.setattr(oauth, "delete_tokens", delete_then_raise)
    monkeypatch.setattr(oauth, "_restore_token_snapshot", fail_restore)

    with pytest.raises(oauth.OAuthPersistenceError, match="configuration was removed") as caught:
        await oauth.persist_logout(
            config,
            ref,
            lambda cfg: cfg.providers.__delitem__(provider_key),
        )

    assert delete_detail not in str(caught.value)
    assert restore_detail not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert provider_key not in load_config(get_config_file()).providers
    assert oauth._load_from_file(ref.key) is None
    assert provider_key not in config.providers


@pytest.mark.asyncio
async def test_persist_logout_without_prior_credential_does_not_retry_deletion(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    provider_key = "managed:missing-logout-token"
    ref = OAuthRef(storage="file", key="oauth/missing-logout-token")
    config = Config(
        is_from_default_location=True,
        providers={provider_key: _provider("missing", oauth_ref=ref)},
    )
    save_config(config)
    delete_calls = 0

    def fail_delete(_ref: OAuthRef) -> None:
        nonlocal delete_calls
        delete_calls += 1
        raise OSError("delete failed")

    monkeypatch.setattr(oauth, "delete_tokens", fail_delete)

    with pytest.raises(oauth.OAuthPersistenceError, match="logout was rolled back"):
        await oauth.persist_logout(
            config,
            ref,
            lambda cfg: cfg.providers.__delitem__(provider_key),
        )

    assert delete_calls == 1
    assert provider_key in load_config(get_config_file()).providers
    assert oauth._load_from_file(ref.key) is None


def test_nested_oauth_ref_has_distinct_credential_and_lock_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))

    canonical_key = "oauth/lowercase-provider"
    formerly_colliding_flat_key = "oauth/v2-YS9i"
    nested_key = "oauth/a/b"
    case_variant_nested_key = "oauth/a/G"
    unsafe_flat_key = "oauth/account name"
    non_prefixed_key = "lowercase-provider"
    uppercase_key = "oauth/LOWERCASE-PROVIDER"
    empty_key = ""
    credentials_dir = tmp_path / "credentials"

    assert oauth._credentials_path(canonical_key) == credentials_dir / "lowercase-provider.json"
    assert (
        oauth._credentials_lock_path(canonical_key) == credentials_dir / "lowercase-provider.lock"
    )
    assert oauth._credentials_path(nested_key) != oauth._credentials_path(
        formerly_colliding_flat_key
    )
    assert oauth._credentials_lock_path(nested_key) != oauth._credentials_lock_path(
        formerly_colliding_flat_key
    )
    assert oauth._credentials_path(nested_key).parent == credentials_dir / "v2"
    assert oauth._credentials_lock_path(nested_key).parent == credentials_dir / "v2"
    assert oauth._credentials_path(nested_key).stem == oauth._credentials_lock_path(nested_key).stem
    assert (
        str(oauth._credentials_path(nested_key)).casefold()
        != str(oauth._credentials_path(case_variant_nested_key)).casefold()
    )
    assert (
        str(oauth._credentials_lock_path(nested_key)).casefold()
        != str(oauth._credentials_lock_path(case_variant_nested_key)).casefold()
    )
    assert oauth._credentials_path(unsafe_flat_key).parent == credentials_dir / "v2"
    assert oauth._credentials_lock_path(unsafe_flat_key).parent == credentials_dir / "v2"
    for aliased_key in (non_prefixed_key, uppercase_key, formerly_colliding_flat_key):
        assert oauth._credentials_path(aliased_key).parent == credentials_dir / "v2"
        assert oauth._credentials_lock_path(aliased_key).parent == credentials_dir / "v2"
        assert oauth._credentials_path(aliased_key) != oauth._credentials_path(canonical_key)
        assert oauth._credentials_lock_path(aliased_key) != oauth._credentials_lock_path(
            canonical_key
        )
    for windows_unsafe_key in ("oauth/CON", "oauth/con.txt", "oauth/account."):
        assert oauth._credentials_path(windows_unsafe_key).parent == credentials_dir / "v2"
        assert oauth._credentials_lock_path(windows_unsafe_key).parent == credentials_dir / "v2"
    assert oauth._credentials_path(empty_key) == credentials_dir / "v2" / "~.json"
    assert oauth._credentials_lock_path(empty_key) == credentials_dir / "v2" / "~.lock"


def test_keyring_missing_credential_delete_is_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    delete_called = False

    def unexpected_delete(_service: str, _key: str) -> None:
        nonlocal delete_called
        delete_called = True

    monkeypatch.setattr(oauth.keyring, "get_password", lambda _service, _key: None)
    monkeypatch.setattr(oauth.keyring, "delete_password", unexpected_delete)

    oauth._delete_from_keyring("oauth/missing")

    assert not delete_called


def test_keyring_delete_race_to_absent_is_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    reads = iter(["credential-present", None])

    monkeypatch.setattr(oauth.keyring, "get_password", lambda _service, _key: next(reads))

    def raced_delete(_service: str, _key: str) -> None:
        raise keyring.errors.PasswordDeleteError("generic delete failure")

    monkeypatch.setattr(oauth.keyring, "delete_password", raced_delete)

    oauth._delete_from_keyring("oauth/raced")

    with pytest.raises(StopIteration):
        next(reads)


def test_generic_keyring_delete_error_with_confirmed_absence_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential: str | None = "credential-present"
    reads = 0

    def read_keyring(_service: str, _key: str) -> str | None:
        nonlocal reads
        reads += 1
        return credential

    def delete_then_raise(_service: str, _key: str) -> None:
        nonlocal credential
        credential = None
        raise keyring.errors.KeyringError("ambiguous backend failure")

    monkeypatch.setattr(oauth.keyring, "get_password", read_keyring)
    monkeypatch.setattr(oauth.keyring, "delete_password", delete_then_raise)

    oauth._delete_from_keyring("oauth/generic-race")

    assert reads == 2
    assert credential is None


def test_generic_keyring_delete_error_with_confirmed_presence_is_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    reads = 0
    secret_detail = f"ambiguous backend failure at {tmp_path}"

    def read_keyring(_service: str, _key: str) -> str:
        nonlocal reads
        reads += 1
        return "credential-still-present"

    def fail_delete(_service: str, _key: str) -> None:
        raise keyring.errors.KeyringError(secret_detail)

    monkeypatch.setattr(oauth.keyring, "get_password", read_keyring)
    monkeypatch.setattr(oauth.keyring, "delete_password", fail_delete)

    with pytest.raises(oauth.OAuthPersistenceError, match="system keyring") as caught:
        oauth._delete_from_keyring("oauth/generic-present")

    assert reads == 2
    assert secret_detail not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_keyring_delete_verification_failure_is_distinct_and_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    reads = 0
    delete_detail = f"delete failed at {tmp_path}"
    read_detail = f"verification failed at {tmp_path}"

    def read_keyring(_service: str, _key: str) -> str:
        nonlocal reads
        reads += 1
        if reads == 1:
            return "credential-present"
        raise keyring.errors.KeyringError(read_detail)

    def fail_delete(_service: str, _key: str) -> None:
        raise keyring.errors.KeyringError(delete_detail)

    monkeypatch.setattr(oauth.keyring, "get_password", read_keyring)
    monkeypatch.setattr(oauth.keyring, "delete_password", fail_delete)

    with pytest.raises(oauth.OAuthPersistenceError, match="verify") as caught:
        oauth._delete_from_keyring("oauth/unknown-delete")

    assert reads == 2
    assert delete_detail not in str(caught.value)
    assert read_detail not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_keyring_password_delete_error_with_remaining_credential_is_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    secret_detail = f"backend database at {tmp_path} is unavailable"
    monkeypatch.setattr(
        oauth.keyring,
        "get_password",
        lambda _service, _key: "credential-still-present",
    )

    def fail_delete(_service: str, _key: str) -> None:
        raise keyring.errors.PasswordDeleteError(secret_detail)

    monkeypatch.setattr(oauth.keyring, "delete_password", fail_delete)

    with pytest.raises(oauth.OAuthPersistenceError) as caught:
        oauth._delete_from_keyring("oauth/provider")

    assert secret_detail not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert "system keyring" in str(caught.value)


def test_keyring_backend_delete_failure_is_safe_and_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    secret_detail = f"backend database at {tmp_path} is unavailable"
    monkeypatch.setattr(
        oauth.keyring,
        "get_password",
        lambda _service, _key: "credential-still-present",
    )

    def fail_delete(_service: str, _key: str) -> None:
        raise keyring.errors.KeyringError(secret_detail)

    monkeypatch.setattr(oauth.keyring, "delete_password", fail_delete)

    with pytest.raises(oauth.OAuthPersistenceError) as caught:
        oauth._delete_from_keyring("oauth/provider")

    assert secret_detail not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert "system keyring" in str(caught.value)


def test_keyring_backend_read_failure_is_safe_and_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    secret_detail = f"backend database at {tmp_path} is unavailable"
    ref = OAuthRef(storage="keyring", key="oauth/provider")

    def fail_read(_service: str, _key: str) -> str | None:
        raise keyring.errors.KeyringError(secret_detail)

    monkeypatch.setattr(oauth.keyring, "get_password", fail_read)

    with pytest.raises(oauth.OAuthPersistenceError) as caught:
        oauth.load_tokens(ref)

    assert secret_detail not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert "system keyring" in str(caught.value)
    assert not oauth._credentials_path(ref.key).exists()


def test_malformed_keyring_token_is_safe_and_typed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="keyring", key="oauth/malformed-provider")
    sensitive_payload = json.dumps(
        {
            "access_token": "secret-access-token",
            "refresh_token": "secret-refresh-token",
            "expires_at": "not-a-number",
        }
    )
    monkeypatch.setattr(
        oauth.keyring,
        "get_password",
        lambda _service, _key: sensitive_payload,
    )

    with pytest.raises(oauth.OAuthPersistenceError) as caught:
        oauth.load_tokens(ref)

    assert "secret-access-token" not in str(caught.value)
    assert "secret-refresh-token" not in str(caught.value)
    assert "system keyring" in str(caught.value)
    assert not oauth._credentials_path(ref.key).exists()


def _keyring_config(ref: OAuthRef) -> Config:
    return Config(
        is_from_default_location=True,
        providers={"managed:keyring-provider": _provider("keyring", oauth_ref=ref)},
    )


def _keyring_payload(token: OAuthToken) -> str:
    return json.dumps(token.to_dict())


def test_manager_migrates_keyring_credential_and_preserves_authoritative_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="keyring", key="oauth/keyring-provider")
    token = _token("access", "refresh")
    save_config(_keyring_config(ref))
    caller = load_config(get_config_file())
    authoritative = load_config(get_config_file())
    authoritative.providers["managed:concurrent"] = _provider("concurrent")
    save_config(authoritative)
    deleted: list[tuple[str, str]] = []

    monkeypatch.setattr(
        oauth.keyring,
        "get_password",
        lambda service, key: _keyring_payload(token),
    )
    monkeypatch.setattr(
        oauth.keyring,
        "delete_password",
        lambda service, key: deleted.append((service, key)),
    )

    oauth.OAuthManager(caller)

    committed = load_config(get_config_file())
    assert committed.providers["managed:keyring-provider"].oauth == OAuthRef(
        storage="file", key=ref.key
    )
    assert "managed:concurrent" in committed.providers
    assert "managed:concurrent" in caller.providers
    assert (
        caller.providers["managed:keyring-provider"].oauth
        == committed.providers["managed:keyring-provider"].oauth
    )
    assert oauth._load_from_file(ref.key) == token
    assert deleted == [(oauth.KEYRING_SERVICE, ref.key)]


def test_manager_migration_prefers_authoritative_keyring_over_stale_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="keyring", key="oauth/divergent-keyring-provider")
    stale_file_token = _token("stale-file-access", "stale-file-refresh")
    keyring_token = _token("keyring-access", "keyring-refresh")
    save_config(_keyring_config(ref))
    caller = load_config(get_config_file())
    oauth._save_to_file(ref.key, stale_file_token)
    credential: str | None = _keyring_payload(keyring_token)

    monkeypatch.setattr(oauth.keyring, "get_password", lambda _service, _key: credential)

    def delete_keyring(_service: str, _key: str) -> None:
        nonlocal credential
        assert oauth._load_from_file(ref.key) == keyring_token
        credential = None

    monkeypatch.setattr(oauth.keyring, "delete_password", delete_keyring)

    oauth.OAuthManager(caller)

    assert oauth._load_from_file(ref.key) == keyring_token
    assert credential is None
    committed_ref = load_config(get_config_file()).providers["managed:keyring-provider"].oauth
    assert committed_ref == OAuthRef(storage="file", key=ref.key)
    assert caller.providers["managed:keyring-provider"].oauth == committed_ref


def test_manager_keyring_cleanup_failure_restores_previous_file_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="keyring", key="oauth/keyring-cleanup-rollback")
    stale_file_token = _token("stale-file-access", "stale-file-refresh")
    keyring_token = _token("keyring-access", "keyring-refresh")
    save_config(_keyring_config(ref))
    caller = load_config(get_config_file())
    stale_snapshot = json.dumps(
        stale_file_token.to_dict(),
        indent=2,
        sort_keys=True,
    ).encode(encoding="utf-8")
    oauth._credentials_path(ref.key).parent.mkdir(parents=True, exist_ok=True)
    oauth._credentials_path(ref.key).write_bytes(stale_snapshot)
    keyring_payload = _keyring_payload(keyring_token)

    monkeypatch.setattr(
        oauth.keyring,
        "get_password",
        lambda _service, _key: keyring_payload,
    )

    def fail_delete(_service: str, _key: str) -> None:
        assert oauth._load_from_file(ref.key) == keyring_token
        raise keyring.errors.KeyringError(f"sensitive backend at {tmp_path}")

    monkeypatch.setattr(oauth.keyring, "delete_password", fail_delete)

    with pytest.raises(oauth.OAuthPersistenceError, match="rolled back") as caught:
        oauth.OAuthManager(caller)

    assert str(tmp_path) not in str(caught.value)
    assert oauth._load_from_file(ref.key) == stale_file_token
    assert oauth._credentials_path(ref.key).read_bytes() == stale_snapshot
    assert oauth._load_from_keyring(ref.key) == keyring_token
    assert load_config(get_config_file()).providers["managed:keyring-provider"].oauth == ref
    assert caller.providers["managed:keyring-provider"].oauth == ref


def test_manager_keyring_cleanup_unknown_retains_authoritative_file_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="keyring", key="oauth/keyring-cleanup-unknown")
    stale_file_token = _token("stale-file-access", "stale-file-refresh")
    keyring_token = _token("keyring-access", "keyring-refresh")
    save_config(_keyring_config(ref))
    caller = load_config(get_config_file())
    oauth._save_to_file(ref.key, stale_file_token)
    reads = 0
    keyring_payload = _keyring_payload(keyring_token)

    def read_keyring(_service: str, _key: str) -> str:
        nonlocal reads
        reads += 1
        if reads <= 2:
            return keyring_payload
        raise keyring.errors.KeyringError(f"sensitive verification at {tmp_path}")

    def fail_delete(_service: str, _key: str) -> None:
        assert oauth._load_from_file(ref.key) == keyring_token
        raise keyring.errors.KeyringError(f"sensitive delete at {tmp_path}")

    monkeypatch.setattr(oauth.keyring, "get_password", read_keyring)
    monkeypatch.setattr(oauth.keyring, "delete_password", fail_delete)

    with pytest.raises(oauth.OAuthPersistenceError, match="could not be verified") as caught:
        oauth.OAuthManager(caller)

    assert str(tmp_path) not in str(caught.value)
    assert oauth._load_from_file(ref.key) == keyring_token
    assert load_config(get_config_file()).providers["managed:keyring-provider"].oauth == ref
    assert caller.providers["managed:keyring-provider"].oauth == ref


def test_manager_migrates_authoritative_keyring_ref_missing_from_stale_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="keyring", key="oauth/authoritative-keyring-provider")
    token = _token("access", "refresh")
    save_config(Config(is_from_default_location=True))
    caller = load_config(get_config_file())
    authoritative = load_config(get_config_file())
    authoritative.providers["managed:keyring-provider"] = _provider("keyring", oauth_ref=ref)
    save_config(authoritative)

    monkeypatch.setattr(
        oauth.keyring,
        "get_password",
        lambda _service, _key: _keyring_payload(token),
    )
    monkeypatch.setattr(oauth.keyring, "delete_password", lambda _service, _key: None)

    oauth.OAuthManager(caller)

    committed_ref = load_config(get_config_file()).providers["managed:keyring-provider"].oauth
    assert committed_ref == OAuthRef(storage="file", key=ref.key)
    assert caller.providers["managed:keyring-provider"].oauth == committed_ref
    assert oauth._load_from_file(ref.key) == token


def test_manager_keyring_migration_fails_closed_on_credential_lock_contention(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="keyring", key="oauth/locked-migration")
    token = _token("access", "refresh")
    save_config(_keyring_config(ref))
    caller = load_config(get_config_file())
    keyring_read = False

    class ContendedLock:
        def __init__(self, key: str) -> None:
            assert key == ref.key

        def acquire_with_retry_sync(self) -> bool:
            return False

        def release(self) -> None:
            return None

    def read_keyring(_service: str, _key: str) -> str:
        nonlocal keyring_read
        keyring_read = True
        return _keyring_payload(token)

    monkeypatch.setattr(oauth, "_CrossProcessLock", ContendedLock)
    monkeypatch.setattr(oauth.keyring, "get_password", read_keyring)

    with pytest.raises(oauth.OAuthPersistenceError, match="credential lock"):
        oauth.OAuthManager(caller)

    assert not keyring_read
    assert caller.providers["managed:keyring-provider"].oauth == ref
    assert load_config(get_config_file()).providers["managed:keyring-provider"].oauth == ref
    assert not oauth._credentials_path(ref.key).exists()


def test_manager_keeps_keyring_ref_when_credential_is_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="keyring", key="oauth/missing-provider")
    save_config(_keyring_config(ref))
    caller = load_config(get_config_file())
    monkeypatch.setattr(oauth.keyring, "get_password", lambda _service, _key: None)

    oauth.OAuthManager(caller)

    assert caller.providers["managed:keyring-provider"].oauth == ref
    assert load_config(get_config_file()).providers["managed:keyring-provider"].oauth == ref
    assert not oauth._credentials_path(ref.key).exists()
    assert oauth._load_from_keyring(ref.key) is None


def test_manager_keyring_read_failure_does_not_rewrite_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="keyring", key="oauth/read-failure")
    save_config(_keyring_config(ref))
    caller = load_config(get_config_file())

    def fail_read(_service: str, _key: str) -> str | None:
        raise keyring.errors.KeyringError(f"sensitive backend {tmp_path}")

    monkeypatch.setattr(oauth.keyring, "get_password", fail_read)

    with pytest.raises(oauth.OAuthPersistenceError) as caught:
        oauth.OAuthManager(caller)

    assert str(tmp_path) not in str(caught.value)
    assert caller.providers["managed:keyring-provider"].oauth == ref
    assert load_config(get_config_file()).providers["managed:keyring-provider"].oauth == ref
    assert not oauth._credentials_path(ref.key).exists()


def test_manager_keyring_delete_failure_rolls_back_file_and_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="keyring", key="oauth/delete-failure")
    token = _token("access", "refresh")
    save_config(_keyring_config(ref))
    caller = load_config(get_config_file())

    monkeypatch.setattr(
        oauth.keyring, "get_password", lambda _service, _key: _keyring_payload(token)
    )

    def fail_delete(_service: str, _key: str) -> None:
        raise keyring.errors.KeyringError(f"sensitive backend {tmp_path}")

    monkeypatch.setattr(oauth.keyring, "delete_password", fail_delete)

    with pytest.raises(oauth.OAuthPersistenceError) as caught:
        oauth.OAuthManager(caller)

    assert str(tmp_path) not in str(caught.value)
    assert "rolled back" in str(caught.value)
    assert caller.providers["managed:keyring-provider"].oauth == ref
    assert load_config(get_config_file()).providers["managed:keyring-provider"].oauth == ref
    assert not oauth._credentials_path(ref.key).exists()
    assert oauth._load_from_keyring(ref.key) == token


def test_manager_keyring_delete_and_file_rollback_failure_is_combined_and_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="keyring", key="oauth/rollback-failure")
    token = _token("access", "refresh")
    save_config(_keyring_config(ref))
    caller = load_config(get_config_file())

    monkeypatch.setattr(
        oauth.keyring, "get_password", lambda _service, _key: _keyring_payload(token)
    )

    def fail_delete(_service: str, _key: str) -> None:
        raise keyring.errors.KeyringError(f"sensitive backend {tmp_path}")

    def fail_rollback(_key: str) -> None:
        raise OSError(f"sensitive file {tmp_path}")

    monkeypatch.setattr(oauth.keyring, "delete_password", fail_delete)
    monkeypatch.setattr(oauth, "_delete_from_file", fail_rollback)

    with pytest.raises(oauth.OAuthPersistenceError) as caught:
        oauth.OAuthManager(caller)

    assert str(tmp_path) not in str(caught.value)
    assert "rollback also failed" in str(caught.value)
    assert caller.providers["managed:keyring-provider"].oauth == ref
    assert load_config(get_config_file()).providers["managed:keyring-provider"].oauth == ref
    assert oauth._credentials_path(ref.key).exists()
    assert oauth._load_from_keyring(ref.key) == token


def test_manager_keyring_migration_respects_bounded_config_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    ref = OAuthRef(storage="keyring", key="oauth/contended-migration")
    token = _token("access", "refresh")
    save_config(_keyring_config(ref))
    caller = load_config(get_config_file())
    lock_entered = threading.Event()
    release_lock = threading.Event()

    monkeypatch.setattr(
        oauth.keyring, "get_password", lambda _service, _key: _keyring_payload(token)
    )
    monkeypatch.setattr(oauth.keyring, "delete_password", lambda _service, _key: None)
    monkeypatch.setattr(oauth, "_PERSISTENCE_LOCK_TIMEOUT_SECONDS", 0.05)

    def hold_config_lock() -> None:
        with file_lock(get_config_file()):
            lock_entered.set()
            if not release_lock.wait(timeout=5):
                raise TimeoutError("config-lock test barrier timed out")

    holder = threading.Thread(target=hold_config_lock)
    holder.start()
    assert lock_entered.wait(timeout=5)
    try:
        with pytest.raises(oauth.OAuthPersistenceError, match="persistence is busy"):
            oauth.OAuthManager(caller)
    finally:
        release_lock.set()
        holder.join(timeout=5)

    assert caller.providers["managed:keyring-provider"].oauth == ref
    assert load_config(get_config_file()).providers["managed:keyring-provider"].oauth == ref
    assert not oauth._credentials_path(ref.key).exists()
