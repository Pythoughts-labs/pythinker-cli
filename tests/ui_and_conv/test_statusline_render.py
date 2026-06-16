"""Tests for statusline v2 rendering: theme tokens, bar, segments."""

from dataclasses import replace

from pythinker_code.config import StatusLineConfig
from pythinker_code.ui.shell.statusline import (
    SEGMENT_REGISTRY,
    GitInfo,
    StatusFlags,
    StatusLineContext,
    smooth_bar,
    split_zones,
    usage_level,
)
from pythinker_code.ui.theme import StatusLineColors, get_statusline_colors


def test_statusline_colors_dark_palette():
    colors = get_statusline_colors()
    assert isinstance(colors, StatusLineColors)
    assert colors.model == "bold fg:#dcb4ff"
    assert colors.usage_ok == "fg:#505564"
    assert colors.usage_crit == "fg:#ff5050"
    assert colors.dim == "fg:#505564"


def test_usage_level_thresholds():
    assert usage_level(0) == "ok"
    assert usage_level(49) == "ok"
    assert usage_level(50) == "mid"
    assert usage_level(69) == "mid"
    assert usage_level(70) == "high"
    assert usage_level(89) == "high"
    assert usage_level(90) == "crit"
    assert usage_level(200) == "crit"


def test_smooth_bar_filled_cells():
    assert smooth_bar(0, width=8) == "▱" * 8
    assert smooth_bar(100, width=8) == "▰" * 8
    # 18% of 10 cells rounds to 2 filled cells.
    assert smooth_bar(18, width=10) == "▰▰" + "▱" * 8
    # never exceeds width
    assert len(smooth_bar(99, width=10)) == 10
    assert smooth_bar(99, width=10) == "▰" * 10


def test_smooth_bar_ascii_fallback():
    assert smooth_bar(50, width=8, ascii_only=True) == "####----"
    assert smooth_bar(0, width=8, ascii_only=True) == "--------"


def make_ctx(**overrides):
    """A minimal idle context; tests override what they exercise."""
    base = StatusLineContext(
        columns=120,
        working=False,
        frame=0,
        model_name="claude-fable-5",
        provider_label=None,
        effort=None,
        rate_in=None,
        rate_out=None,
        session_cost_usd=0.0,
        cost_budget_usd=None,
        context_tokens=36_000,
        max_context_tokens=200_000,
        elapsed_s=72.0,
        clock="14:32",
        cwd="pythinker-code-main",
        git=None,
        diff_added=None,
        diff_removed=None,
        flags=StatusFlags(yolo=False, auto=False, plan=False),
        limits=None,
        ascii_only=False,
        style="fancy",
        bar_width=10,
    )
    return replace(base, **overrides)


def test_registry_covers_all_config_ids():
    from pythinker_code.config import STATUSLINE_SEGMENT_IDS

    assert set(SEGMENT_REGISTRY) == set(STATUSLINE_SEGMENT_IDS)


def test_split_zones_preserves_user_order():
    cfg = StatusLineConfig(segments=["clock", "model", "spinner", "context"])
    zones = split_zones(cfg.segments)
    assert zones.line1 == ["model", "spinner"]
    assert zones.line2_right == ["clock", "context"]


def test_split_zones_ignores_unknown_ids():
    zones = split_zones(["model", "hologram"])
    assert zones.line1 == ["model"]


def _text(fragments):
    return "".join(t for _, t in fragments)


def test_spinner_working_vs_idle():
    working = SEGMENT_REGISTRY["spinner"].render(make_ctx(working=True, frame=3))
    idle = SEGMENT_REGISTRY["spinner"].render(make_ctx(working=False))
    assert _text(working) == "⠸"  # frame 3 of the braille cycle
    assert _text(idle) == "◇"


def test_spinner_ascii_fallback():
    frags = SEGMENT_REGISTRY["spinner"].render(make_ctx(working=True, frame=0, ascii_only=True))
    assert _text(frags) in {"|", "/", "-", "\\"}


def test_model_with_and_without_provider():
    assert _text(SEGMENT_REGISTRY["model"].render(make_ctx())) == "claude-fable-5"
    frags = SEGMENT_REGISTRY["model"].render(make_ctx(provider_label="anthropic"))
    assert _text(frags) == "claude-fable-5 @anthropic"
    assert SEGMENT_REGISTRY["model"].render(make_ctx(model_name=None)) is None


