"""Renders the account token activity card for ``/usage``.

The card is a 52-week × 7-day GitHub-style heatmap of total tokens consumed
each day, a one-line summary of headline numbers, and a footer that lets the
user switch between daily/weekly/cumulative views. Data comes from the local
session wire files (no remote usage API is required), so the card is always
representative of the same on-disk activity that the existing cost panel
reads. Bucketing, level grading, and Rich rendering are isolated here so the
dispatcher in :mod:`pythinker_code.ui.shell.usage` stays slim.
"""

from __future__ import annotations

import enum
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from rich.console import Group, RenderableType
from rich.style import Style
from rich.text import Text

from pythinker_code.ui.shell.stats_collector import (
    StepRecord,
    collect_session_files,
    get_sessions_root,
    parse_wire_file,
)
from pythinker_code.ui.theme import tui_rich_style
from pythinker_code.utils.datetime import format_duration

WEEK_COUNT = 52
DAY_COUNT = 7
CELL_COUNT = WEEK_COUNT * DAY_COUNT
CHART_LEFT_WIDTH = 4
LEGEND_LEFT_PAD = "   "

# Glyph policy: a filled square for the
# GitHub-style daily view, a full block for bar views, and a hollow square
# reserved for low-color fallbacks if we ever need to detect one.
ACTIVE_GLYPH = "■"
EMPTY_GLYPH = "□"
BAR_GLYPH = "█"
WEEKDAY_LABELS: tuple[str, ...] = ("Su", "Mo", "Tu", "We", "Th", "Fr", "Sa")


class TokenActivityView(enum.Enum):
    """Aggregation for one ``/usage`` card render."""

    DAILY = "daily"
    WEEKLY = "weekly"
    CUMULATIVE = "cumulative"

    @property
    def label(self) -> str:
        return self.value


def parse_view(value: str) -> TokenActivityView | None:
    """Map a free-form argument to a supported view, or ``None`` if unsupported.

    Empty input defaults to the daily view so ``/usage`` and ``/usage daily``
    behave identically. Returning ``None`` for unknown values lets the caller
    surface a clear error instead of silently picking a view.
    """

    key = value.strip().lower()
    if key in {"", "day", "daily"}:
        return TokenActivityView.DAILY
    if key in {"week", "weekly"}:
        return TokenActivityView.WEEKLY
    if key == "cumulative":
        return TokenActivityView.CUMULATIVE
    return None


@dataclass(slots=True, frozen=True)
class ActivitySummary:
    """Headline numbers for the one-line summary above the chart."""

    lifetime_tokens: int
    peak_daily_tokens: int
    current_streak_days: int
    longest_streak_days: int
    longest_task_seconds: int


@dataclass(slots=True, frozen=True)
class TokenActivity:
    """Renderable payload for a single ``/usage`` card."""

    summary: ActivitySummary
    daily_values: tuple[int, ...]  # length == CELL_COUNT
    today_index: int  # position in daily_values that maps to ``today``


def load_activity(today: date | None = None) -> TokenActivity:
    """Load and aggregate local session usage for the activity card.

    Returns an empty activity (zeroed summary, all-zero cells) when no
    sessions exist, so the card still renders with a "No token activity"
    hint rather than raising.
    """

    today = today or datetime.now(tz=UTC).date()
    steps = _collect_steps()
    return _build_activity(steps, today)


def _collect_steps() -> list[StepRecord]:
    seen: set[str] = set()
    out: list[StepRecord] = []
    root = get_sessions_root()
    for wire_path in collect_session_files(root):
        # Mirror the session-id resolution in ``stats_collector.load_all_stats``:
        # nested subagent files share their parent session's identity so the
        # activity card double-counts subagent work the same way the cost panel
        # already does.
        if wire_path.parent.parent.name == "subagents":
            session_id = f"{wire_path.parents[3].name}/{wire_path.parents[2].name}"
        else:
            session_id = f"{wire_path.parents[1].name}/{wire_path.parents[0].name}"
        out.extend(parse_wire_file(wire_path, session_id, seen))
    return out


