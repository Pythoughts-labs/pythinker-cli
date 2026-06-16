from __future__ import annotations

import asyncio
import json
import shlex
from typing import TYPE_CHECKING

from rich.markup import escape

from pythinker_code.auth.platforms import parse_managed_provider_key
from pythinker_code.config import LLMProvider
from pythinker_code.models_dev import refresh_catalog as _refresh_catalog
from pythinker_code.soul.pythinkersoul import PythinkerSoul
from pythinker_code.ui.shell.console import console
from pythinker_code.ui.shell.slash import registry
from pythinker_code.ui.shell.stats_collector import AllStats
from pythinker_code.ui.shell.stats_collector import load_all_stats as _load_all_stats_raw
from pythinker_code.ui.shell.usage_activity import (
    TokenActivityView,
)
from pythinker_code.ui.shell.usage_activity import (
    load_activity as _load_activity,
)
from pythinker_code.ui.shell.usage_activity import (
    parse_view as _parse_activity_view,
)
from pythinker_code.ui.shell.usage_activity import (
    render_activity as _render_activity,
)
from pythinker_code.ui.shell.usage_adapters import ADAPTERS
from pythinker_code.ui.shell.usage_adapters.base import (
    UsageAdapter,
    UsageReport,
    UsageRow,
    used_from_remaining,
)
from pythinker_code.ui.shell.usage_adapters.pythinker import to_int as _to_int
from pythinker_code.ui.shell.usage_render import (
    build_panel,
)
from pythinker_code.ui.shell.usage_render import (
    format_row as _format_row,
)
from pythinker_code.ui.shell.usage_render import (
    ratio_color as _ratio_color,
)
from pythinker_code.ui.shell.usage_render import (
    remaining_quota as _remaining_quota,
)
from pythinker_code.ui.theme import get_tui_tokens as _get_tui_tokens
from pythinker_code.usage_ratelimit_cache import get_cache
from pythinker_code.utils.datetime import format_duration
from pythinker_code.utils.logging import logger

if TYPE_CHECKING:
    from pythinker_code.auth.oauth import OAuthManager
    from pythinker_code.ui.shell import Shell

__all__ = [
    "UsageRow",
    "_format_row",
    "_ratio_color",
    "_remaining_quota",
    "_gather_reports",
    "_print_json",
    "_print_no_usage_providers",
    "_select_providers",
    "_to_int",
    "usage",
]


def _select_providers(
    config_providers: dict[str, LLMProvider],
    registered_platform_ids: set[str],
    filter_provider_key: str | None,
) -> list[tuple[str, LLMProvider]]:
    providers = (
        [(filter_provider_key, config_providers[filter_provider_key])]
        if filter_provider_key in config_providers
        else list(config_providers.items())
        if filter_provider_key is None
        else []
    )

    pairs: list[tuple[str, LLMProvider]] = []
    for provider_key, provider in providers:
        platform_id = parse_managed_provider_key(provider_key)
        if platform_id in registered_platform_ids:
            pairs.append((platform_id, provider))
    return pairs


async def _fetch_report(
    adapter: UsageAdapter,
    provider: LLMProvider,
    oauth_mgr: OAuthManager,
) -> UsageReport:
    try:
        return await adapter.fetch(provider, oauth_mgr)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return UsageReport(
            provider_label=getattr(adapter, "provider_label", adapter.platform_id),
            summary=None,
            limits=[],
            notes=[f"Adapter raised: {e!r}"],
        )


async def _gather_reports(
    pairs: list[tuple[UsageAdapter, LLMProvider]],
    oauth_mgr: OAuthManager,
) -> list[UsageReport]:
    return list(
        await asyncio.gather(
            *(_fetch_report(adapter, provider, oauth_mgr) for adapter, provider in pairs)
        )
    )


def _print_json(value: object) -> None:
    console.file.write(json.dumps(value, indent=2))
    console.file.write("\n")
    console.file.flush()


def _print_no_usage_providers(json_mode: bool) -> None:
    if json_mode:
        _print_json([])
        return
    _t = _get_tui_tokens()
    console.print(f"[{_t.warning}]No providers with usage support are configured.[/]")


