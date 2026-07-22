from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEP_CHECK = REPO_ROOT / "scripts" / "check_pythinker_dependency_versions.py"

_spec = importlib.util.spec_from_file_location("release_tool", REPO_ROOT / "scripts" / "release.py")
assert _spec and _spec.loader
release_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_tool)


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _run_dep_check(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DEP_CHECK), *args],
        capture_output=True,
        text=True,
    )


def test_dep_check_passes_when_review_pin_matches(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        "root.toml",
        '[project]\nname="pythinker-code"\nversion="0.27.0"\n'
        'dependencies=["pythinker-core[contrib]==1.1.1","pythinker-host==1.0.0",'
        '"pythinker-review==0.1.0"]\n',
    )
    core = _write(tmp_path, "core.toml", '[project]\nname="pythinker-core"\nversion="1.1.1"\n')
    host = _write(tmp_path, "host.toml", '[project]\nname="pythinker-host"\nversion="1.0.0"\n')
    review = _write(
        tmp_path, "review.toml", '[project]\nname="pythinker-review"\nversion="0.1.0"\n'
    )
    sdk = _write(
        tmp_path,
        "sdk.toml",
        '[project]\nname="pythinker-sdk"\nversion="1.1.0"\n'
        'dependencies=["pythinker-core==1.1.1"]\n',
    )
    result = _run_dep_check(
        "--root-pyproject",
        str(root),
        "--pythinker-core-pyproject",
        str(core),
        "--pythinker-host-pyproject",
        str(host),
        "--pythinker-review-pyproject",
        str(review),
        "--pythinker-sdk-pyproject",
        str(sdk),
    )
    assert result.returncode == 0, result.stderr


def test_dep_check_fails_when_review_pin_drifts(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        "root.toml",
        '[project]\nname="pythinker-code"\nversion="0.27.0"\n'
        'dependencies=["pythinker-core[contrib]==1.1.1","pythinker-host==1.0.0",'
        '"pythinker-review==0.1.0"]\n',
    )
    core = _write(tmp_path, "core.toml", '[project]\nname="pythinker-core"\nversion="1.1.1"\n')
    host = _write(tmp_path, "host.toml", '[project]\nname="pythinker-host"\nversion="1.0.0"\n')
    review = _write(
        tmp_path, "review.toml", '[project]\nname="pythinker-review"\nversion="0.2.0"\n'
    )
    sdk = _write(
        tmp_path,
        "sdk.toml",
        '[project]\nname="pythinker-sdk"\nversion="1.1.0"\n'
        'dependencies=["pythinker-core==1.1.1"]\n',
    )
    result = _run_dep_check(
        "--root-pyproject",
        str(root),
        "--pythinker-core-pyproject",
        str(core),
        "--pythinker-host-pyproject",
        str(host),
        "--pythinker-review-pyproject",
        str(review),
        "--pythinker-sdk-pyproject",
        str(sdk),
    )
    assert result.returncode == 1
    assert "pythinker-review version mismatch" in result.stderr


def test_dep_check_fails_when_sdk_core_pin_drifts(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        "root.toml",
        '[project]\nname="pythinker-code"\nversion="0.29.0"\n'
        'dependencies=["pythinker-core[contrib]==1.2.0","pythinker-host==1.0.0",'
        '"pythinker-review==0.1.0"]\n',
    )
    core = _write(tmp_path, "core.toml", '[project]\nname="pythinker-core"\nversion="1.2.0"\n')
    host = _write(tmp_path, "host.toml", '[project]\nname="pythinker-host"\nversion="1.0.0"\n')
    review = _write(
        tmp_path, "review.toml", '[project]\nname="pythinker-review"\nversion="0.1.0"\n'
    )
    sdk = _write(
        tmp_path,
        "sdk.toml",
        '[project]\nname="pythinker-sdk"\nversion="1.1.0"\n'
        'dependencies=["pythinker-core==1.1.1"]\n',
    )
    result = _run_dep_check(
        "--root-pyproject",
        str(root),
        "--pythinker-core-pyproject",
        str(core),
        "--pythinker-host-pyproject",
        str(host),
        "--pythinker-review-pyproject",
        str(review),
        "--pythinker-sdk-pyproject",
        str(sdk),
    )
    assert result.returncode == 1
    assert "pythinker-sdk core dependency mismatch" in result.stderr


def test_parse_semver_accepts_xyz() -> None:
    assert release_tool.parse_semver("0.28.0") == (0, 28, 0)


def test_parse_semver_rejects_non_xyz() -> None:
    with pytest.raises(release_tool.ReleaseError):
        release_tool.parse_semver("0.28")
    with pytest.raises(release_tool.ReleaseError):
        release_tool.parse_semver("v0.28.0")


def test_assert_monotonic_allows_increase() -> None:
    release_tool.assert_monotonic(current="0.27.0", target="0.28.0")


def test_assert_monotonic_rejects_equal_or_lower() -> None:
    with pytest.raises(release_tool.ReleaseError):
        release_tool.assert_monotonic(current="0.27.0", target="0.27.0")
    with pytest.raises(release_tool.ReleaseError):
        release_tool.assert_monotonic(current="0.27.0", target="0.26.0")