def _build_activity(steps: Iterable[StepRecord], today: date) -> TokenActivity:
    start = _chart_start(today)
    end = start + timedelta(days=CELL_COUNT)
    counts: Counter[date] = Counter()
    for step in steps:
        if not step.timestamp:
            continue
        # timestamps come from the wire; treat them as UTC instants.
        try:
            ts = datetime.fromtimestamp(step.timestamp, tz=UTC).date()
        except (OverflowError, OSError, ValueError):
            continue
        if ts < start or ts >= end or ts > today:
            continue
        counts[ts] += max(step.total_tokens, 0)

    values = tuple(counts.get(start + timedelta(days=offset), 0) for offset in range(CELL_COUNT))
    today_offset = (today - start).days
    if 0 <= today_offset < CELL_COUNT:
        # ``today`` always lands on the rightmost week; clamp any drift so the
        # "future cells" branch in the renderer stays consistent.
        today_index = min(today_offset, CELL_COUNT - 1)
    else:
        today_index = CELL_COUNT - 1

    return TokenActivity(
        summary=_summarize(values, today),
        daily_values=values,
        today_index=today_index,
    )


def _summarize(values: Sequence[int], today: date) -> ActivitySummary:
    start = _chart_start(today)
    lifetime = sum(values)
    peak = max(values, default=0)
    current_streak = _current_streak(values, today_offset=(today - start).days)
    longest_streak = _longest_streak(values)
    return ActivitySummary(
        lifetime_tokens=lifetime,
        peak_daily_tokens=peak,
        current_streak_days=current_streak,
        longest_streak_days=longest_streak,
        longest_task_seconds=0,
    )


def _current_streak(values: Sequence[int], today_offset: int) -> int:
    """Count consecutive non-zero days ending at ``today``.

    The reference renderer treats the current streak as the number of days,
    ending today, that have any activity. We do the same without treating
    today as a "miss" when its bucket is empty: the user is most often on
    this card mid-day and we don't want a partial day to look like the
    streak ended.
    """

    streak = 0
    end = min(today_offset, len(values) - 1)
    for offset in range(end, -1, -1):
        if values[offset] <= 0:
            break
        streak += 1
    return streak


def _longest_streak(values: Sequence[int]) -> int:
    best = 0
    current = 0
    for value in values:
        if value > 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _chart_start(today: date) -> date:
    """First cell of the 52-week window (a Sunday)."""

    week_start = today - timedelta(days=(today.weekday() + 1) % DAY_COUNT)
    return week_start - timedelta(weeks=WEEK_COUNT - 1)


def _graded_levels(values: Sequence[int]) -> list[int]:
    """Assign each daily bucket an intensity level in ``0..4``.

    Mirrors the upstream 5-step scale: the peak day hits level 4, zero days
    sit at level 0, and the boundaries land at 1/4, 1/2, and 3/4 of the
    peak. Doing it this way keeps the heatmap readable when the user has
    a single busy day and a long tail of quieter ones.
    """

    peak = max(values, default=0)
    if peak <= 0:
        return [0] * len(values)
    out: list[int] = []
    for value in values:
        if value <= 0:
            out.append(0)
        elif value * 4 > peak * 3:
            out.append(4)
        elif value * 2 > peak:
            out.append(3)
        elif value * 4 > peak:
            out.append(2)
        else:
            out.append(1)
    return out


def _weekly_totals(values: Sequence[int]) -> list[int]:
    return [sum(values[row : row + DAY_COUNT]) for row in range(0, len(values), DAY_COUNT)]


def _bar_levels(weekly: Sequence[int]) -> list[int]:
    """Height (0..DAY_COUNT) of each column in the weekly/cumulative views.

    Stored as a flat list of length ``CELL_COUNT`` so the renderer can
    index by ``column * DAY_COUNT + row`` exactly like the daily view. A
    positive column fills from the bottom up, leaving the empty rows at
    the top; the gutter shows ``max``/``0`` to read the column as a
    mini bar chart.
    """

    peak = max(weekly, default=0)
    out: list[int] = []
    for total in weekly:
        # Round-up integer division so a column at 1/7 of the peak still
        # renders a single visible block.
        height = 0 if peak <= 0 or total <= 0 else (total * DAY_COUNT + peak - 1) // peak
        height = min(height, DAY_COUNT)
        for row in range(DAY_COUNT):
            out.append(4 if DAY_COUNT - row <= height else 0)
    return out


