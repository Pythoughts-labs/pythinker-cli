from __future__ import annotations

import platform
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from inline_snapshot import snapshot


def test_pyinstaller_datas():
    from pythinker_code.utils.pyinstaller import datas

    project_root = Path(__file__).parent.parent.parent
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = f".venv/lib/python{python_version}/site-packages"
    rg_binary = "rg.exe" if platform.system() == "Windows" else "rg"
    has_rg_binary = (project_root / "src/pythinker_code/deps/bin" / rg_binary).exists()
    datas = [
        (
            Path(path)
            .relative_to(project_root)
            .as_posix()
            .replace(f".venv/lib64/python{python_version}/site-packages", site_packages)
            .replace(".venv/Lib/site-packages", site_packages),
            Path(dst).as_posix(),
        )
        for path, dst in datas
    ]

    # justext ships ~100 per-language stoplists under justext/stoplists/. They
    # are bundled via collect_data_files("justext") so trafilatura's justext
    # fallback works in frozen builds (otherwise get_stoplists() does an
    # os.listdir() on a missing _MEIxxxx/justext/stoplists and crashes the first
    # Fetch). Like web/static, the set is large and version-dependent, so assert
    # it is collected here rather than pinning every language in expected_datas.
    justext_stoplists = [p for p, d in datas if d == "justext/stoplists"]
    assert justext_stoplists, "justext stoplists must be bundled for the native web Fetch tool"
    assert any(p.endswith("/stoplists/English.txt") for p in justext_stoplists), (
        "justext English stoplist missing from the PyInstaller datas"
    )

    # fastmcp and mcp call importlib.metadata.version() at module import time;
    # copy_metadata() bundles their dist-info so importlib.metadata can resolve
    # them in the frozen binary. The exact file set is version-dependent (like
    # justext stoplists), so assert METADATA presence rather than pinning every
    # file. copy_metadata() produces clean paths ({dist-info}/FILE) — no
    # fastmcp/../ prefix that the old collect_data_files workaround created.
    # copy_metadata() returns a single directory entry per package (the whole
    # dist-info dir), so check that the dest dir name matches — not a file inside it.
    for pkg in ("fastmcp", "mcp"):
        pkg_dist = f"{pkg}-{version(pkg)}.dist-info"
        assert any(d == pkg_dist for p, d in datas), (
            f"{pkg_dist} must be bundled so importlib.metadata works in the frozen app"
        )

    dist_info_dirs = {f"{pkg}-{version(pkg)}.dist-info" for pkg in ("fastmcp", "mcp")}
    datas = [
        (p, d)
        for p, d in datas
        if "web/static" not in d
        and "dashboard/static" not in d
        and d != "justext/stoplists"
        and not any(d == di or d.startswith(di + "/") for di in dist_info_dirs)
    ]

    expected_datas = [
        (
            f"{site_packages}/trafilatura/data/tei_corpus.dtd",
            "trafilatura/data",
        ),
        (
            f"{site_packages}/trafilatura/py.typed",
            "trafilatura",
        ),
        (
            f"{site_packages}/trafilatura/settings.cfg",
            "trafilatura",
        ),
        (
            "src/pythinker_code/CHANGELOG.md",
            "pythinker_code",
        ),
        ("src/pythinker_code/agents/default/agent.yaml", "pythinker_code/agents/default"),
        ("src/pythinker_code/agents/default/ask.yaml", "pythinker_code/agents/default"),
        (
            "src/pythinker_code/agents/default/code_reviewer.yaml",
            "pythinker_code/agents/default",
        ),
        ("src/pythinker_code/agents/default/coder.yaml", "pythinker_code/agents/default"),
        ("src/pythinker_code/agents/default/debug.yaml", "pythinker_code/agents/default"),
        ("src/pythinker_code/agents/default/debugger.yaml", "pythinker_code/agents/default"),
        ("src/pythinker_code/agents/default/explore.yaml", "pythinker_code/agents/default"),
        ("src/pythinker_code/agents/default/implementer.yaml", "pythinker_code/agents/default"),
        ("src/pythinker_code/agents/default/judge.yaml", "pythinker_code/agents/default"),
        ("src/pythinker_code/agents/default/plan.yaml", "pythinker_code/agents/default"),
        ("src/pythinker_code/agents/default/planner.yaml", "pythinker_code/agents/default"),
        ("src/pythinker_code/agents/default/review.yaml", "pythinker_code/agents/default"),
        ("src/pythinker_code/agents/default/scout.yaml", "pythinker_code/agents/default"),
        (
            "src/pythinker_code/agents/default/security_reviewer.yaml",
            "pythinker_code/agents/default",
        ),
        ("src/pythinker_code/agents/default/system.md", "pythinker_code/agents/default"),
        ("src/pythinker_code/agents/default/verifier.yaml", "pythinker_code/agents/default"),
        ("src/pythinker_code/agents/okabe/agent.yaml", "pythinker_code/agents/okabe"),
        ("src/pythinker_code/prompts/best_practices.md", "pythinker_code/prompts"),
        ("src/pythinker_code/prompts/compact.md", "pythinker_code/prompts"),
        ("src/pythinker_code/prompts/goal_continuation.md", "pythinker_code/prompts"),
        ("src/pythinker_code/prompts/goal_set.md", "pythinker_code/prompts"),
        ("src/pythinker_code/prompts/goal_wrap_up.md", "pythinker_code/prompts"),
        ("src/pythinker_code/prompts/init.md", "pythinker_code/prompts"),
        ("src/pythinker_code/prompts/learn.md", "pythinker_code/prompts"),
        (
            "src/pythinker_code/skills/agent-creator/SKILL.md",
            "pythinker_code/skills/agent-creator",
        ),
        (
            "src/pythinker_code/skills/check-impl-against-spec/SKILL.md",
            "pythinker_code/skills/check-impl-against-spec",
        ),
        (
            "src/pythinker_code/skills/customize-pythinker/SKILL.md",
            "pythinker_code/skills/customize-pythinker",
        ),
        (
            "src/pythinker_code/skills/create-pr/SKILL.md",
            "pythinker_code/skills/create-pr",
        ),
        (
            "src/pythinker_code/skills/designer-skill/SKILL.md",
            "pythinker_code/skills/designer-skill",
        ),
        (
            "src/pythinker_code/skills/diagnose-ci-failures/SKILL.md",
            "pythinker_code/skills/diagnose-ci-failures",
        ),
        (
            "src/pythinker_code/skills/fix-errors/SKILL.md",
            "pythinker_code/skills/fix-errors",
        ),
        (
            "src/pythinker_code/skills/implement-specs/SKILL.md",
            "pythinker_code/skills/implement-specs",
        ),
        (
            "src/pythinker_code/skills/pr-walkthrough/SKILL.md",
            "pythinker_code/skills/pr-walkthrough",
        ),
        (
            "src/pythinker_code/skills/pythinker-code-help/SKILL.md",
            "pythinker_code/skills/pythinker-code-help",
        ),
        (
            "src/pythinker_code/skills/reproduce-bug-report/SKILL.md",
            "pythinker_code/skills/reproduce-bug-report",
        ),
        (
            "src/pythinker_code/skills/resolve-merge-conflicts/SKILL.md",
            "pythinker_code/skills/resolve-merge-conflicts",
        ),
        (
            "src/pythinker_code/skills/review-pr/SKILL.md",
            "pythinker_code/skills/review-pr",
        ),
        (
            "src/pythinker_code/skills/skill-creator/SKILL.md",
            "pythinker_code/skills/skill-creator",
        ),
        (
            "src/pythinker_code/skills/spec-driven-implementation/SKILL.md",
            "pythinker_code/skills/spec-driven-implementation",
        ),
        (
            "src/pythinker_code/skills/write-product-spec/SKILL.md",
            "pythinker_code/skills/write-product-spec",
        ),
        (
            "src/pythinker_code/skills/write-tech-spec/SKILL.md",
            "pythinker_code/skills/write-tech-spec",
        ),
        ("src/pythinker_code/tools/agent/description.md", "pythinker_code/tools/agent"),
        ("src/pythinker_code/tools/ask_user/description.md", "pythinker_code/tools/ask_user"),
        (
            "src/pythinker_code/tools/dmail/dmail.md",
            "pythinker_code/tools/dmail",
        ),
        ("src/pythinker_code/tools/background/handoff.md", "pythinker_code/tools/background"),
        ("src/pythinker_code/tools/background/input.md", "pythinker_code/tools/background"),
        ("src/pythinker_code/tools/background/list.md", "pythinker_code/tools/background"),
        ("src/pythinker_code/tools/background/output.md", "pythinker_code/tools/background"),
        ("src/pythinker_code/tools/background/stop.md", "pythinker_code/tools/background"),
        (
            "src/pythinker_code/tools/file/glob.md",
            "pythinker_code/tools/file",
        ),
        (
            "src/pythinker_code/tools/file/grep.md",
            "pythinker_code/tools/file",
        ),
        (
            "src/pythinker_code/tools/file/read.md",
            "pythinker_code/tools/file",
        ),
        (
            "src/pythinker_code/tools/file/read_media.md",
            "pythinker_code/tools/file",
        ),
        (
            "src/pythinker_code/tools/file/replace.md",
            "pythinker_code/tools/file",
        ),
        (
            "src/pythinker_code/tools/file/write.md",
            "pythinker_code/tools/file",
        ),
        (
            "src/pythinker_code/tools/goal/update_goal.md",
            "pythinker_code/tools/goal",
        ),
        (
            "src/pythinker_code/tools/mcp_resource/list_description.md",
            "pythinker_code/tools/mcp_resource",
        ),
        (
            "src/pythinker_code/tools/mcp_resource/read_description.md",
            "pythinker_code/tools/mcp_resource",
        ),
        ("src/pythinker_code/tools/memory/memory.md", "pythinker_code/tools/memory"),
        ("src/pythinker_code/tools/recall/description.md", "pythinker_code/tools/recall"),
        (
            "src/pythinker_code/tools/scratchpad/scratchpad_tool.md",
            "pythinker_code/tools/scratchpad",
        ),
        ("src/pythinker_code/tools/plan/description.md", "pythinker_code/tools/plan"),
        ("src/pythinker_code/tools/plan/enter_description.md", "pythinker_code/tools/plan"),
        ("src/pythinker_code/tools/progress/description.md", "pythinker_code/tools/progress"),
        ("src/pythinker_code/tools/suggest/description.md", "pythinker_code/tools/suggest"),
        ("src/pythinker_code/tools/shell/bash.md", "pythinker_code/tools/shell"),
        ("src/pythinker_code/tools/shell/powershell.md", "pythinker_code/tools/shell"),
        ("src/pythinker_code/tools/skill/description.md", "pythinker_code/tools/skill"),
        (
            "src/pythinker_code/tools/think/think.md",
            "pythinker_code/tools/think",
        ),
        (
            "src/pythinker_code/tools/todo/set_todo_list.md",
            "pythinker_code/tools/todo",
        ),
        (
            "src/pythinker_code/tools/web/fetch.md",
            "pythinker_code/tools/web",
        ),
        (
            "src/pythinker_code/tools/tool_search/tool_search.md",
            "pythinker_code/tools/tool_search",
        ),
        (
            "src/pythinker_code/tools/web/search.md",
            "pythinker_code/tools/web",
        ),
        (
            "src/pythinker_code/tools/worktree/enter_worktree.md",
            "pythinker_code/tools/worktree",
        ),
        (
            "src/pythinker_code/tools/worktree/exit_worktree.md",
            "pythinker_code/tools/worktree",
        ),
    ]
    if has_rg_binary:
        expected_datas.append(
            (f"src/pythinker_code/deps/bin/{rg_binary}", "pythinker_code/deps/bin")
        )

    assert sorted(datas) == sorted(expected_datas)


