from __future__ import annotations

import tarfile
from unittest.mock import patch

import pytest

from pythinker_code.ui.shell import update as upd


def test_detect_upgrade_command_returns_native_marker_when_native():
    with patch("pythinker_code.ui.shell.update._is_native_build", return_value=True):
        cmd = upd._detect_upgrade_command()
    assert cmd == [upd.NATIVE_INSTALLER_MARKER]


def test_detect_upgrade_command_pypi_path_when_not_native():
    with (
        patch("pythinker_code.ui.shell.update._is_native_build", return_value=False),
        patch("sys.executable", "/usr/local/bin/python"),
    ):
        cmd = upd._detect_upgrade_command()
    assert "pythinker-code" in cmd
    assert cmd != [upd.NATIVE_INSTALLER_MARKER]


@pytest.mark.asyncio
async def test_native_update_runs_when_proactive_checks_are_disabled(monkeypatch):
    monkeypatch.setenv("PYTHINKER_CLI_NO_AUTO_UPDATE", "1")
    installed: list[str] = []

    async def fake_fetch(session, asset_name: str, channel: str):
        return "https://example.invalid/asset", "a" * 64

    async def fake_download(session, asset_name: str, download_url: str, destination):
        destination.write_bytes(b"archive")
        return upd.UpdateResult.UPDATED

    def fake_install_native_archive(asset) -> upd.UpdateResult:
        installed.append(asset.name)
        return upd.UpdateResult.UPDATED

    monkeypatch.setattr(upd, "_is_windows", lambda: False)
    monkeypatch.setattr(upd, "_installed_linux_package_kind", lambda: None)
    monkeypatch.setattr(
        upd,
        "native_archive_asset_name",
        lambda version: f"pythinker-{version}.tar.gz",
    )
    monkeypatch.setattr(upd, "_fetch_native_release_asset", fake_fetch)
    monkeypatch.setattr(upd, "_download_native_asset", fake_download)
    monkeypatch.setattr(upd, "_verify_sha256", lambda path, expected: True)
    monkeypatch.setattr(upd, "_install_native_archive", fake_install_native_archive)

    result = await upd._maybe_run_native_update(latest_version="9.9.9")

    assert result is upd.UpdateResult.UPDATED
    assert installed == ["pythinker-9.9.9.tar.gz"]


def test_native_prompt_does_not_leak_marker(monkeypatch):
    """Regression: the update prompt must never render the raw marker token."""
    with patch("pythinker_code.ui.shell.update._is_native_build", return_value=True):
        rendered = upd._update_prompt_text("0.1.0", "0.2.0")
    text = rendered.plain
    assert upd.NATIVE_INSTALLER_MARKER not in text
    assert "native updater" in text


def _make_native_archive(tmp_path, body: str = "new"):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "pythinker").write_text(body, encoding="utf-8")
    archive = tmp_path / "pythinker-0.2.0-x86_64-unknown-linux-gnu.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload / "pythinker", arcname="pythinker")
    return archive


def test_install_native_archive_stages_without_touching_running_exe(monkeypatch, tmp_path):
    # The running executable must be left untouched (overwriting it mid-session
    # corrupts this onefile build's lazy archive reads). The new binary lands in
    # the staged side path instead, ready to promote on exit.
    current = tmp_path / "pythinker"
    current.write_text("old", encoding="utf-8")
    current.chmod(0o755)
    archive = _make_native_archive(tmp_path)

    monkeypatch.setattr(upd.sys, "executable", str(current))

    assert upd._install_native_archive(archive) is upd.UpdateResult.UPDATED
    # Running exe is unchanged...
    assert current.read_text(encoding="utf-8") == "old"
    # ...the update is staged beside it, executable, and marked native.
    staged = upd.staged_native_path()
    assert staged.read_text(encoding="utf-8") == "new"
    assert staged.stat().st_mode & 0o111
    assert (tmp_path / ".pythinker-native").read_text(encoding="utf-8") == (
        "pythinker-native-build\n"
    )


def test_promote_staged_native_update_swaps_into_place(monkeypatch, tmp_path):
    current = tmp_path / "pythinker"
    current.write_text("old", encoding="utf-8")
    monkeypatch.setattr(upd.sys, "executable", str(current))
    upd.staged_native_path().write_text("new", encoding="utf-8")

    upd._promote_staged_native_update()

    assert current.read_text(encoding="utf-8") == "new"
    assert not upd.staged_native_path().exists()


def test_promote_staged_native_update_noop_without_staged_file(monkeypatch, tmp_path):
    current = tmp_path / "pythinker"
    current.write_text("old", encoding="utf-8")
    monkeypatch.setattr(upd.sys, "executable", str(current))

    upd._promote_staged_native_update()  # no staged file → no-op

    assert current.read_text(encoding="utf-8") == "old"


def test_discard_staged_native_update_removes_file(monkeypatch, tmp_path):
    current = tmp_path / "pythinker"
    current.write_text("old", encoding="utf-8")
    monkeypatch.setattr(upd.sys, "executable", str(current))
    upd.staged_native_path().write_text("broken", encoding="utf-8")

    upd.discard_staged_native_update()

    assert not upd.staged_native_path().exists()
    assert current.read_text(encoding="utf-8") == "old"  # running exe untouched


def test_register_staged_native_promotion_is_idempotent(monkeypatch):
    registered: list[object] = []
    monkeypatch.setattr(upd.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(upd, "_staged_promotion_registered", False)

    upd.register_staged_native_promotion()
    upd.register_staged_native_promotion()

    assert registered == [upd._promote_staged_native_update]