def _shown_columns(width: int) -> int:
    """How many of the 52 weekly columns the terminal can fit."""

    if width <= 0:
        return 0
    usable = max(width - CHART_LEFT_WIDTH, 0) + 1
    return min(usable // 2, WEEK_COUNT)


def _format_compact(value: int) -> str:
    """Compact integer formatter matching the upstream ``format_tokens_compact``.

    Keeps a leading sign of magnitude so ``260_000_000`` prints as ``260M``
    and ``21_400_000_000`` prints as ``21.4B``. The exact suffixes match
    what users see in the upstream ``/usage`` card.
    """

    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        scaled = value / 1_000_000_000
        return f"{scaled:.1f}B".replace(".0B", "B")
    if abs_value >= 1_000_000:
        scaled = value / 1_000_000
        return f"{scaled:.1f}M".replace(".0M", "M")
    if abs_value >= 1_000:
        scaled = value / 1_000
        return f"{scaled:.1f}K".replace(".0K", "K")
    return str(value)


def _format_optional_tokens(value: int) -> str:
    return _format_compact(value) if value > 0 else "-"


def _format_streak(current: int, longest: int) -> str:
    if current <= 0 and longest <= 0:
        return "-"
    if longest <= 0 or current == longest:
        return f"{current}d"
    return f"{current}d (best {longest}d)"


def _format_optional_duration(value: int) -> str:
    if value <= 0:
        return "-"
    return format_duration(value)


def render_activity(
    activity: TokenActivity,
    view: TokenActivityView,
    width: int = 80,
) -> RenderableType:
    """Build the Rich renderable for a ``/usage`` card."""

    width = max(width, 0)
    lines: list[RenderableType] = []

    title = Text()
    title.append(" Token activity", style="bold")
    title.append("   last 12 months", style=tui_rich_style("muted"))
    lines.append(title)

    lines.extend(_summary_lines(activity.summary, width))

    if not any(activity.daily_values):
        lines.append(Text(" "))
        lines.append(
            Text(
                "   No token activity in the last 12 months",
                style=tui_rich_style("muted"),
            )
        )
        return Group(*lines)

    lines.append(Text(" "))
    lines.extend(_chart_lines(activity, view, width))
    return Group(*lines)


def _summary_lines(summary: ActivitySummary, width: int) -> list[RenderableType]:
    """Greedy-packing summary into as many lines as the terminal allows."""

    fields: list[tuple[str, str]] = [
        ("Lifetime", _format_optional_tokens(summary.lifetime_tokens)),
        ("Peak", _format_optional_tokens(summary.peak_daily_tokens)),
        ("Streak", _format_streak(summary.current_streak_days, summary.longest_streak_days)),
        ("Longest task", _format_optional_duration(summary.longest_task_seconds)),
    ]
    if width <= 0:
        return [Text(_join_fields(fields))]
    max_width = max(width - 1, 1)
    groups: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for field in fields:
        candidate = current + [field]
        if current and len(_join_fields(candidate)) > max_width:
            groups.append(current)
            current = [field]
        else:
            current = candidate
    if current:
        groups.append(current)
    return [Text(" " + _join_fields(group)) for group in groups]


def _join_fields(fields: Sequence[tuple[str, str]]) -> str:
    parts: list[str] = []
    for label, value in fields:
        parts.append(f"{label} {value}")
    return " · ".join(parts)


def _month_labels(today: date, first_column: int, shown_columns: int) -> Text:
    cells = [" "] * (shown_columns * 2 - 1)
    last_end = 0
    absolute_start = _chart_start(today)
    for column in range(first_column, WEEK_COUNT):
        cell_date = absolute_start + timedelta(days=column * DAY_COUNT)
        if cell_date.day > 7:
            continue
        label = cell_date.strftime("%b")
        offset = (column - first_column) * 2
        if offset < last_end or offset + len(label) > len(cells):
            continue
        for index, ch in enumerate(label):
            cells[offset + index] = ch
        last_end = offset + len(label) + 1
    line = Text(" " * CHART_LEFT_WIDTH, style=tui_rich_style("muted"))
    line.append("".join(cells), style=tui_rich_style("muted"))
    return line


def _chart_lines(
    activity: TokenActivity, view: TokenActivityView, width: int
) -> list[RenderableType]:
    shown = _shown_columns(width)
    if shown == 0:
        return [
            Text(
                "   Widen terminal to show activity graph",
                style=tui_rich_style("muted"),
            )
        ]
    first_column = WEEK_COUNT - shown
    today = datetime.now(tz=UTC).date()
    out: list[RenderableType] = [_month_labels(today, first_column, shown)]

    if view is TokenActivityView.DAILY:
        levels = _graded_levels(activity.daily_values)
    elif view is TokenActivityView.WEEKLY:
        levels = _bar_levels(_weekly_totals(activity.daily_values))
    else:  # Cumulative
        totals = _weekly_totals(activity.daily_values)
        running: list[int] = []
        accumulator = 0
        for total in totals:
            accumulator += total
            running.append(accumulator)
        levels = _bar_levels(running)

    empty_style = tui_rich_style("muted")
    active_style = tui_rich_style("success")
    future_style = tui_rich_style("muted")

    chart_start = _chart_start(today)

    for row in range(DAY_COUNT):
        gutter = Text(_gutter_label(view, row), style=tui_rich_style("muted"))
        line = Text()
        line.append_text(gutter)
        for column in range(first_column, WEEK_COUNT):
            if column > first_column:
                line.append(" ")
            index = column * DAY_COUNT + row
            level = levels[index]
            cell_date = chart_start + timedelta(days=index)
            if view is TokenActivityView.DAILY and cell_date > today:
                # Upcoming cells stay blank so the heatmap doesn't pretend to
                # show data we don't have.
                line.append(" ", style=future_style)
                continue
            glyph, style = _glyph_and_style(view, level, empty_style, active_style)
            line.append(glyph, style=style)
        out.append(line)

    out.append(Text(" "))
    if view is TokenActivityView.DAILY:
        out.append(_legend_line(empty_style, active_style))
    else:
        out.append(_bar_caption(view, activity, empty_style, active_style))
    out.append(_view_footer(view))
    return out


def _gutter_label(view: TokenActivityView, row: int) -> str:
    if view is TokenActivityView.DAILY:
        return f" {WEEKDAY_LABELS[row]} "
    if row == 0:
        return "max "
    if row == DAY_COUNT - 1:
        return "  0 "
    return "    "


def _glyph_and_style(
    view: TokenActivityView,
    level: int,
    empty_style: Style,
    active_style: Style,
) -> tuple[str, Style]:
    if view is not TokenActivityView.DAILY:
        glyph = BAR_GLYPH if level > 0 else " "
        return glyph, active_style if level > 0 else empty_style
    if level <= 0:
        return EMPTY_GLYPH, empty_style
    return ACTIVE_GLYPH, active_style


def _legend_line(empty_style: Style, active_style: Style) -> Text:
    line = Text(LEGEND_LEFT_PAD + "Less ", style=tui_rich_style("muted"))
    for level in range(5):
        if level > 0:
            line.append(" ")
        glyph, style = _glyph_and_style(TokenActivityView.DAILY, level, empty_style, active_style)
        line.append(glyph, style=style)
    line.append(" More", style=tui_rich_style("muted"))
    return line


def _bar_caption(
    view: TokenActivityView,
    activity: TokenActivity,
    empty_style: Style,
    active_style: Style,
) -> Text:
    del empty_style, active_style  # caption only re-uses the muted + bold styles
    weekly = _weekly_totals(activity.daily_values)
    if view is TokenActivityView.WEEKLY:
        peak = max(weekly, default=0)
        lead = "Each column = 1 week · tallest "
    else:
        peak = sum(weekly)
        lead = "Running total · top "
    line = Text(LEGEND_LEFT_PAD, style=tui_rich_style("muted"))
    if peak <= 0:
        line.append("No token activity in the last 12 months", style=tui_rich_style("muted"))
        return line
    line.append(lead, style=tui_rich_style("muted"))
    line.append(_format_compact(peak), style="bold")
    return line


def _view_footer(active: TokenActivityView) -> Text:
    line = Text(LEGEND_LEFT_PAD, style=tui_rich_style("muted"))
    views = [
        (TokenActivityView.DAILY, "daily"),
        (TokenActivityView.WEEKLY, "weekly"),
        (TokenActivityView.CUMULATIVE, "cumulative"),
    ]
    for index, (view, name) in enumerate(views):
        if index > 0:
            line.append(" · ", style=tui_rich_style("muted"))
        style = "bold" if view is active else tui_rich_style("muted")
        line.append(name, style=style)
    return line


__all__ = [
    "ActivitySummary",
    "TokenActivity",
    "TokenActivityView",
    "load_activity",
    "parse_view",
    "render_activity",
]