def test_pyinstaller_hiddenimports():
    import pkgutil

    import pygments.styles

    from pythinker_code.utils.pyinstaller import hiddenimports

    # Pygments style list is owned by the Pygments package and changes with upgrades;
    # enumerate actual sub-modules (matching collect_submodules) rather than the style
    # registry names (get_all_styles uses hyphens, modules use underscores).
    expected_pygments = {"pygments.styles"} | {
        f"pygments.styles.{mod.name}" for mod in pkgutil.iter_modules(pygments.styles.__path__)
    }
    assert expected_pygments <= set(hiddenimports)

    project_entries = sorted(set(hiddenimports) - expected_pygments)
    assert project_entries == snapshot(
        [
            "pythinker_code.cli._lazy_group",
            "pythinker_code.cli.dashboard",
            "pythinker_code.cli.debug",
            "pythinker_code.cli.export",
            "pythinker_code.cli.info",
            "pythinker_code.cli.mcp",
            "pythinker_code.cli.plugin",
            "pythinker_code.cli.review",
            "pythinker_code.cli.secscan",
            "pythinker_code.cli.security_scan",
            "pythinker_code.cli.skill",
            "pythinker_code.cli.system_prompt",
            "pythinker_code.cli.update",
            "pythinker_code.cli.web",
            "pythinker_code.tools",
            "pythinker_code.tools.agent",
            "pythinker_code.tools.ask_user",
            "pythinker_code.tools.background",
            "pythinker_code.tools.display",
            "pythinker_code.tools.dmail",
            "pythinker_code.tools.file",
            "pythinker_code.tools.file.glob",
            "pythinker_code.tools.file.grep_local",
            "pythinker_code.tools.file.plan_mode",
            "pythinker_code.tools.file.read",
            "pythinker_code.tools.file.read_media",
            "pythinker_code.tools.file.replace",
            "pythinker_code.tools.file.utils",
            "pythinker_code.tools.file.write",
            "pythinker_code.tools.goal",
            "pythinker_code.tools.mcp_resource",
            "pythinker_code.tools.memory",
            "pythinker_code.tools.memory.routing_guard",
            "pythinker_code.tools.plan",
            "pythinker_code.tools.plan.enter",
            "pythinker_code.tools.plan.handoff",
            "pythinker_code.tools.plan.heroes",
            "pythinker_code.tools.progress",
            "pythinker_code.tools.recall",
            "pythinker_code.tools.scratchpad",
            "pythinker_code.tools.shell",
            "pythinker_code.tools.skill",
            "pythinker_code.tools.skill._mcp_bridge",
            "pythinker_code.tools.suggest",
            "pythinker_code.tools.test",
            "pythinker_code.tools.think",
            "pythinker_code.tools.todo",
            "pythinker_code.tools.tool_search",
            "pythinker_code.tools.utils",
            "pythinker_code.tools.web",
            "pythinker_code.tools.web._allowlist",
            "pythinker_code.tools.web.fetch",
            "pythinker_code.tools.web.search",
            "pythinker_code.tools.worktree",
            "setproctitle",
        ]
    )


def test_pyinstaller_hiddenimports_include_lazy_cli_subcommands():
    from pythinker_code.cli._lazy_group import LazySubcommandGroup
    from pythinker_code.utils.pyinstaller import hiddenimports

    expected_hiddenimports = {
        module_name
        for module_name, _attribute_name, _help_text in LazySubcommandGroup.lazy_subcommands.values()
    }

    assert expected_hiddenimports <= set(hiddenimports)


def test_require_ui_assets_accepts_built_tree(tmp_path: Path) -> None:
    from pythinker_code.utils.pyinstaller import require_ui_assets

    for rel in ("web/static/index.html", "dashboard/static/index.html"):
        target = tmp_path / rel
        target.parent.mkdir(parents=True)
        target.write_text("<!doctype html>")

    require_ui_assets(tmp_path)  # must not raise


def test_require_ui_assets_rejects_unbuilt_tree(tmp_path: Path) -> None:
    """A freeze without the gitignored web/dashboard bundles ships a web UI that
    404s on "/"; the guard must abort the build with an actionable message."""
    from pythinker_code.utils.pyinstaller import require_ui_assets

    with pytest.raises(SystemExit, match="build-web"):
        require_ui_assets(tmp_path)