def test_cost_hidden_at_zero_shown_with_budget():
    assert SEGMENT_REGISTRY["cost"].render(make_ctx(session_cost_usd=0.0)) is None
    assert _text(SEGMENT_REGISTRY["cost"].render(make_ctx(session_cost_usd=1.844))) == "$1.84"
    frags = SEGMENT_REGISTRY["cost"].render(make_ctx(session_cost_usd=10.2, cost_budget_usd=50.0))
    assert _text(frags) == "$10.20/$50"


def test_speed_requires_working_and_a_rate():
    assert SEGMENT_REGISTRY["speed"].render(make_ctx(working=False, rate_out=80)) is None
    assert SEGMENT_REGISTRY["speed"].render(make_ctx(working=True)) is None
    frags = SEGMENT_REGISTRY["speed"].render(make_ctx(working=True, rate_in=92, rate_out=85))
    assert _text(frags) == "in 92 out 85 t/s"
    frags = SEGMENT_REGISTRY["speed"].render(make_ctx(working=True, rate_out=85))
    assert _text(frags) == "out 85 t/s"


def test_effort_badge_levels_and_hidden():
    assert SEGMENT_REGISTRY["effort"].render(make_ctx(effort=None)) is None
    assert _text(SEGMENT_REGISTRY["effort"].render(make_ctx(effort="high"))) == "▲ high"
    assert _text(SEGMENT_REGISTRY["effort"].render(make_ctx(effort="medium"))) == "◆ med"
    assert _text(SEGMENT_REGISTRY["effort"].render(make_ctx(effort="low"))) == "▽ low"


def test_cwd_and_git_segments():
    assert _text(SEGMENT_REGISTRY["cwd"].render(make_ctx())) == "pythinker-code-main"
    assert SEGMENT_REGISTRY["cwd"].render(make_ctx(cwd=None)) is None
    git = GitInfo(branch="feat/x", dirty=True, ahead=2, behind=0)
    text = _text(SEGMENT_REGISTRY["git"].render(make_ctx(git=git)))
    assert "feat/x" in text
    assert SEGMENT_REGISTRY["git"].render(make_ctx(git=None)) is None


def test_diff_segment():
    assert SEGMENT_REGISTRY["diff"].render(make_ctx()) is None
    assert SEGMENT_REGISTRY["diff"].render(make_ctx(diff_added=0, diff_removed=0)) is None
    frags = SEGMENT_REGISTRY["diff"].render(make_ctx(diff_added=54, diff_removed=13))
    assert frags is not None
    assert _text(frags) == "+54/-13"
    styles = [s for s, _ in frags]
    assert any("78dc8c" in s for s in styles)  # additions mint
    assert any("ff6e6e" in s for s in styles)  # deletions red


def test_flags_segment():
    assert SEGMENT_REGISTRY["flags"].render(make_ctx()) is None
    frags = SEGMENT_REGISTRY["flags"].render(
        make_ctx(flags=StatusFlags(yolo=True, auto=False, plan=True))
    )
    assert _text(frags) == "yolo plan"


def test_context_segment_bar_and_gradient():
    frags = SEGMENT_REGISTRY["context"].render(make_ctx())
    text = _text(frags)
    assert text.startswith("ctx 36k/200k ")
    assert "18%" in text
    assert "▰" in text and "▱" in text
    assert SEGMENT_REGISTRY["context"].render(make_ctx(max_context_tokens=0)) is None


def test_context_low_warning_blinks_on_frame():
    ctx_hot = make_ctx(context_tokens=190_000, frame=0)
    assert "CTX LOW" in _text(SEGMENT_REGISTRY["context"].render(ctx_hot))
    s0 = SEGMENT_REGISTRY["context"].render(make_ctx(context_tokens=190_000, frame=0))
    s1 = SEGMENT_REGISTRY["context"].render(make_ctx(context_tokens=190_000, frame=1))
    assert s0 is not None and s1 is not None
    assert [s for s, _ in s0] != [s for s, _ in s1]  # bold/dim alternation


def test_elapsed_and_clock():
    assert _text(SEGMENT_REGISTRY["elapsed"].render(make_ctx(elapsed_s=4325))) == "1h 12m elapsed"
    assert _text(SEGMENT_REGISTRY["clock"].render(make_ctx())) == "14:32"


def test_limits_hidden_without_data():
    assert SEGMENT_REGISTRY["limits"].render(make_ctx(limits=None)) is None
    from pythinker_code.ui.shell.statusline import ProviderLimits

    lim = ProviderLimits(
        requests_pct=37, requests_reset_s=9960.0, tokens_pct=None, tokens_reset_s=None
    )
    text = _text(SEGMENT_REGISTRY["limits"].render(make_ctx(limits=lim)))
    assert "37%" in text and "2h 46m" in text


