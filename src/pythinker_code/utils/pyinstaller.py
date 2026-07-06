from __future__ import annotations

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

from pythinker_code.cli._lazy_group import LazySubcommandGroup

# The web/dashboard frontends are gitignored build artifacts synced into the package
# by scripts/build_web.py and scripts/build_dashboard.py. collect_data_files()
# silently collects nothing for missing globs, which froze builds whose web UI
# answered "/" with a 404 (Windows/Linux native installers).
_REQUIRED_UI_ASSETS = ("web/static/index.html", "dashboard/static/index.html")


def require_ui_assets(package_root: Path | None = None) -> None:
    """Abort the freeze when the web/dashboard UI bundles haven't been built."""
    if package_root is None:
        package_root = Path(__file__).resolve().parents[1]
    missing = [rel for rel in _REQUIRED_UI_ASSETS if not (package_root / rel).is_file()]
    if missing:
        raise SystemExit(
            "PyInstaller build aborted: UI bundles missing from pythinker_code "
            f"({', '.join(missing)}). They are gitignored build artifacts; run "
            "`make build-web build-dashboard` before freezing, or the packaged web UI "
            "will 404 on '/'."
        )


lazy_cli_hiddenimports = [
    module_name
    for module_name, _attribute_name, _help_text in (LazySubcommandGroup.lazy_subcommands.values())
]

hiddenimports = (
    collect_submodules("pythinker_code.tools")
    + lazy_cli_hiddenimports
    # `cli/__init__.py` resolves _lazy_group via `import_module(f"{__name__}._lazy_group")`,
    # which PyInstaller's static analysis can't follow.
    + ["pythinker_code.cli._lazy_group", "setproctitle"]
    # Pygments resolves a style module dynamically (e.g. `import
    # pygments.styles.monokai`) when config.tui.code_theme names a stock style,
    # so static analysis misses it and the frozen binary raises ClassNotFound.
    # Collect all style modules so any opted-in code_theme resolves.
    + collect_submodules("pygments.styles")
)
datas = (
    collect_data_files(
        "pythinker_code",
        includes=[
            "agents/**/*.yaml",
            "agents/**/*.md",
            "benchmark/bundled/**/*.json",
            "deps/bin/**",
            "prompts/**/*.md",
            "skills/**",
            "tools/**/*.md",
            "web/static/**",
            "dashboard/static/**",
            "CHANGELOG.md",
        ],
        excludes=[
            "tools/*.md",
        ],
    )
    + collect_data_files(
        "dateparser",
        includes=["**/*.pkl"],
    )
    # fastmcp calls importlib.metadata.version("fastmcp") at module import time.
    # copy_metadata() is the PyInstaller-standard hook for making
    # importlib.metadata work in frozen apps; it bundles the dist-info sibling
    # directory that collect_data_files() silently skips.
    + copy_metadata("fastmcp")
    + copy_metadata("mcp")
    + collect_data_files("trafilatura")
    # justext is trafilatura's fallback extractor. It loads its per-language
    # stoplists by os.listdir()-ing justext/stoplists/, so without the data
    # files the frozen web Fetch tool crashes on a missing
    # _MEIxxxx/justext/stoplists directory the first time extraction falls back.
    + collect_data_files("justext")
)
