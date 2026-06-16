from __future__ import annotations

from collections.abc import Callable

from pythinker_code.ui.shell.selector import SelectorConfig, SelectorItem, run_selector


def _build_code_theme_config(
    current_theme: str,
    available_themes: list[str],
    on_preview: Callable[[str], None] | None = None,
    *,
    theme_matches_current: Callable[[str, str], bool] | None = None,
) -> SelectorConfig[str]:
    matches = theme_matches_current or (lambda theme, configured: theme == configured)
    return SelectorConfig(
        title="Select syntax theme",
        items=[
            SelectorItem(
                value=theme,
                label=theme,
                is_current=matches(theme, current_theme),
            )
            for theme in available_themes
        ],
        on_change=on_preview,
    )


async def run_code_theme_selector(
    current_theme: str,
    available_themes: list[str],
    on_preview: Callable[[str], None] | None = None,
    *,
    theme_matches_current: Callable[[str, str], bool] | None = None,
) -> str | None:
    return await run_selector(
        _build_code_theme_config(
            current_theme,
            available_themes,
            on_preview,
            theme_matches_current=theme_matches_current,
        )
    )