def test_assemble_footer_two_lines_and_separators():
    from pythinker_code.ui.shell.statusline import assemble_footer

    cfg = StatusLineConfig()
    lines = assemble_footer(make_ctx(), cfg.segments)
    assert len(lines) == 2
    line1 = _text(lines[0])
    assert "claude-fable-5" in line1 and "pythinker-code-main" in line1
    assert "│" in line1 or "·" in line1
    line2 = _text(lines[1])
    assert "ctx" in line2 and "14:32" in line2


def test_assemble_footer_drops_redundant_tokens_when_context_present():
    from pythinker_code.ui.shell.statusline import assemble_footer

    # "context" already prints the used/total ratio; a standalone "tokens"
    # segment next to it is pure duplication and must be dropped.
    segments = ["cwd", "context", "tokens", "model"]
    line2 = _text(assemble_footer(make_ctx(), segments)[1])
    assert "ctx 36k/200k" in line2
    # The ratio appears exactly once (no "… │ 36k/200k" repeat).
    assert line2.count("36k/200k") == 1

    # With only "tokens" (no "context"), the ratio still renders.
    only_tokens = _text(assemble_footer(make_ctx(), ["tokens"])[1])
    assert only_tokens.strip() == "36k/200k"


def test_assemble_footer_drops_segments_under_width_pressure():
    from pythinker_code.ui.shell.statusline import assemble_footer

    cfg = StatusLineConfig()
    wide = make_ctx(
        working=True,
        rate_in=92,
        rate_out=85,
        session_cost_usd=1.5,
        effort="high",
        diff_added=54,
        diff_removed=13,
    )
    narrow = make_ctx(
        columns=60,
        working=True,
        rate_in=92,
        rate_out=85,
        session_cost_usd=1.5,
        effort="high",
        diff_added=54,
        diff_removed=13,
    )
    assert "in 92" in _text(assemble_footer(wide, cfg.segments)[0])
    line1_narrow = _text(assemble_footer(narrow, cfg.segments)[0])
    assert "in 92" not in line1_narrow  # speed dropped first
    assert "claude-fable-5" in line1_narrow  # model survives


def test_segment_exception_is_isolated(monkeypatch):
    from pythinker_code.ui.shell import statusline as sl

    def _boom(ctx):
        raise ZeroDivisionError

    boom = sl.SegmentSpec("cost", "line1", _boom, drop_priority=5)
    monkeypatch.setitem(sl.SEGMENT_REGISTRY, "cost", boom)
    cfg = StatusLineConfig()
    lines = sl.assemble_footer(make_ctx(session_cost_usd=5.0), cfg.segments)
    assert "claude-fable-5" in _text(lines[0])  # render survived


def test_rate_sampler_window_and_rate():
    from pythinker_code.ui.shell.statusline import RateSampler

    s = RateSampler(window_s=1.5, min_samples=3)
    assert s.update(0.0, 0) is None  # 1 sample
    assert s.update(0.5, 100) is None  # 2 samples
    rate = s.update(1.0, 200)  # 3 samples: 200 tokens over 1.0s
    assert rate == 200
    # stale samples evicted beyond the window: only the new sample survives,
    # which is below min_samples, so no rate is reported
    assert s.update(3.0, 260) is None


def test_rate_sampler_needs_min_samples():
    from pythinker_code.ui.shell.statusline import RateSampler

    s = RateSampler(window_s=1.5, min_samples=3)
    assert s.update(0.0, 0) is None
    assert s.update(0.1, 10) is None


def test_rate_sampler_non_increasing_returns_none():
    from pythinker_code.ui.shell.statusline import RateSampler

    s = RateSampler(window_s=10.0, min_samples=2)
    s.update(0.0, 100)
    # total did not grow → delta 0 → None
    assert s.update(1.0, 100) is None


def test_rate_sampler_reset():
    from pythinker_code.ui.shell.statusline import RateSampler

    s = RateSampler(window_s=10.0, min_samples=2)
    s.update(0.0, 0)
    s.update(1.0, 50)
    s.reset()
    assert s.update(2.0, 60) is None  # back to 1 sample after reset


def test_parse_shortstat():
    from pythinker_code.ui.shell.statusline import parse_shortstat

    assert parse_shortstat(" 3 files changed, 54 insertions(+), 13 deletions(-)") == (54, 13)
    assert parse_shortstat(" 1 file changed, 7 insertions(+)") == (7, 0)
    assert parse_shortstat(" 2 files changed, 5 deletions(-)") == (0, 5)
    assert parse_shortstat("") == (0, 0)