def _ratelimit_fallback_report(provider_key: str, provider_label: str) -> UsageReport | None:
    """Build a 'live rate limits' UsageReport from the most recent snapshot
    captured for `provider_key`, or None when nothing has been recorded yet."""
    snap = get_cache().snapshot(provider_key)
    if snap is None:
        return None

    rows: list[UsageRow] = []
    if snap.requests_limit is not None and snap.requests_remaining is not None:
        rows.append(
            UsageRow(
                label="Requests",
                used=used_from_remaining(snap.requests_limit, snap.requests_remaining),
                limit=snap.requests_limit,
                unit="requests",
                reset_hint=_seconds_to_reset_hint(snap.requests_reset_seconds),
            )
        )
    if snap.tokens_limit is not None and snap.tokens_remaining is not None:
        rows.append(
            UsageRow(
                label="Tokens",
                used=used_from_remaining(snap.tokens_limit, snap.tokens_remaining),
                limit=snap.tokens_limit,
                unit="tokens",
                reset_hint=_seconds_to_reset_hint(snap.tokens_reset_seconds),
            )
        )

    if not rows:
        return None

    return UsageReport(
        provider_label=f"{provider_label} (live rate limits)",
        summary=rows[0],
        limits=rows[1:],
        notes=[
            "Captured from the most recent chat-completion response headers — "
            "no dedicated usage adapter is available for this provider yet."
        ],
        unit_hint="rate-limit",
    )