def test_set_root_version_rewrites_and_parses_back(tmp_path: Path) -> None:
    src = (
        '[project]\nname = "pythinker-code"\nversion = "0.27.0"\n'
        "dependencies = [\n"
        '    "pythinker-core[contrib]==1.1.1",\n'
        '    "pythinker-host==1.0.0",\n'
        '    "pythinker-review==0.1.0",\n'
        "]\n"
    )
    p = tmp_path / "pyproject.toml"
    p.write_text(src, encoding="utf-8")
    release_tool.set_root_version(p, "0.28.0")
    assert release_tool.read_project_version(p) == "0.28.0"


def test_set_dependency_pin_updates_extras_form(tmp_path: Path) -> None:
    src = (
        '[project]\nname = "x"\nversion = "0.1.0"\n'
        'dependencies = [\n    "pythinker-core[contrib]==1.1.1",\n    "rich==15.0.0",\n]\n'
    )
    p = tmp_path / "pyproject.toml"
    p.write_text(src, encoding="utf-8")
    release_tool.set_dependency_pin(p, "pythinker-core", "1.2.0")
    with p.open("rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    assert "pythinker-core[contrib]==1.2.0" in deps
    assert "rich==15.0.0" in deps  # untouched


def test_set_dependency_pin_rejects_missing(tmp_path: Path) -> None:
    src = '[project]\nname="x"\nversion="0.1.0"\ndependencies=["rich==15.0.0"]\n'
    p = tmp_path / "pyproject.toml"
    p.write_text(src, encoding="utf-8")
    with pytest.raises(release_tool.ReleaseError):
        release_tool.set_dependency_pin(p, "pythinker-core", "1.2.0")


def test_promote_changelog_preserves_body_and_reinserts_unreleased(tmp_path: Path) -> None:
    src = (
        "# Changelog\n\n"
        "## Unreleased\n\n"
        "- **Did a thing.** Detail line.\n\n"
        "## 0.27.0 (2026-05-31)\n\n- Older entry.\n"
    )
    p = tmp_path / "CHANGELOG.md"
    p.write_text(src, encoding="utf-8")
    release_tool.promote_changelog(p, "0.28.0", release_date="2026-06-01")
    out = p.read_text(encoding="utf-8")
    assert "## Unreleased\n" in out  # empty anchor re-inserted
    assert "## 0.28.0 (2026-06-01)\n" in out
    assert "- **Did a thing.** Detail line." in out  # authored body preserved
    # the new dated section sits above the previous release
    assert out.index("## 0.28.0 (2026-06-01)") < out.index("## 0.27.0 (2026-05-31)")
    # the empty Unreleased anchor sits above the new dated section
    assert out.index("## Unreleased") < out.index("## 0.28.0 (2026-06-01)")


def test_promote_changelog_empty_unreleased_is_ok(tmp_path: Path) -> None:
    src = "# Changelog\n\n## Unreleased\n\n## 0.27.0 (2026-05-31)\n\n- Older.\n"
    p = tmp_path / "CHANGELOG.md"
    p.write_text(src, encoding="utf-8")
    release_tool.promote_changelog(p, "0.28.0", release_date="2026-06-01")
    out = p.read_text(encoding="utf-8")
    assert "## 0.28.0 (2026-06-01)" in out
    assert "## Unreleased" in out


def test_promote_changelog_missing_anchor_raises(tmp_path: Path) -> None:
    p = tmp_path / "CHANGELOG.md"
    p.write_text("# Changelog\n\n## 0.27.0 (2026-05-31)\n", encoding="utf-8")
    with pytest.raises(release_tool.ReleaseError):
        release_tool.promote_changelog(p, "0.28.0", release_date="2026-06-01")


def test_open_pr_dry_run_branches_from_origin_main(capsys: pytest.CaptureFixture[str]) -> None:
    release_tool.open_pr("0.28.0", bump_core=None, bump_host=None, dry_run=True)
    out = capsys.readouterr().out
    assert "[dry-run] git switch -c release/0.28.0 origin/main" in out


def test_format_called_process_error_includes_returncode_and_command() -> None:
    exc = subprocess.CalledProcessError(7, ["git", "fetch", "origin"])
    assert release_tool._format_called_process_error(exc) == "command failed (7): git fetch origin"


def test_rewrite_version_strings_targets_only_release_patterns() -> None:
    text = (
        "## 🆕 What's New in 0.27.0\n"
        "pip install --upgrade pythinker-code==0.27.0\n"
        "PythinkerSetup-0.27.0.exe\n"
        "pythinker-code_0.27.0_amd64.deb\n"
        "pythinker-code-0.27.0.x86_64.rpm\n"
        "releases/download/v0.27.0/pythinker-code_0.27.0_arm64.deb\n"
        "bash packages/linux-installer/build.sh 0.26.0\n"
        "bash -s -- --version 0.27.0\n"  # flag example: MUST be preserved
    )
    out = release_tool.rewrite_version_strings(text, old="0.27.0", new="0.28.0")
    assert "## 🆕 What's New in 0.28.0" in out
    assert "pythinker-code==0.28.0" in out
    assert "PythinkerSetup-0.28.0.exe" in out
    assert "pythinker-code_0.28.0_amd64.deb" in out
    assert "pythinker-code-0.28.0.x86_64.rpm" in out
    assert "releases/download/v0.28.0/pythinker-code_0.28.0_arm64.deb" in out
    assert "bash packages/linux-installer/build.sh 0.28.0" in out
    # the flag example is the documented exception — untouched
    assert "--version 0.27.0" in out
    assert "--version 0.28.0" not in out
