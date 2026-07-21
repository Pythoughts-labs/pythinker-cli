"""Tests for the shell file mention completer."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from inline_snapshot import snapshot
from prompt_toolkit.completion import CompleteEvent, Completion
from prompt_toolkit.document import Document
from pythinker_host import get_current_host
from pythinker_host.path import HostPath

from pythinker_code.ui.shell.prompt import LocalFileMentionCompleter
from pythinker_code.ui.shell.prompting.completion.context import (
    CompletionKind,
    parse_completion_context,
)
from pythinker_code.ui.shell.prompting.completion.workspace import WorkspaceIndex
from tests.ui_and_conv._prompt_lifecycle import RecordingLifecycle as _Lifecycle


async def _completion_results(root: Path, text: str, *, limit: int = 1000) -> list[Completion]:
    lifecycle = _Lifecycle()
    index = WorkspaceIndex(
        get_current_host(),
        lifecycle,
        HostPath.unsafe_from_local_path(root),
        refresh_interval=60,
        limit=limit,
    )
    completer = LocalFileMentionCompleter(index)
    document = Document(text=text, cursor_position=len(text))
    context = parse_completion_context(document, allow_slash=False)
    index.request_refresh(context.token)
    if lifecycle.created:
        await lifecycle.created[-1]
    event = CompleteEvent(completion_requested=True)
    completions = list(completer.get_completions(document, event))
    await lifecycle.aclose()
    return completions


async def _completion_texts(root: Path, text: str, *, limit: int = 1000) -> list[str]:
    return [completion.text for completion in await _completion_results(root, text, limit=limit)]


@pytest.mark.asyncio
async def test_top_level_paths_skip_ignored_names(tmp_path: Path):
    """Only surface non-ignored entries when completing the top level."""
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / ".DS_Store").write_text("")
    (tmp_path / "README.md").write_text("hello")

    texts = await _completion_texts(tmp_path, "@")

    assert "src/" in texts
    assert "README.md" in texts
    assert "node_modules/" not in texts
    assert ".DS_Store" not in texts


@pytest.mark.asyncio
async def test_directory_completion_continues_after_slash(tmp_path: Path):
    """Continue descending when the fragment ends with a slash."""
    src = tmp_path / "src"
    src.mkdir()
    nested = src / "module.py"
    nested.write_text("print('hi')\n")

    texts = await _completion_texts(tmp_path, "@src/")

    assert "src/" in texts
    assert "src/module.py" in texts


@pytest.mark.asyncio
async def test_completed_file_short_circuits_completions(tmp_path: Path):
    """Stop offering fuzzy matches once the fragment resolves to an existing file."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Agents\n")

    nested_dir = tmp_path / "src" / "pythinker_code" / "agents"
    nested_dir.mkdir(parents=True)
    (nested_dir / "README.md").write_text("nested\n")

    texts = await _completion_texts(tmp_path, "@AGENTS.md")

    assert not texts


@pytest.mark.asyncio
async def test_limit_is_enforced(tmp_path: Path):
    """Respect the configured limit when building top-level candidates."""
    for index in range(10):
        (tmp_path / f"dir{index}").mkdir()
    for index in range(10):
        (tmp_path / f"file{index}.txt").write_text("x")

    limit = 8
    texts = await _completion_texts(tmp_path, "@", limit=limit)

    assert len(set(texts)) == limit


@pytest.mark.asyncio
async def test_at_guard_prevents_email_like_fragments(tmp_path: Path):
    """Ignore `@` that are embedded inside identifiers (e.g. emails)."""
    (tmp_path / "example.py").write_text("")

    texts = await _completion_texts(tmp_path, "email@example.com")

    assert not texts


def test_file_context_matrix_for_boundaries_quotes_and_cursor_position():
    quoted = parse_completion_context(Document('@"docs/design notes.md'))
    punctuation = parse_completion_context(Document("see (@src/main.py"))
    email = parse_completion_context(Document("email@example.com"))
    escaped_whitespace = parse_completion_context(Document(r"@docs/design\ notes.md"))
    mid_token = parse_completion_context(Document(text="@src/main.py", cursor_position=len("@src")))

    assert (quoted.kind, quoted.token, quoted.quoted) == (
        CompletionKind.FILE,
        "docs/design notes.md",
        True,
    )
    assert (punctuation.kind, punctuation.token) == (CompletionKind.FILE, "src/main.py")
    assert email.kind is CompletionKind.NONE
    assert escaped_whitespace.kind is CompletionKind.NONE
    assert (mid_token.kind, mid_token.token) == (CompletionKind.FILE, "src")


