from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SEMVER = r"\d+\.\d+\.\d+"


def _version(rel: str) -> str:
    with (REPO_ROOT / rel).open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _deps(rel: str) -> list[str]:
    with (REPO_ROOT / rel).open("rb") as fh:
        return tomllib.load(fh)["project"]["dependencies"]


def _root_deps() -> list[str]:
    return _deps("pyproject.toml")


def _pin_in(deps: list[str], name: str) -> str:
    for dep in deps:
        head = dep.split("==", 1)
        if len(head) == 2 and head[0].split("[")[0] == name:
            return head[1].split(";")[0].strip()
    raise AssertionError(f"no =={'<ver>'} pin for {name}")


VERSION = _version("pyproject.toml")


def test_version_is_semver() -> None:
    assert re.fullmatch(SEMVER, VERSION), VERSION


def test_subpackage_pins_match_versions() -> None:
    assert _pin("pythinker-core") == _version("packages/pythinker-core/pyproject.toml")
    assert _pin("pythinker-host") == _version("packages/pythinker-host/pyproject.toml")
    assert _pin("pythinker-review") == _version("packages/pythinker-review/pyproject.toml")


def _pin(name: str) -> str:
    return _pin_in(_root_deps(), name)


def test_review_is_frozen_at_0_1_0() -> None:
    assert _pin("pythinker-review") == "0.1.0"


def test_sdk_core_pin_matches_core_version() -> None:
    assert _pin_in(_deps("sdks/pythinker-sdk/pyproject.toml"), "pythinker-core") == _version(
        "packages/pythinker-core/pyproject.toml"
    )


def test_readme_heading_and_pip_snippet() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"What's New in {VERSION}" in readme
    assert f"pythinker-code=={VERSION}" in readme


def test_changelog_has_dated_heading_for_version() -> None:
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {VERSION} (" in changelog


def test_asset_names_match_version_across_files() -> None:
    files = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "packages" / "linux-installer" / "README.md",
        REPO_ROOT / "docs" / "en" / "guides" / "getting-started.md",
    ]
    # Each asset shape, where present, must carry VERSION (never a stale one).
    shape_res = [
        re.compile(rf"PythinkerSetup-({SEMVER})\.exe"),
        re.compile(rf"pythinker-code_({SEMVER})_[a-z0-9]+\.deb"),
        re.compile(rf"pythinker-code-({SEMVER})\.[a-z0-9_]+\.rpm"),
        re.compile(rf"releases/download/v({SEMVER})/"),
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for rx in shape_res:
            for found in rx.findall(text):
                assert found == VERSION, f"{path}: {found} != {VERSION}"


def test_linux_installer_build_example_matches_version() -> None:
    readme = (REPO_ROOT / "packages" / "linux-installer" / "README.md").read_text(encoding="utf-8")
    versions = re.findall(r"bash packages/linux-installer/build\.sh (\d+\.\d+\.\d+)", readme)
    assert versions, "missing Linux installer build example"
    assert versions == [VERSION]


def test_no_hardcoded_version_badge_in_readme() -> None:
    # Guard the contract's "badges" clause: the only version-bearing badge is the
    # shields.io-live PyPI badge (img.shields.io/pypi/v/...). Fail if a future edit
    # hardcodes VERSION into a shields.io badge label/path, which would silently drift.
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for line in readme.splitlines():
        if "img.shields.io" in line and re.search(rf"badge/[^)]*{re.escape(VERSION)}", line):
            raise AssertionError(f"hardcoded-version badge found: {line!r}")


def test_install_flag_examples_are_valid_semver_shape_only() -> None:
    # The documented §3 exception: `--version <x.y.z>` teaches flag syntax and
    # is NOT lockstepped to VERSION — only asserted to be valid semver shape.
    flag_re = re.compile(rf"--version ({SEMVER})")
    for rel in ("README.md", "docs/en/guides/getting-started.md"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for found in flag_re.findall(text):
            assert re.fullmatch(SEMVER, found), found
