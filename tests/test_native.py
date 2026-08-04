from __future__ import annotations

import sys
from unittest.mock import patch

from pythinker_code import native


def test_is_native_build_false_when_not_frozen():
    with patch.object(sys, "frozen", False, create=True):
        assert native.is_native_build() is False


def test_is_native_build_false_when_frozen_without_sentinel(tmp_path):
    fake_exe = tmp_path / "other-app"
    fake_exe.write_bytes(b"")
    with (
        patch.object(sys, "frozen", True, create=True),
        patch.object(sys, "executable", str(fake_exe)),
    ):
        assert native.is_native_build() is False


def test_is_native_build_true_when_frozen_pythinker_without_sentinel(tmp_path):
    fake_exe = tmp_path / "pythinker"
    fake_exe.write_bytes(b"")
    with (
        patch.object(sys, "frozen", True, create=True),
        patch.object(sys, "executable", str(fake_exe)),
    ):
        assert native.is_native_build() is True


def test_is_native_build_true_when_sentinel_present(tmp_path):
    fake_exe = tmp_path / "pythinker.exe"
    fake_exe.write_bytes(b"")
    (tmp_path / ".pythinker-native").write_text("pythinker-native-build")
    with (
        patch.object(sys, "frozen", True, create=True),
        patch.object(sys, "executable", str(fake_exe)),
    ):
        assert native.is_native_build() is True


def test_is_native_build_true_when_sentinel_present_without_frozen_flag(tmp_path):
    fake_exe = tmp_path / "pythinker.exe"
    fake_exe.write_bytes(b"")
    (tmp_path / ".pythinker-native").write_text("pythinker-native-build")
    with (
        patch.object(sys, "frozen", False, create=True),
        patch.object(sys, "executable", str(fake_exe)),
    ):
        assert native.is_native_build() is True


def test_native_installer_release_url_latest():
    url = native.native_installer_release_url(channel="latest")
    assert url == ("https://api.github.com/repos/Pythoughts-labs/pythinker-cli/releases/latest")


def test_native_installer_release_url_stable():
    url = native.native_installer_release_url(channel="stable")
    assert "/releases/tags/stable" in url


def test_native_installer_asset_name():
    assert native.native_installer_asset_name("0.11.0") == "PythinkerSetup-0.11.0.exe"


def test_native_archive_asset_name_linux_x86_64():
    with (
        patch.object(sys, "platform", "linux"),
        patch("platform.machine", return_value="x86_64"),
    ):
        assert (
            native.native_archive_asset_name("0.14.0")
            == "pythinker-0.14.0-x86_64-unknown-linux-gnu.tar.gz"
        )


def test_native_archive_asset_name_macos_intel():
    with (
        patch.object(sys, "platform", "darwin"),
        patch("platform.machine", return_value="x86_64"),
    ):
        assert (
            native.native_archive_asset_name("0.14.0")
            == "pythinker-0.14.0-x86_64-apple-darwin.tar.gz"
        )
