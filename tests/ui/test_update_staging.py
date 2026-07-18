"""Windows update staging: manifest lifecycle, intent gating, pre-start apply."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pythinker_code.ui.shell import update as upd


@pytest.fixture
def staging(monkeypatch, tmp_path: Path) -> Path:
    parent = tmp_path / "windows-update-staging"
    monkeypatch.setattr(upd, "_windows_update_staging_parent", lambda: parent)
    monkeypatch.setattr(upd, "_is_windows", lambda: True)
    return parent


def _stage(
    parent: Path, version: str = "9.9.9", body: bytes = b"installer"
) -> upd.StagedWindowsUpdate:
    import hashlib

    asset_dir = parent / "pythinker-update-test"
    asset_dir.mkdir(parents=True)
    installer = asset_dir / f"PythinkerSetup-{version}.exe"
    installer.write_bytes(body)
    staged = upd.StagedWindowsUpdate(
        version=version,
        installer_path=installer,
        sha256=hashlib.sha256(body).hexdigest(),
        created_at=time.time(),
    )
    assert upd._write_windows_staged_manifest(staged)
    return staged


def test_manifest_round_trip(staging: Path):
    staged = _stage(staging)
    loaded = upd.read_windows_staged_update()
    assert loaded is not None
    assert loaded.version == staged.version
    assert loaded.installer_path == staged.installer_path
    assert loaded.sha256 == staged.sha256


def test_manifest_missing_returns_none(staging: Path):
    assert upd.read_windows_staged_update() is None


def test_malformed_manifest_is_discarded(staging: Path):
    staging.mkdir(parents=True)
    upd._windows_staged_manifest_path().write_text("{not json", encoding="utf-8")
    assert upd.read_windows_staged_update() is None
    assert not upd._windows_staged_manifest_path().exists()


def test_manifest_without_ready_state_is_discarded(staging: Path):
    staged = _stage(staging)
    manifest = upd._windows_staged_manifest_path()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["state"] = "downloading"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert upd.read_windows_staged_update() is None
    assert not staged.installer_path.exists()


@pytest.mark.asyncio
async def test_windows_stage_for_restart_never_spawns_installer(staging: Path, monkeypatch):
    """The core regression: a background update on Windows stages the installer
    and must never launch it or exit the process mid-session."""

    async def fake_fetch(session, asset_name: str, channel: str):
        return "https://example.invalid/asset", "a" * 64

    async def fake_download(session, asset_name: str, download_url: str, destination: Path):
        destination.write_bytes(b"installer")
        return upd.UpdateResult.UPDATED

    def forbidden_installer(asset: Path) -> None:
        raise AssertionError("STAGE_FOR_RESTART must never launch the installer")

    monkeypatch.setattr(upd, "_installed_linux_package_kind", lambda: None)
    monkeypatch.setattr(upd, "native_installer_asset_name", lambda v: f"PythinkerSetup-{v}.exe")
    monkeypatch.setattr(upd, "_fetch_native_release_asset", fake_fetch)
    monkeypatch.setattr(upd, "_download_native_asset", fake_download)
    monkeypatch.setattr(upd, "_verify_sha256", lambda path, expected: True)
    monkeypatch.setattr(upd, "_run_native_installer", forbidden_installer)

    result = await upd._maybe_run_native_update("9.9.9", intent=upd.UpdateIntent.STAGE_FOR_RESTART)

    assert result is upd.UpdateResult.UPDATED
    staged = upd.read_windows_staged_update()
    assert staged is not None
    assert staged.version == "9.9.9"
    assert staged.installer_path.is_file()


@pytest.mark.asyncio
async def test_windows_install_and_exit_launches_installer(staging: Path, monkeypatch):
    launched: list[Path] = []

    async def fake_fetch(session, asset_name: str, channel: str):
        return "https://example.invalid/asset", "a" * 64

    async def fake_download(session, asset_name: str, download_url: str, destination: Path):
        destination.write_bytes(b"installer")
        return upd.UpdateResult.UPDATED

    monkeypatch.setattr(upd, "_installed_linux_package_kind", lambda: None)
    monkeypatch.setattr(upd, "native_installer_asset_name", lambda v: f"PythinkerSetup-{v}.exe")
    monkeypatch.setattr(upd, "_fetch_native_release_asset", fake_fetch)
    monkeypatch.setattr(upd, "_download_native_asset", fake_download)
    monkeypatch.setattr(upd, "_verify_sha256", lambda path, expected: True)

    def fake_spawn(asset: Path) -> bool:
        launched.append(asset)
        return True

    monkeypatch.setattr(upd, "_spawn_detached_windows_installer", fake_spawn)

    with pytest.raises(SystemExit) as exc_info:
        await upd._maybe_run_native_update("9.9.9", intent=upd.UpdateIntent.INSTALL_AND_EXIT)

    assert exc_info.value.code == 0
    assert len(launched) == 1


@pytest.mark.asyncio
async def test_linux_package_stage_for_restart_defers(monkeypatch):
    monkeypatch.setattr(upd, "_installed_linux_package_kind", lambda: "deb")

    def forbidden_install(asset, kind):
        raise AssertionError("background updates must not run package installs")

    monkeypatch.setattr(upd, "_install_linux_package", forbidden_install)

    result = await upd._maybe_run_native_update("9.9.9", intent=upd.UpdateIntent.STAGE_FOR_RESTART)
    assert result is upd.UpdateResult.UPDATE_AVAILABLE


def test_apply_now_rejects_digest_mismatch(staging: Path, monkeypatch):
    staged = _stage(staging)
    staged.installer_path.write_bytes(b"tampered")
    spawned: list[Path] = []
    monkeypatch.setattr(upd, "_spawn_detached_windows_installer", lambda p: spawned.append(p))
    monkeypatch.setattr("pythinker_code.constant.VERSION", "0.1.0")

    assert upd.apply_windows_staged_update_now() is False
    assert spawned == []
    assert upd.read_windows_staged_update() is None


def test_apply_now_rejects_stale_version(staging: Path, monkeypatch):
    _stage(staging, version="0.0.1")
    spawned: list[Path] = []
    monkeypatch.setattr(upd, "_spawn_detached_windows_installer", lambda p: spawned.append(p))

    assert upd.apply_windows_staged_update_now() is False
    assert spawned == []
    assert upd.read_windows_staged_update() is None


def test_apply_now_spawns_and_clears_manifest(staging: Path, monkeypatch):
    staged = _stage(staging)
    spawned: list[Path] = []
    monkeypatch.setattr("pythinker_code.constant.VERSION", "0.1.0")

    def fake_spawn(path: Path) -> bool:
        spawned.append(path)
        return True

    monkeypatch.setattr(upd, "_spawn_detached_windows_installer", fake_spawn)

    assert upd.apply_windows_staged_update_now() is True
    assert spawned == [staged.installer_path]
    assert not upd._windows_staged_manifest_path().exists()


def test_apply_before_start_noop_off_windows(monkeypatch):
    monkeypatch.setattr(upd, "_is_windows", lambda: False)
    assert upd.apply_staged_update_before_start() is False


def test_apply_before_start_respects_kill_switch(staging: Path, monkeypatch):
    _stage(staging)
    monkeypatch.setenv("PYTHINKER_CLI_NO_AUTO_UPDATE", "1")
    assert upd.apply_staged_update_before_start() is False


def test_apply_before_start_applies_valid_stage(staging: Path, monkeypatch):
    _stage(staging)
    monkeypatch.delenv("PYTHINKER_CLI_NO_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(upd, "_is_running_from_source_checkout", lambda: False)
    monkeypatch.setattr("pythinker_code.constant.VERSION", "0.1.0")
    monkeypatch.setattr(upd, "_spawn_detached_windows_installer", lambda p: True)

    assert upd.apply_staged_update_before_start() is True


def test_apply_before_start_fails_closed_on_bad_stage(staging: Path, monkeypatch):
    staged = _stage(staging)
    staged.installer_path.unlink()
    monkeypatch.delenv("PYTHINKER_CLI_NO_AUTO_UPDATE", raising=False)
    monkeypatch.setattr(upd, "_is_running_from_source_checkout", lambda: False)
    monkeypatch.setattr("pythinker_code.constant.VERSION", "0.1.0")

    assert upd.apply_staged_update_before_start() is False
    assert upd.read_windows_staged_update() is None


def test_cleanup_preserves_manifest_referenced_dir(staging: Path):
    staged = _stage(staging)
    stale_dir = staging / "pythinker-update-stale"
    stale_dir.mkdir()
    old = time.time() - upd.WINDOWS_UPDATE_STAGING_MAX_AGE_SECONDS - 10
    import os

    os.utime(stale_dir, (old, old))
    os.utime(staged.installer_path.parent, (old, old))

    upd._cleanup_stale_windows_update_staging()

    assert not stale_dir.exists()
    assert staged.installer_path.exists()


@pytest.mark.asyncio
async def test_stage_for_restart_never_runs_package_upgrade(monkeypatch, tmp_path):
    """Non-native installs (pip/uv/brew): a background update surfaces the
    version but must not run the upgrade subprocess inline."""

    async def fake_get_latest(session):
        return "999.0.0"

    async def fake_unavailable(session, latest_version, upgrade_command):
        return None

    def forbidden_upgrade(command, **kw):
        raise AssertionError("background updates must not run upgrade commands")

    monkeypatch.setattr(upd, "LATEST_VERSION_FILE", tmp_path / "latest.txt")
    monkeypatch.setattr(upd, "_get_latest_version", fake_get_latest)
    monkeypatch.setattr(upd, "_update_candidate_unavailable_reason", fake_unavailable)
    monkeypatch.setattr(upd, "_detect_upgrade_command", lambda: ["pip", "install", "-U", "x"])
    monkeypatch.setattr(upd, "_run_upgrade_command", forbidden_upgrade)

    result = await upd.do_update(print_output=False, intent=upd.UpdateIntent.STAGE_FOR_RESTART)
    assert result is upd.UpdateResult.UPDATE_AVAILABLE


def test_apply_now_preserves_superseded_manifest(staging: Path, monkeypatch):
    """If a newer stage lands between validation and spawn, the newer manifest
    must survive so the next launch applies it."""
    staged = _stage(staging)
    monkeypatch.setattr("pythinker_code.constant.VERSION", "0.1.0")

    def spawn_and_supersede(path: Path) -> bool:
        # Simulate a concurrent process replacing the stage mid-apply.
        newer_dir = staging / "pythinker-update-newer"
        newer_dir.mkdir()
        installer = newer_dir / "PythinkerSetup-10.0.0.exe"
        installer.write_bytes(b"newer")
        import hashlib

        upd._write_windows_staged_manifest(
            upd.StagedWindowsUpdate(
                version="10.0.0",
                installer_path=installer,
                sha256=hashlib.sha256(b"newer").hexdigest(),
                created_at=time.time(),
            )
        )
        return True

    monkeypatch.setattr(upd, "_spawn_detached_windows_installer", spawn_and_supersede)

    assert upd.apply_windows_staged_update_now() is True
    survivor = upd.read_windows_staged_update()
    assert survivor is not None
    assert survivor.version == "10.0.0"
    del staged


@pytest.mark.asyncio
async def test_run_update_job_skips_smoke_check_for_windows_stage(monkeypatch, tmp_path):
    """The smoke check would run the OLD executable and falsely certify the
    staged installer; the staged path must report digest verification instead."""
    from pythinker_code.ui.shell import update_orchestrator as orch

    monkeypatch.setattr(orch, "UPDATE_STATUS_FILE", tmp_path / "update_status.json")
    monkeypatch.setattr(orch, "UPDATE_LOG_FILE", tmp_path / "update.log")
    monkeypatch.setattr(orch, "UPDATE_LOCK_FILE", tmp_path / "update.lock")
    monkeypatch.setattr(orch, "UPDATE_LAST_SUCCESS_FILE", tmp_path / "last_success.json")

    async def fake_do_update(*, print_output, intent, output_callback=None):
        return upd.UpdateResult.UPDATED

    def forbidden_smoke():
        raise AssertionError("smoke check must not run for a Windows staged update")

    monkeypatch.setattr(upd, "do_update", fake_do_update)
    monkeypatch.setattr(orch, "run_post_install_smoke_check", forbidden_smoke)
    monkeypatch.setattr(
        orch,
        "_pending_windows_staged_update",
        lambda: upd.StagedWindowsUpdate(
            version="9.9.9",
            installer_path=Path("X"),
            sha256="a" * 64,
            created_at=time.time(),
        ),
    )

    result = await orch.run_update_job(
        print_output=False, intent=upd.UpdateIntent.STAGE_FOR_RESTART, source="test"
    )

    assert result is upd.UpdateResult.UPDATED
    status = orch.read_update_status()
    assert status is not None
    assert "staged" in (status.message or "").lower()
    assert orch.UPDATE_LAST_SUCCESS_FILE.exists()


def test_apply_now_concurrent_callers_only_one_wins(staging: Path, monkeypatch):
    """Regression: two processes racing to apply the same stage must not both
    pass validation and spawn duplicate installers."""
    staged = _stage(staging)
    spawned: list[Path] = []
    monkeypatch.setattr("pythinker_code.constant.VERSION", "0.1.0")
    monkeypatch.setattr(
        upd, "_spawn_detached_windows_installer", lambda p: spawned.append(p) or True
    )

    first = upd.apply_windows_staged_update_now()
    second = upd.apply_windows_staged_update_now()

    assert first is True
    assert second is False
    assert spawned == [staged.installer_path]


def test_apply_now_failed_claim_cleans_up_only_claimed_copy(staging: Path, monkeypatch):
    """A validation failure after claiming must not touch a manifest staged by
    another process in the meantime."""
    staged = _stage(staging, version="0.0.1")
    monkeypatch.setattr("pythinker_code.constant.VERSION", "5.0.0")

    assert upd.apply_windows_staged_update_now() is False
    assert upd.read_windows_staged_update() is None
    del staged
