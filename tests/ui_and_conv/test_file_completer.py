"""Tests for the shell file mention completer."""

from __future__ import annotations

import subprocess
from pathlib import Path

from inline_snapshot import snapshot
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from pythinker_code.ui.shell.prompt import LocalFileMentionCompleter
from pythinker_code.ui.shell.prompting.completion.context import (
    CompletionKind,
    parse_completion_context,
)


def _completion_texts(completer: LocalFileMentionCompleter, text: str) -> list[str]:
    document = Document(text=text, cursor_position=len(text))
    event = CompleteEvent(completion_requested=True)
    return [completion.text for completion in completer.get_completions(document, event)]


def test_top_level_paths_skip_ignored_names(tmp_path: Path):
    """Only surface non-ignored entries when completing the top level."""
    (tmp_path / "src").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / ".DS_Store").write_text("")
    (tmp_path / "README.md").write_text("hello")

    completer = LocalFileMentionCompleter(tmp_path)

    texts = _completion_texts(completer, "@")

    assert "src/" in texts
    assert "README.md" in texts
    assert "node_modules/" not in texts
    assert ".DS_Store" not in texts


def test_directory_completion_continues_after_slash(tmp_path: Path):
    """Continue descending when the fragment ends with a slash."""
    src = tmp_path / "src"
    src.mkdir()
    nested = src / "module.py"
    nested.write_text("print('hi')\n")

    completer = LocalFileMentionCompleter(tmp_path)

    texts = _completion_texts(completer, "@src/")

    assert "src/" in texts
    assert "src/module.py" in texts


def test_completed_file_short_circuits_completions(tmp_path: Path):
    """Stop offering fuzzy matches once the fragment resolves to an existing file."""
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Agents\n")

    nested_dir = tmp_path / "src" / "pythinker_code" / "agents"
    nested_dir.mkdir(parents=True)
    (nested_dir / "README.md").write_text("nested\n")

    completer = LocalFileMentionCompleter(tmp_path)

    texts = _completion_texts(completer, "@AGENTS.md")

    assert not texts


def test_limit_is_enforced(tmp_path: Path):
    """Respect the configured limit when building top-level candidates."""
    for index in range(10):
        (tmp_path / f"dir{index}").mkdir()
    for index in range(10):
        (tmp_path / f"file{index}.txt").write_text("x")

    limit = 8
    completer = LocalFileMentionCompleter(tmp_path, limit=limit)

    texts = _completion_texts(completer, "@")

    assert len(set(texts)) == limit


def test_at_guard_prevents_email_like_fragments(tmp_path: Path):
    """Ignore `@` that are embedded inside identifiers (e.g. emails)."""
    (tmp_path / "example.py").write_text("")

    completer = LocalFileMentionCompleter(tmp_path)

    texts = _completion_texts(completer, "email@example.com")

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


def test_quoted_completion_retains_quotes_and_escapes_path(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    path = docs / 'design "notes"\\draft.md'
    path.write_text("notes\n")
    completer = LocalFileMentionCompleter(tmp_path)
    document = Document('@"docs/design')
    event = CompleteEvent(completion_requested=True)

    completions = list(completer.get_completions(document, event))

    assert [completion.text for completion in completions] == [
        '"docs/design \\"notes\\"\\\\draft.md"'
    ]
    assert completions[0].start_position == -len('"docs/design')


def test_unicode_and_cjk_paths_complete(tmp_path: Path):
    (tmp_path / "café.md").write_text("accent\n")
    (tmp_path / "设计说明.md").write_text("CJK\n")
    completer = LocalFileMentionCompleter(tmp_path)

    assert "café.md" in _completion_texts(completer, "@caf")
    assert "设计说明.md" in _completion_texts(completer, "@设计")


def test_completed_quoted_file_short_circuits_completions(tmp_path: Path):
    (tmp_path / "design notes.md").write_text("done\n")
    completer = LocalFileMentionCompleter(tmp_path)

    assert not _completion_texts(completer, '@"design notes.md"')


def test_scoped_walk_finds_late_alphabetical_dirs(tmp_path: Path):
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
    completer = LocalFileMentionCompleter(tmp_path, limit=50)

    texts = _completion_texts(completer, "@zzz_target/")

    assert "zzz_target/important.py" in texts


def test_basename_prefix_is_ranked_first(tmp_path: Path):
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

    completer = LocalFileMentionCompleter(tmp_path)

    texts = _completion_texts(completer, "@fetch")

    # Snapshot the full candidate list to keep order/content deterministic
    assert texts == snapshot(
        [
            "src/pythinker_code/tools/web/fetch.py",
            "src/pythinker_code/tools/file/patch.py",
        ]
    )


def test_test_paths_are_ranked_after_source_matches(tmp_path: Path):
    """Prefer source files over equally relevant test paths."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "zzz_src").mkdir()
    (tmp_path / "tests" / "foo.py").write_text("# test\n")
    (tmp_path / "zzz_src" / "foo.py").write_text("# source\n")

    completer = LocalFileMentionCompleter(tmp_path)

    texts = _completion_texts(completer, "@foo")

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


def test_tracked_ignored_dirs_filtered_in_git_mode(tmp_path: Path):
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

    completer = LocalFileMentionCompleter(tmp_path)

    texts = _completion_texts(completer, "@nod")
    assert not any("node_modules" in t for t in texts), (
        f"node_modules should be filtered even if tracked, got: {texts}"
    )

    texts = _completion_texts(completer, "@ven")
    assert not any("vendor" in t for t in texts), (
        f"vendor should be filtered even if tracked, got: {texts}"
    )


def test_unstaged_rename_hides_deleted_path(tmp_path: Path):
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

    completer = LocalFileMentionCompleter(tmp_path)

    texts = _completion_texts(completer, "@old")
    assert not any("old.py" in t for t in texts), (
        f"Deleted old.py should not appear in completion, got: {texts}"
    )

    texts = _completion_texts(completer, "@new")
    assert any("new.py" in t for t in texts), (
        f"Renamed new.py should appear via --others, got: {texts}"
    )