def _seconds_to_reset_hint(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    if seconds <= 0:
        return "reset"
    return f"resets in {format_duration(int(seconds))}"


def _enrich_with_ratelimit_fallback(
    reports: list[UsageReport],
    selected: list[tuple[str, LLMProvider]],
) -> list[UsageReport]:
    """Merge live rate-limit cache rows into reports that lack real data.

    A "real data" report has a `summary` or non-empty `limits`. Anything
    without that — including notes-only reports from best-effort adapters
    (MiniMax, OpenCode Go, Pythinker AI) — gets the cached rate-limit rows
    appended, while the adapter's notes (which usually explain *why* there's
    no real data) are preserved.
    """
    enriched: list[UsageReport] = []
    for report, (platform_id, _) in zip(reports, selected, strict=True):
        if report.summary is not None or report.limits:
            enriched.append(report)
            continue
        fallback = _ratelimit_fallback_report(f"managed:{platform_id}", report.provider_label)
        if fallback is None:
            enriched.append(report)
            continue
        enriched.append(
            UsageReport(
                provider_label=fallback.provider_label,
                summary=fallback.summary,
                limits=fallback.limits,
                # Adapter notes first so the user reads "why" before "what".
                notes=[*report.notes, *fallback.notes],
                unit_hint=fallback.unit_hint,
            )
        )
    return enriched


def _load_cost_stats() -> AllStats | None:
    """Return AllStats from wire files, or None if no data or error."""
    try:
        stats = _load_all_stats_raw()
        if stats.periods["all_time"].total_messages == 0:
            return None
        return stats
    except Exception:
        return None


def _build_cost_panel(stats: AllStats):
    from rich import box as _box
    from rich.panel import Panel as _Panel
    from rich.table import Table as _Table

    from pythinker_code.ui.shell.stats import fmt_cost
    from pythinker_code.ui.theme import tui_rich_style

    t = _Table.grid(padding=(0, 2))
    t.add_column(style=tui_rich_style("info"))
    t.add_column(style="bold")

    periods = [
        ("Today", stats.periods["today"].total_cost),
        ("This Week", stats.periods["this_week"].total_cost),
        ("All time", stats.periods["all_time"].total_cost),
    ]
    for label, cost in periods:
        t.add_row(label, fmt_cost(cost))

    return _Panel(
        t,
        title="Session Cost",
        border_style=tui_rich_style("border_muted"),
        box=_box.ROUNDED,
        padding=(0, 2),
        expand=False,
    )


async def _maybe_print_cost_panel() -> None:
    """Print the session cost panel if local usage data exists. Never raises."""
    try:
        stats = await asyncio.to_thread(_load_cost_stats)
        if stats is None:
            return
        console.print(_build_cost_panel(stats))
    except Exception as e:
        logger.debug("cost panel failed to render: {error}", error=e, exc_info=True)


@registry.command(aliases=["status", "cost", "/status"], available_during_task=True)
async def usage(app: Shell, args: str):
    """Display usage for the current model's provider.

    Pass `all` for every provider, or a provider key to filter. Pass
    `daily`, `weekly`, or `cumulative` to render the token activity card
    driven by local session history.
    """
    assert isinstance(app.soul, PythinkerSoul)
    await _refresh_catalog()

    _t = _get_tui_tokens()
    try:
        tokens = shlex.split(args.strip())
    except ValueError as e:
        console.print(f"[{_t.error}]Invalid usage arguments: {escape(str(e))}[/]")
        return

    json_mode = "--json" in tokens
    positional = [token for token in tokens if token != "--json"]
    # `daily|weekly|cumulative` is the token activity card; it is not a
    # provider filter, so route to it before we try to interpret the argument
    # as a managed provider key.
    if positional and _parse_activity_view(positional[0]) is not None:
        await _print_activity_card(positional[0], json_mode=json_mode)
        return
    scoped_to_active = False
    active_provider_key: str | None = None
    if positional:
        # `/usage all` → all providers; `/usage <key>` → just that one.
        filter_provider_key = None if positional[0] == "all" else positional[0]
    else:
        # `/usage` with no args → scope to the active model's provider so
        # users with multiple providers configured don't see a wall of
        # unrelated reports. Falls back to all providers when no model is
        # active (e.g. before /login).
        llm = app.soul.runtime.llm
        if llm is not None and llm.model_config is not None:
            active_provider_key = llm.model_config.provider
            scoped_to_active = True
        filter_provider_key = active_provider_key

    selected = _select_providers(
        app.soul.runtime.config.providers,
        registered_platform_ids=set(ADAPTERS),
        filter_provider_key=filter_provider_key,
    )
    if not selected:
        # No dedicated adapter for the active provider (e.g. MiniMax,
        # OpenCode Go). Try the live-rate-limit cache as a universal
        # fallback before giving up — chat-completion responses populate
        # it for every openai-shape provider.
        if scoped_to_active and active_provider_key in app.soul.runtime.config.providers:
            platform_id = parse_managed_provider_key(active_provider_key)
            fallback = _ratelimit_fallback_report(active_provider_key, platform_id or "Provider")
            if fallback is not None:
                if json_mode:
                    _print_json([fallback.to_dict()])
                else:
                    console.print(build_panel(fallback))
                return
            if json_mode:
                _print_json([])
            else:
                console.print(
                    f"[{_t.warning}]Usage tracking is not yet available for the active "
                    f"provider ({active_provider_key}). Send a message first so "
                    f"live rate-limit headers can be captured, or run "
                    f"[bold]/usage all[/bold] to see other configured providers."
                    f"[/]"
                )
            return
        _print_no_usage_providers(json_mode)
        return

    pairs = [(ADAPTERS[platform_id], provider) for platform_id, provider in selected]

    if json_mode:
        reports = await _gather_reports(pairs, app.soul.runtime.oauth)
        reports = _enrich_with_ratelimit_fallback(reports, selected)
        _print_json([r.to_dict() for r in reports])
        return

    with console.status(f"[{_t.info}]Fetching usage...[/]"):
        reports = await _gather_reports(pairs, app.soul.runtime.oauth)

    # Swap any empty primary report for a live-rate-limit fallback when one
    # has been captured for that provider's chat completions.
    reports = _enrich_with_ratelimit_fallback(reports, selected)

    non_empty_reports = [report for report in reports if not report.is_empty]
    if not non_empty_reports:
        # When scoped to a single active provider (no positional arg), always
        # render the report even if empty — otherwise the user sees a flat
        # "No usage data available." with no idea which provider was queried
        # or what the adapter actually got back.
        if scoped_to_active and len(reports) == 1:
            console.print(build_panel(reports[0]))
            return
        console.print(f"[{_t.warning}]No usage data available.[/]")
        return

    for report in non_empty_reports:
        console.print(build_panel(report))

    await _maybe_print_cost_panel()


async def _print_activity_card(arg: str, *, json_mode: bool) -> None:
    """Render the token activity card.

    Falls back to a single-line warning if the user asks for an activity
    view but the rich console is unavailable (e.g. the JSON mode test path
    that bypasses the live render). The chart itself is intentionally
    text-only; structured JSON output is reserved for the per-provider
    adapter reports and is not part of the activity card surface.
    """

    view = _parse_activity_view(arg) or TokenActivityView.DAILY
    activity = await asyncio.to_thread(_load_activity)
    if json_mode:
        _print_json(
            {
                "view": view.label,
                "summary": {
                    "lifetime_tokens": activity.summary.lifetime_tokens,
                    "peak_daily_tokens": activity.summary.peak_daily_tokens,
                    "current_streak_days": activity.summary.current_streak_days,
                    "longest_streak_days": activity.summary.longest_streak_days,
                    "longest_task_seconds": activity.summary.longest_task_seconds,
                },
                "daily_values": list(activity.daily_values),
            }
        )
        return
    width = console.size.width
    console.print(_render_activity(activity, view, width=width))
