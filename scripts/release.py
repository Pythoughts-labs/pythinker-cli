#!/usr/bin/env python3
"""Pythinker-code release orchestrator.

Rewrites every version-derived file + uv.lock from the single source of
truth (pyproject.toml:3), runs the same gates CI runs, and opens a
release/X.Y.Z PR. It never pushes to main and never pushes the tag — the
maintainer pushes the tag(s) after the PR merges (C1).

stdlib + shells out to git/gh/uv. The shipped agent gains zero runtime deps
(C3: CI/release-tooling exemption).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

import tomlkit

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"
CORE_PYPROJECT = REPO_ROOT / "packages" / "pythinker-core" / "pyproject.toml"
HOST_PYPROJECT = REPO_ROOT / "packages" / "pythinker-host" / "pyproject.toml"
REVIEW_PYPROJECT = REPO_ROOT / "packages" / "pythinker-review" / "pyproject.toml"
SDK_PYPROJECT = REPO_ROOT / "sdks" / "pythinker-sdk" / "pyproject.toml"

# Hand-authored changelog files. The docs changelog is intentionally excluded:
# docs/en/release-notes/changelog.md is auto-synced from the root CHANGELOG.md
# by docs/scripts/sync-changelog.mjs and must not be edited directly.
CHANGELOG_FILES = (
    REPO_ROOT / "CHANGELOG.md",
    REPO_ROOT / "docs" / "en" / "release-notes" / "breaking-changes.md",
)
DOCS_CHANGELOG_SCRIPT = REPO_ROOT / "docs" / "scripts" / "sync-changelog.mjs"

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_DEP_PIN_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+)(?P<extras>\[[^\]]+\])?==(?P<ver>[^;\s]+)(?P<rest>.*)$"
)
_UNRELEASED_RE = re.compile(r"^## Unreleased[ \t]*$", re.MULTILINE)


class ReleaseError(Exception):
    """Raised when a precondition or rewrite invariant fails."""


def parse_semver(version: str) -> tuple[int, int, int]:
    m = SEMVER_RE.match(version)
    if not m:
        raise ReleaseError(f"not a valid x.y.z version: {version!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def assert_monotonic(*, current: str, target: str) -> None:
    if parse_semver(target) <= parse_semver(current):
        raise ReleaseError(
            f"target version {target} must be strictly greater than current {current}"
        )


def read_project_version(pyproject_path: Path) -> str:
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ReleaseError(f"missing project.version in {pyproject_path}")
    return version


def _dump_and_verify(path: Path, doc: tomlkit.TOMLDocument) -> None:
    """Write `doc` then re-read with tomllib to confirm it parses."""
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    with path.open("rb") as fh:
        tomllib.load(fh)  # raises tomllib.TOMLDecodeError if we produced junk


def set_root_version(pyproject_path: Path, version: str) -> None:
    parse_semver(version)
    doc = tomlkit.parse(pyproject_path.read_text(encoding="utf-8"))
    doc["project"]["version"] = version  # type: ignore[index]
    _dump_and_verify(pyproject_path, doc)
    if read_project_version(pyproject_path) != version:
        raise ReleaseError(f"parse-back failed: {pyproject_path} did not re-read as {version}")


def set_dependency_pin(pyproject_path: Path, name: str, version: str) -> None:
    """Rewrite the `name[extras]==<ver>` pin in [project].dependencies, preserving extras."""
    parse_semver(version)
    doc = tomlkit.parse(pyproject_path.read_text(encoding="utf-8"))
    deps = doc["project"]["dependencies"]  # type: ignore[index]
    found = False
    for i, dep in enumerate(deps):
        m = _DEP_PIN_RE.match(str(dep))
        if m and m.group("name") == name:
            extras = m.group("extras") or ""
            rest = m.group("rest") or ""
            deps[i] = f"{name}{extras}=={version}{rest}"
            found = True
            break
    if not found:
        raise ReleaseError(f"no `=={'<ver>'}` pin for {name} in {pyproject_path}")
    _dump_and_verify(pyproject_path, doc)
    # parse-back assertion: the intended pin re-reads to the intended version
    with pyproject_path.open("rb") as fh:
        reread = tomllib.load(fh)["project"]["dependencies"]
    expected = next((d for d in reread if d.split("==")[0].split("[")[0] == name), None)
    if expected is None or expected.split("==", 1)[1].split(";")[0].strip() != version:
        raise ReleaseError(f"parse-back failed: {name} pin in {pyproject_path} != {version}")


def promote_changelog(path: Path, version: str, *, release_date: str) -> None:
    """Rename `## Unreleased` to `## X.Y.Z (DATE)`, preserving its body.

    Re-inserts a fresh empty `## Unreleased` above the promoted dated section.
    """
    parse_semver(version)
    text = path.read_text(encoding="utf-8")
    m = _UNRELEASED_RE.search(text)
    if m is None:
        raise ReleaseError(f"no `## Unreleased` anchor in {path}")
    # Replace the heading line in place, then prepend a new empty anchor.
    dated = f"## {version} ({release_date})"
    promoted = text[: m.start()] + dated + text[m.end() :]
    new_text = promoted[: m.start()] + "## Unreleased\n\n" + promoted[m.start() :]
    path.write_text(new_text, encoding="utf-8")


def rewrite_version_strings(text: str, *, old: str, new: str) -> str:
    """Replace ONLY release-pattern occurrences of `old` with `new`.

    Deliberately skips `--version <old>` flag examples (the documented
    §3 exception) so they stay shape-only — the lockstep test enforces this.
    """
    o = re.escape(old)
    patterns = [
        (rf"(What's New in ){o}", rf"\g<1>{new}"),
        (rf"(pythinker-code==){o}", rf"\g<1>{new}"),
        (rf"(PythinkerSetup-){o}(\.exe)", rf"\g<1>{new}\g<2>"),
        (rf"(pythinker-code_){o}(_[a-z0-9]+\.deb)", rf"\g<1>{new}\g<2>"),
        (rf"(pythinker-code-){o}(\.[a-z0-9_]+\.rpm)", rf"\g<1>{new}\g<2>"),
        (rf"(releases/download/v){o}(/)", rf"\g<1>{new}\g<2>"),
        (
            r"(bash packages/linux-installer/build\.sh )\d+\.\d+\.\d+",
            rf"\g<1>{new}",
        ),
    ]
    for pat, repl in patterns:
        text = re.sub(pat, repl, text)
    return text


def rewrite_version_in_files(paths: list[Path], *, old: str, new: str) -> None:
    for path in paths:
        original = path.read_text(encoding="utf-8")
        path.write_text(rewrite_version_strings(original, old=old, new=new), encoding="utf-8")


def require_executable(name: str, *, recovery: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise ReleaseError(f"required executable `{name}` not found on PATH; {recovery}")
    return path


def sync_docs_changelog() -> None:
    """Regenerate the docs changelog; validate() preflights node before mutations."""
    node = require_executable("node", recovery="install Node.js or add `node` to PATH")
    subprocess.run([node, str(DOCS_CHANGELOG_SCRIPT)], cwd=REPO_ROOT / "docs", check=True)


def _run(cmd: list[str], *, dry_run: bool, check: bool = True) -> subprocess.CompletedProcess[str]:
    if dry_run:
        print(f"[dry-run] {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=REPO_ROOT, text=True, check=check)


def _git_capture(cmd: list[str]) -> str:
    return subprocess.run(
        cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=True
    ).stdout.strip()


def validate(target: str) -> None:
    """Phase 1 — fail loud, no writes."""
    parse_semver(target)
    require_executable("node", recovery="install Node.js or add `node` to PATH")
    if _git_capture(["git", "status", "--porcelain"]):
        raise ReleaseError("working tree is not clean; commit or stash first")
    _git_capture(["git", "fetch", "origin"])
    local = _git_capture(["git", "rev-parse", "main"])
    remote = _git_capture(["git", "rev-parse", "origin/main"])
    if local != remote:
        raise ReleaseError("local main != origin/main; rebase onto origin/main first")
    head = _git_capture(["git", "rev-parse", "HEAD"])
    if head != remote:
        raise ReleaseError("current HEAD is not origin/main; switch to main before release prep")
    assert_monotonic(current=read_project_version(ROOT_PYPROJECT), target=target)
    # Assert the `## Unreleased` anchor in ALL hand-authored changelog files BEFORE
    # any write (same list rewrite() promotes) so Phase 2 cannot partially rewrite
    # the tree.
    for changelog in CHANGELOG_FILES:
        if _UNRELEASED_RE.search(changelog.read_text(encoding="utf-8")) is None:
            raise ReleaseError(f"{changelog} has no `## Unreleased` section")
    # The primary CHANGELOG's body may legitimately be empty (CI-only/docs release):
    # warn, do not abort.
    primary = CHANGELOG_FILES[0].read_text(encoding="utf-8")
    m = _UNRELEASED_RE.search(primary)
    assert m is not None  # guaranteed by the loop above
    body = primary[m.end() :].split("\n## ", 1)[0].strip()
    if not body:
        print("warning: `## Unreleased` body is empty (CI-only/docs release?)")


def rewrite(target: str, *, bump_core: str | None, bump_host: str | None) -> None:
    """Phase 2 — rewrite all derived files before regenerating uv.lock."""
    old = read_project_version(ROOT_PYPROJECT)
    set_root_version(ROOT_PYPROJECT, target)
    if bump_core:
        set_root_version(CORE_PYPROJECT, bump_core)
        set_dependency_pin(ROOT_PYPROJECT, "pythinker-core", bump_core)
        set_dependency_pin(SDK_PYPROJECT, "pythinker-core", bump_core)
    if bump_host:
        set_root_version(HOST_PYPROJECT, bump_host)
        set_dependency_pin(ROOT_PYPROJECT, "pythinker-host", bump_host)
    today = date.today().isoformat()
    for changelog in CHANGELOG_FILES:
        promote_changelog(changelog, target, release_date=today)
    sync_docs_changelog()
    rewrite_version_in_files(
        [
            REPO_ROOT / "README.md",
            REPO_ROOT / "packages" / "linux-installer" / "README.md",
            REPO_ROOT / "docs" / "en" / "guides" / "getting-started.md",
        ],
        old=old,
        new=target,
    )


GATES = [
    [
        "uv",
        "run",
        "python",
        "scripts/check_version_tag.py",
        "--pyproject",
        "pyproject.toml",
        "--expected-version",
        "{target}",
    ],
    [
        "uv",
        "run",
        "python",
        "scripts/check_pythinker_dependency_versions.py",
        "--root-pyproject",
        "pyproject.toml",
        "--pythinker-core-pyproject",
        "packages/pythinker-core/pyproject.toml",
        "--pythinker-host-pyproject",
        "packages/pythinker-host/pyproject.toml",
        "--pythinker-review-pyproject",
        "packages/pythinker-review/pyproject.toml",
        "--pythinker-sdk-pyproject",
        "sdks/pythinker-sdk/pyproject.toml",
    ],
    ["uv", "sync", "--frozen", "--all-extras", "--all-packages"],
    ["uv", "run", "pytest", "tests/test_version_lockstep.py", "-q"],
]


def run_gates(target: str) -> None:
    """Phase 3 — the same gates CI runs; abort before push on any failure."""
    for tmpl in GATES:
        cmd = [part.format(target=target) for part in tmpl]
        result = subprocess.run(cmd, cwd=REPO_ROOT, text=True)
        if result.returncode != 0:
            raise ReleaseError(f"local gate failed: {' '.join(cmd)}")
    # README/CHANGELOG fixed-string greps (grep -qF, not regex).
    for needle, path in (
        (f"What's New in {target}", "README.md"),
        (f"pythinker-code=={target}", "README.md"),
        (f"## {target} (", "CHANGELOG.md"),
    ):
        if subprocess.run(["grep", "-qF", needle, path], cwd=REPO_ROOT).returncode != 0:
            raise ReleaseError(f"expected string {needle!r} not found in {path}")


def open_pr(target: str, *, bump_core: str | None, bump_host: str | None, dry_run: bool) -> None:
    """Phase 4 — branch + commit + push + PR (never main, C1)."""
    branch = f"release/{target}"
    _run(["git", "switch", "-c", branch, "origin/main"], dry_run=dry_run)
    _run(["git", "add", "-A"], dry_run=dry_run)
    _run(["git", "commit", "-m", f"chore(release): prepare {target}"], dry_run=dry_run)
    _run(["git", "push", "-u", "origin", branch], dry_run=dry_run)
    _run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            f"chore(release): prepare {target}",
            "--body",
            f"Automated release prep for {target}. Tag after merge (C1).",
        ],
        dry_run=dry_run,
    )
    print("\nAfter the PR merges and CodeRabbit status is success, push the tag(s):")
    if bump_core:
        print(f"  git tag pythinker-core-{bump_core} && git push origin pythinker-core-{bump_core}")
    if bump_host:
        print(f"  git tag pythinker-host-{bump_host} && git push origin pythinker-host-{bump_host}")
    if bump_core or bump_host:
        print("  # wait for the sub-package OIDC publish jobs to land on PyPI, THEN:")
    print(f"  git tag v{target} && git push origin v{target}")


def _format_called_process_error(exc: subprocess.CalledProcessError) -> str:
    cmd = " ".join(str(part) for part in exc.cmd) if isinstance(exc.cmd, list) else str(exc.cmd)
    return f"command failed ({exc.returncode}): {cmd}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a pythinker-code release PR.")
    parser.add_argument("--set-version", required=True, help="target X.Y.Z")
    parser.add_argument("--bump-core", default=None, help="new pythinker-core A.B.C")
    parser.add_argument("--bump-host", default=None, help="new pythinker-host A.B.C")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    target = args.set_version
    try:
        validate(target)
        if args.dry_run:
            print(
                f"[dry-run] would rewrite SSOT -> {target}"
                + (f", core -> {args.bump_core}" if args.bump_core else "")
                + (f", host -> {args.bump_host}" if args.bump_host else "")
            )
            print("[dry-run] would run: uv lock; gates; branch+PR")
            open_pr(target, bump_core=args.bump_core, bump_host=args.bump_host, dry_run=True)
            return 0
        rewrite(target, bump_core=args.bump_core, bump_host=args.bump_host)
        _run(["uv", "lock"], dry_run=False)
        run_gates(target)
        open_pr(target, bump_core=args.bump_core, bump_host=args.bump_host, dry_run=False)
    except subprocess.CalledProcessError as exc:
        print(f"error: {_format_called_process_error(exc)}", file=sys.stderr)
        return 1
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
