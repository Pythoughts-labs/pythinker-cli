from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from rich.console import Console
from rich.text import Text

from pythinker_code.ui.shell.usage_activity import (
    ActivitySummary,
    TokenActivity,
    TokenActivityView,
    _bar_levels,
    _chart_start,
    _graded_levels,
    _month_labels,
    _summary_lines,
    _weekly_totals,
    load_activity,
    parse_view,
    render_activity,
)

# ----- parse_view -----


def test_parse_view_defaults_daily_for_empty() -> None:
    assert parse_view("") is TokenActivityView.DAILY
    assert parse_view("daily") is TokenActivityView.DAILY
    assert parse_view(" day ") is TokenActivityView.DAILY
    assert parse_view("weekly") is TokenActivityView.WEEKLY
    assert parse_view("cumulative") is TokenActivityView.CUMULATIVE
    assert parse_view("year") is None
    assert parse_view("monthly") is None


# ----- bucketing helpers -----


def test_chart_start_is_a_sunday_52_weeks_before_today() -> None:
    today = date(2026, 5, 29)  # Friday in the upstream snapshot test
    start = _chart_start(today)
    # ``weekday()`` returns Monday=0; a Sunday is 6.
    assert start.weekday() == 6
    expected_offset = (52 - 1) * 7 + (today.weekday() + 1) % 7
    assert (today - start).days == expected_offset


def test_graded_levels_use_5_step_scale() -> None:
    # Peak is 16; boundaries fall at 1/4 (4), 1/2 (8), and 3/4 (12).
    values = [0, 1, 4, 8, 9, 12, 16]
    levels = _graded_levels(values)
    assert levels == [0, 1, 1, 2, 3, 3, 4]


def test_graded_levels_zero_when_all_zero() -> None:
    assert _graded_levels([0, 0, 0]) == [0, 0, 0]


def test_bar_levels_fill_from_bottom() -> None:
    # 1:0 split → second column fills every row, first column is empty.
    levels = _bar_levels([0, 10])
    assert levels[:7] == [0] * 7
    assert levels[7:] == [4] * 7


def test_weekly_totals_chunk_by_seven() -> None:
    values = list(range(1, 15))  # 14 values, exactly two weeks
    assert _weekly_totals(values) == [sum(range(1, 8)), sum(range(8, 15))]


# ----- summary formatting -----


def test_summary_lines_pack_into_wide_terminal() -> None:
    summary = ActivitySummary(
        lifetime_tokens=21_400_000_000,
        peak_daily_tokens=835_000_000,
        current_streak_days=54,
        longest_streak_days=54,
        longest_task_seconds=13_920,
    )
    lines = _summary_lines(summary, width=120)
    text = "".join(_plain_text(line) for line in lines)
    assert "Lifetime 21.4B" in text
    assert "Peak 835M" in text
    assert "Streak 54d" in text
    assert "Longest task 3h 52m" in text


def test_summary_lines_split_when_too_narrow() -> None:
    summary = ActivitySummary(
        lifetime_tokens=21_400_000_000,
        peak_daily_tokens=835_000_000,
        current_streak_days=54,
        longest_streak_days=54,
        longest_task_seconds=13_920,
    )
    lines = _summary_lines(summary, width=44)
    joined = "\n".join(_plain_text(line) for line in lines)
    # The "Longest task" field should drop to the second line.
    assert "Streak 54d" in joined
    assert joined.count("\n") >= 1


def test_summary_streak_uses_best_format() -> None:
    summary = ActivitySummary(
        lifetime_tokens=0,
        peak_daily_tokens=0,
        current_streak_days=12,
        longest_streak_days=54,
        longest_task_seconds=0,
    )
    text = "".join(_plain_text(line) for line in _summary_lines(summary, width=120))
    assert "12d (best 54d)" in text


# ----- rendering -----


def test_month_labels_show_unique_abbrevs() -> None:
    today = date(2026, 5, 29)
    # 26 weeks ≈ 6 months ending in late May. The label rule prints a
    # month label only when the first day of the column falls on day 1-7,
    # so the rendered row should label the months the chart covers
    # without labelling May (the current month) or anything past it.
    line = _month_labels(today, first_column=0, shown_columns=26)
    text = _plain_text(line)
    for month in ("Jul", "Aug", "Sep", "Oct", "Nov"):
        assert month in text, f"missing {month} in {text!r}"
    assert "May" not in text, "May is the current month and should not yet be labelled"


def test_render_activity_includes_title_summary_and_footer() -> None:
    activity = TokenActivity(
        summary=ActivitySummary(
            lifetime_tokens=120_000_000,
            peak_daily_tokens=12_000_000,
            current_streak_days=3,
            longest_streak_days=10,
            longest_task_seconds=0,
        ),
        daily_values=tuple(1 if idx % 5 == 0 else 0 for idx in range(7 * 52)),
        today_index=7 * 52 - 1,
    )
    text = _render(render_activity(activity, TokenActivityView.DAILY, width=120))
    assert "Token activity" in text
    assert "last 12 months" in text
    assert "Lifetime" in text
    assert "Less" in text and "More" in text
    assert "daily" in text and "weekly" in text and "cumulative" in text