@pytest.mark.asyncio
async def test_quoted_completion_retains_quotes_and_escapes_path(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    path = docs / 'design "notes"\\draft.md'
    path.write_text("notes\n")
    completions = await _completion_results(tmp_path, '@"docs/design')

    assert [completion.text for completion in completions] == [
        '"docs/design \\"notes\\"\\\\draft.md"'
    ]
    assert completions[0].start_position == -len('"docs/design')


@pytest.mark.asyncio
async def test_unicode_and_cjk_paths_complete(tmp_path: Path):
    (tmp_path / "café.md").write_text("accent\n")
    (tmp_path / "设计说明.md").write_text("CJK\n")
    assert "café.md" in await _completion_texts(tmp_path, "@caf")
    assert "设计说明.md" in await _completion_texts(tmp_path, "@设计")


@pytest.mark.asyncio
async def test_completed_quoted_file_short_circuits_completions(tmp_path: Path):
    (tmp_path / "design notes.md").write_text("done\n")
    assert not await _completion_texts(tmp_path, '@"design notes.md"')


@pytest.mark.asyncio
async def test_scoped_walk_finds_late_alphabetical_dirs(tmp_path: Path):
    """Directories that sort late alphabetically must still be reachable.

    Regression test for #1375: in large repos, ``os.walk`` exhausted the
    1000-file limit on early directories, making later ones (like ``src/``)
    invisible.  With scoped search (fragment contains ``/``), the walk starts
    at the target subtree.
    """
    # Create many early-alphabetical directories with files to exhaust a small limit.
    for i in range(20):
        d = tmp_path / f"aaa_{i:03d}"
        d.mkdir()
        for j in range(10):
            (d / f"file_{j}.txt").write_text("")

    # The target directory sorts late.
    target = tmp_path / "zzz_target"
    target.mkdir()
    (target / "important.py").write_text("# find me")

    # With a low limit, the old os.walk approach would never reach zzz_target.
    texts = await _completion_texts(tmp_path, "@zzz_target/", limit=50)

    assert "zzz_target/important.py" in texts


@pytest.mark.asyncio
async def test_basename_prefix_is_ranked_first(tmp_path: Path):
    """Prefer basename prefix matches over cross-segment fuzzy matches.

    For query 'fetch', we want '.../fetch.py' to appear before paths that only
    match by spreading characters across segments like 'file/patch.py'.
    """
    # Build a small tree mimicking the real project structure
    (tmp_path / "src" / "pythinker_code" / "tools" / "web").mkdir(parents=True)
    (tmp_path / "src" / "pythinker_code" / "tools" / "file").mkdir(parents=True)

    fetch_py = tmp_path / "src" / "pythinker_code" / "tools" / "web" / "fetch.py"
    fetch_py.write_text("# fetch\n")
    patch_py = tmp_path / "src" / "pythinker_code" / "tools" / "file" / "patch.py"
    patch_py.write_text("# patch\n")

    texts = await _completion_texts(tmp_path, "@fetch")

    # Snapshot the full candidate list to keep order/content deterministic
    assert texts == snapshot(
        [
            "src/pythinker_code/tools/web/fetch.py",
            "src/pythinker_code/tools/file/patch.py",
        ]
    )


@pytest.mark.asyncio
async def test_test_paths_are_ranked_after_source_matches(tmp_path: Path):
    """Prefer source files over equally relevant test paths."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "zzz_src").mkdir()
    (tmp_path / "tests" / "foo.py").write_text("# test\n")
    (tmp_path / "zzz_src" / "foo.py").write_text("# source\n")

    texts = await _completion_texts(tmp_path, "@foo")

    assert texts[:2] == ["zzz_src/foo.py", "tests/foo.py"]


def _init_git_repo(work_dir: Path) -> None:
    """Initialise a git repo, stage all files, and commit."""
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "init"],
    ):
        subprocess.run(cmd, cwd=work_dir, capture_output=True, check=True)


@pytest.mark.asyncio
async def test_tracked_ignored_dirs_filtered_in_git_mode(tmp_path: Path):
    """Tracked ``node_modules/`` and ``vendor/`` must still be filtered.

    Regression test: ``git ls-files`` returns all tracked paths, so
    directories in ``_IGNORED_NAMES`` were surfacing in completion when
    they happened to be committed.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("# app")
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("module.exports = {}")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "dep.py").write_text("# dep")

    _init_git_repo(tmp_path)

    texts = await _completion_texts(tmp_path, "@nod")
    assert not any("node_modules" in t for t in texts), (
        f"node_modules should be filtered even if tracked, got: {texts}"
    )

    texts = await _completion_texts(tmp_path, "@ven")
    assert not any("vendor" in t for t in texts), (
        f"vendor should be filtered even if tracked, got: {texts}"
    )


@pytest.mark.asyncio
async def test_unstaged_rename_hides_deleted_path(tmp_path: Path):
    """After ``mv old.py new.py`` without staging, old.py must not appear.

    Regression test: ``git ls-files`` reads the index, so a file that was
    moved on disk (but not staged) would still show up as a stale
    candidate.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "old.py").write_text("# original")

    _init_git_repo(tmp_path)

    # Rename without staging.
    (tmp_path / "src" / "old.py").rename(tmp_path / "src" / "new.py")

    texts = await _completion_texts(tmp_path, "@old")
    assert not any("old.py" in t for t in texts), (
        f"Deleted old.py should not appear in completion, got: {texts}"
    )

    texts = await _completion_texts(tmp_path, "@new")
    assert any("new.py" in t for t in texts), (
        f"Renamed new.py should appear via --others, got: {texts}"
    )