def test_render_activity_wide_left_aligns_chart() -> None:
    # Non-zero data is required for the heatmap to render; an empty history
    # short-circuits to the "No token activity" placeholder.
    activity = TokenActivity(
        summary=ActivitySummary(1, 1, 0, 0, 0),
        daily_values=(0,) * (7 * 52 - 1) + (1,),
        today_index=7 * 52 - 1,
    )
    text = _render(render_activity(activity, TokenActivityView.DAILY, width=160))
    lines = text.splitlines()
    chart_rows = [line for line in lines if line.startswith((" Su ", " Mo ", " Tu "))]
    assert chart_rows, "expected to find weekday rows in the wide render"
    # Wide render: 52 columns × 2 cells - 1 = 103 cells, plus a 4-char gutter.
    # The first weekday row should be at least 100 chars wide.
    assert max(len(line) for line in chart_rows) >= 100


def test_render_activity_weekly_uses_bar_chart() -> None:
    activity = TokenActivity(
        summary=ActivitySummary(
            lifetime_tokens=18,
            peak_daily_tokens=9,
            current_streak_days=0,
            longest_streak_days=0,
            longest_task_seconds=0,
        ),
        daily_values=_sample_weekly_buckets(),
        today_index=7 * 52 - 1,
    )
    text = _render(render_activity(activity, TokenActivityView.WEEKLY, width=22))
    # In the bar view, the gutter shows "max" / "0" instead of weekday labels.
    assert "max" in text
    assert "Each column = 1 week" in text


def test_render_activity_cumulative_caption() -> None:
    activity = TokenActivity(
        summary=ActivitySummary(
            lifetime_tokens=18,
            peak_daily_tokens=9,
            current_streak_days=0,
            longest_streak_days=0,
            longest_task_seconds=0,
        ),
        daily_values=_sample_weekly_buckets(),
        today_index=7 * 52 - 1,
    )
    text = _render(render_activity(activity, TokenActivityView.CUMULATIVE, width=22))
    assert "Running total" in text


def test_render_activity_narrow_widens_terminal_hint() -> None:
    activity = TokenActivity(
        summary=ActivitySummary(0, 0, 0, 0, 0),
        daily_values=(1,) * (7 * 52),
        today_index=7 * 52 - 1,
    )
    text = _render(render_activity(activity, TokenActivityView.DAILY, width=2))
    assert "Widen terminal" in text


def test_render_activity_empty_history_shows_placeholder() -> None:
    activity = TokenActivity(
        summary=ActivitySummary(0, 0, 0, 0, 0),
        daily_values=(0,) * (7 * 52),
        today_index=7 * 52 - 1,
    )
    text = _render(render_activity(activity, TokenActivityView.DAILY, width=80))
    assert "No token activity in the last 12 months" in text


# ----- integration with local wire files -----


def test_load_activity_uses_local_wire_files(monkeypatch: pytest.MonkeyPatch) -> None:
    today = datetime(2026, 5, 29, tzinfo=UTC).date()
    timestamps = [
        datetime(2026, 5, 22, 12, 0, tzinfo=UTC).timestamp() + offset * 86_400
        for offset in range(7)
    ]
    steps = _make_steps(timestamps=timestamps, tokens=[10] * 7)
    # Patch the lowest-level collector so we don't need to fabricate wire
    # files on disk. ``_collect_steps`` is the only function that walks the
    # session tree; bypassing it isolates the bucketing logic the test
    # actually cares about.
    monkeypatch.setattr(
        "pythinker_code.ui.shell.usage_activity._collect_steps",
        lambda: steps,
    )
    activity = load_activity(today=today)
    assert activity.summary.lifetime_tokens == 70
    assert activity.summary.peak_daily_tokens == 10
    assert activity.summary.longest_streak_days == 7


# ----- helpers -----


def _sample_weekly_buckets() -> tuple[int, ...]:
    """Three weeks of values whose per-week totals are 3, 6, 9."""

    pattern = [3] + [0] * 6 + [6] + [0] * 6 + [9] + [0] * 6
    return tuple(pattern + [0] * (7 * 52 - len(pattern)))


def _make_steps(*, timestamps: list[float], tokens: list[int]) -> list:
    from pythinker_code.ui.shell.stats_collector import StepRecord

    return [
        StepRecord(
            session_id="s",
            timestamp=ts,
            model_name="m",
            provider_key="managed:test",
            input_other=tok,
            output=0,
            input_cache_read=0,
            input_cache_creation=0,
        )
        for ts, tok in zip(timestamps, tokens, strict=True)
    ]


def _plain_text(line) -> str:
    if isinstance(line, Text):
        return line.plain
    return str(line)


def _render(renderable) -> str:
    console = Console(force_terminal=False, width=160, record=True)
    console.print(renderable)
    return console.export_text()
