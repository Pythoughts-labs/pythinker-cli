"""Status line segment resolution and the external status command runner.

Kept separate from ``prompt.py`` so the footer customization logic stays small,
pure, and independently testable. The render paths in ``prompt.py`` consult
``resolve_segments`` and read ``StatusLineCommandRunner.current_line`` — they
never run subprocesses themselves (the toolbar re-renders on every keystroke).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shlex
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from prompt_toolkit.utils import get_cwidth

from pythinker_code.config import StatusLineConfig
from pythinker_code.ui.shell.components import format_tokens, sanitize_ansi
from pythinker_code.ui.theme import get_statusline_colors, get_toolbar_colors
from pythinker_code.utils.datetime import format_duration
from pythinker_code.utils.logging import logger


class RateSampler:
    """Sliding-window tokens/sec over a monotonically growing counter."""

    def __init__(self, window_s: float = 1.5, min_samples: int = 3) -> None:
        self._window_s = window_s
        self._min_samples = min_samples
        self._samples: deque[tuple[float, int]] = deque()

    def update(self, now: float, total: int) -> int | None:
        self._samples.append((now, total))
        while len(self._samples) > 1 and now - self._samples[0][0] > self._window_s:
            self._samples.popleft()
        if len(self._samples) < self._min_samples:
            return None
        span = self._samples[-1][0] - self._samples[0][0]
        delta = self._samples[-1][1] - self._samples[0][1]
        if span <= 0 or delta <= 0:
            return None
        return int(delta / span)

    def reset(self) -> None:
        self._samples.clear()


_SHORTSTAT_RE = re.compile(r"(?:(\d+) insertion)|(?:(\d+) deletion)")


def parse_shortstat(out: str) -> tuple[int, int]:
    """Parse ``git diff --shortstat`` output into (added, removed)."""
    added = removed = 0
    for m in _SHORTSTAT_RE.finditer(out):
        if m.group(1):
            added = int(m.group(1))
        if m.group(2):
            removed = int(m.group(2))
    return added, removed


def usage_level(pct: int) -> str:
    """Gradient bucket for a 0-100+ percentage: ok | mid | high | crit."""
    if pct >= 90:
        return "crit"
    if pct >= 70:
        return "high"
    if pct >= 50:
        return "mid"
    return "ok"


def smooth_bar(pct: int, *, width: int, ascii_only: bool = False) -> str:
    """Render a progress bar using the compaction view's ``▰``/``▱`` cells.

    ``pct`` is clamped to [0, 100]. The glyphs are East-Asian-width *neutral*
    (always one column), so the bar measures the same as it renders on every
    terminal. ASCII mode degrades to '#'/'-' cells.
    """
    pct = max(0, min(100, pct))
    if ascii_only:
        filled = pct * width // 100
        return "#" * filled + "-" * (width - filled)
    filled = max(0, min(width, round(pct * width / 100)))
    return "▰" * filled + "▱" * (width - filled)


DEFAULT_STATUSLINE_SEGMENTS: tuple[str, ...] = (
    "cwd",
    "git",
    "flags",
    "context",
    "model",
)

_LINE1_SEGMENTS: frozenset[str] = frozenset({"cwd", "git", "flags"})
_LINE2_RIGHT_SEGMENTS: frozenset[str] = frozenset({"context", "tokens", "model"})

_MAX_COMMAND_LINE_CHARS = 200
_MIN_REFRESH_INTERVAL_S = 0.5
# Floor for explicitly-passed intervals: keeps tests fast while preventing a
# zero/negative interval from busy-looping subprocess spawns.
_MIN_EXPLICIT_INTERVAL_S = 0.01
_MAX_COMMAND_OUTPUT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class StatusLineLayout:
    """Which footer segments to render, split by footer zone."""

    line1: list[str] = field(default_factory=list[str])
    line2_right: list[str] = field(default_factory=list[str])
    show_command: bool = False


def resolve_segments(cfg: StatusLineConfig) -> StatusLineLayout:
    """Map a :class:`StatusLineConfig` to the footer zones.

    When customization is disabled the stock layout is returned so render
    paths behave exactly as before. The ``command`` segment only shows when an
    external command is actually configured.
    """
    segments = list(DEFAULT_STATUSLINE_SEGMENTS) if not cfg.enabled else list(cfg.segments)
    show_command = cfg.enabled and "command" in segments and bool(cfg.command)
    return StatusLineLayout(
        line1=[s for s in segments if s in _LINE1_SEGMENTS],
        line2_right=[s for s in segments if s in _LINE2_RIGHT_SEGMENTS],
        show_command=show_command,
    )


StyleFragment = tuple[str, str]  # (prompt_toolkit style string, text)


@dataclass(frozen=True, slots=True)
class GitInfo:
    branch: str
    dirty: bool
    ahead: int
    behind: int


@dataclass(frozen=True, slots=True)
class StatusFlags:
    yolo: bool
    auto: bool
    plan: bool


@dataclass(frozen=True, slots=True)
class ProviderLimits:
    """Pre-digested rate-limit view for the footer (built in prompt.py)."""

    requests_pct: int | None
    requests_reset_s: float | None
    tokens_pct: int | None
    tokens_reset_s: float | None


@dataclass(frozen=True, slots=True)
class StatusLineContext:
    columns: int
    working: bool
    frame: int
    model_name: str | None
    provider_label: str | None
    effort: str | None
    rate_in: int | None
    rate_out: int | None
    session_cost_usd: float
    cost_budget_usd: float | None
    context_tokens: int
    max_context_tokens: int
    elapsed_s: float
    clock: str
    cwd: str | None
    git: GitInfo | None
    diff_added: int | None
    diff_removed: int | None
    flags: StatusFlags
    limits: ProviderLimits | None
    ascii_only: bool
    style: str  # "fancy" | "plain"
    bar_width: int


@dataclass(frozen=True, slots=True)
class SegmentSpec:
    id: str
    zone: str  # "line1" | "line2_right" | "line2_left"
    render: Callable[[StatusLineContext], list[StyleFragment] | None]
    drop_priority: int  # higher = dropped sooner under width pressure


@dataclass(frozen=True, slots=True)
class ZoneSplit:
    line1: list[str]
    line2_right: list[str]
    line2_left: list[str]


def _not_rendered(ctx: StatusLineContext) -> list[StyleFragment] | None:
    """Placeholder renderer; replaced by real renderers in later tasks."""
    return None


# ---------------------------------------------------------------------------
# Line-1 segment renderers
# ---------------------------------------------------------------------------

_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_SPINNER_FRAMES_ASCII = ("|", "/", "-", "\\")


def _style(ctx: StatusLineContext, color: str) -> str:
    return "" if ctx.style == "plain" else color


def _render_spinner(ctx: StatusLineContext) -> list[StyleFragment] | None:
    colors = get_statusline_colors()
    if ctx.working:
        frames = _SPINNER_FRAMES_ASCII if ctx.ascii_only else _SPINNER_FRAMES
        return [(_style(ctx, colors.spinner), frames[ctx.frame % len(frames)])]
    return [(_style(ctx, colors.spinner_idle), "*" if ctx.ascii_only else "◇")]


def _render_model(ctx: StatusLineContext) -> list[StyleFragment] | None:
    if not ctx.model_name:
        return None
    colors = get_statusline_colors()
    frags: list[StyleFragment] = [(_style(ctx, colors.model), ctx.model_name)]
    if ctx.provider_label:
        frags.append((_style(ctx, colors.dim), f" @{ctx.provider_label}"))
    return frags


def _render_cost(ctx: StatusLineContext) -> list[StyleFragment] | None:
    if ctx.session_cost_usd <= 0:
        return None
    colors = get_statusline_colors()
    text = f"${ctx.session_cost_usd:.2f}"
    if ctx.cost_budget_usd:
        text += f"/${ctx.cost_budget_usd:g}"
    return [(_style(ctx, colors.cost), text)]


def _render_speed(ctx: StatusLineContext) -> list[StyleFragment] | None:
    if not ctx.working:
        return None
    parts: list[str] = []
    if ctx.rate_in and ctx.rate_in > 0:
        parts.append(f"in {ctx.rate_in}")
    if ctx.rate_out and ctx.rate_out > 0:
        parts.append(f"out {ctx.rate_out}")
    if not parts:
        return None
    colors = get_statusline_colors()
    return [(_style(ctx, colors.speed), f"{' '.join(parts)} t/s")]


_EFFORT_BADGES: dict[str, tuple[str, str, str, str]] = {
    "high": ("▲", "^", "high", "effort_hi"),
    "medium": ("◆", "#", "med", "effort_md"),
    "low": ("▽", "v", "low", "effort_lo"),
}


def _render_effort(ctx: StatusLineContext) -> list[StyleFragment] | None:
    badge = _EFFORT_BADGES.get((ctx.effort or "").lower())
    if badge is None:
        return None
    glyph, ascii_glyph, label, color_attr = badge
    colors = get_statusline_colors()
    g = ascii_glyph if ctx.ascii_only else glyph
    return [(_style(ctx, getattr(colors, color_attr)), f"{g} {label}")]


def format_git_badge(info: GitInfo, *, ascii_only: bool) -> str:
    """Branch name + optional status badge, e.g. ``main [± ↑3↓1]``.

    Mirrors the legacy prompt.py badge; ASCII mode swaps the glyphs.
    """
    dirty_glyph = "*" if ascii_only else "±"
    up = "+" if ascii_only else "↑"
    down = "-" if ascii_only else "↓"
    parts: list[str] = []
    if info.dirty:
        parts.append(dirty_glyph)
    sync = ""
    if info.ahead:
        sync += f"{up}{info.ahead}"
    if info.behind:
        sync += f"{down}{info.behind}"
    if sync:
        parts.append(sync)
    if not parts:
        return info.branch
    return f"{info.branch} [{' '.join(parts)}]"


def _render_cwd(ctx: StatusLineContext) -> list[StyleFragment] | None:
    if not ctx.cwd:
        return None
    colors = get_statusline_colors()
    return [(_style(ctx, colors.dir), ctx.cwd)]


def _render_git(ctx: StatusLineContext) -> list[StyleFragment] | None:
    if ctx.git is None:
        return None
    colors = get_statusline_colors()
    badge = format_git_badge(ctx.git, ascii_only=ctx.ascii_only)
    return [(_style(ctx, colors.branch), badge)]


def _render_diff(ctx: StatusLineContext) -> list[StyleFragment] | None:
    added, removed = ctx.diff_added, ctx.diff_removed
    if not added and not removed:
        return None
    colors = get_statusline_colors()
    return [
        (_style(ctx, colors.add), f"+{added or 0}"),
        (_style(ctx, colors.dim), "/"),
        (_style(ctx, colors.delete), f"-{removed or 0}"),
    ]


def _render_flags(ctx: StatusLineContext) -> list[StyleFragment] | None:
    tc = get_toolbar_colors()
    chips = [
        (tc.yolo_label, "yolo", ctx.flags.yolo),
        (tc.auto_label, "auto", ctx.flags.auto),
        (tc.plan_label, "plan", ctx.flags.plan),
    ]
    frags: list[StyleFragment] = []
    for style, label, on in chips:
        if not on:
            continue
        if frags:
            frags.append(("", " "))
        frags.append((_style(ctx, style), label))
    return frags or None


# ---------------------------------------------------------------------------
# Line-2 segment renderers
# ---------------------------------------------------------------------------


def _render_context(ctx: StatusLineContext) -> list[StyleFragment] | None:
    if ctx.max_context_tokens <= 0:
        return None
    colors = get_statusline_colors()
    pct = min(100, ctx.context_tokens * 100 // ctx.max_context_tokens)
    level_color = getattr(colors, f"usage_{usage_level(pct)}")
    used = format_tokens(ctx.context_tokens)
    total = format_tokens(ctx.max_context_tokens)
    frags: list[StyleFragment] = [
        (_style(ctx, colors.label), "ctx "),
        (_style(ctx, level_color), f"{used}"),
        (_style(ctx, colors.dim), "/"),
        (_style(ctx, level_color), f"{total} "),
    ]
    if ctx.style != "plain" and pct >= 70:
        frags.append((level_color, smooth_bar(pct, width=ctx.bar_width, ascii_only=ctx.ascii_only)))
        frags.append(("", " "))
    frags.append((_style(ctx, f"bold {level_color}".strip()), f"{pct}%"))
    if pct >= 90:
        blink = "bold" if ctx.frame % 2 == 0 else ""
        warn_style = f"{blink} {colors.warn}".strip() if ctx.style != "plain" else blink
        prefix = "! " if ctx.ascii_only else "⚠ "
        frags.append(("", "  "))
        frags.append((warn_style, f"{prefix}CTX LOW"))
    return frags


def _render_tokens(ctx: StatusLineContext) -> list[StyleFragment] | None:
    if not ctx.max_context_tokens:
        return None
    colors = get_statusline_colors()
    text = f"{format_tokens(ctx.context_tokens)}/{format_tokens(ctx.max_context_tokens)}"
    return [(_style(ctx, colors.label), text)]


def _render_elapsed(ctx: StatusLineContext) -> list[StyleFragment] | None:
    colors = get_statusline_colors()
    return [
        (_style(ctx, colors.time), format_duration(int(ctx.elapsed_s))),
        (_style(ctx, colors.dim), " elapsed"),
    ]


def _render_limits(ctx: StatusLineContext) -> list[StyleFragment] | None:
    lim = ctx.limits
    if lim is None or (lim.requests_pct is None and lim.tokens_pct is None):
        return None
    colors = get_statusline_colors()
    frags: list[StyleFragment] = []
    for label, pct, reset_s in (
        ("req", lim.requests_pct, lim.requests_reset_s),
        ("tok", lim.tokens_pct, lim.tokens_reset_s),
    ):
        if pct is None:
            continue
        if frags:
            frags.append((_style(ctx, colors.dim), " - " if ctx.ascii_only else " · "))
        level_color = getattr(colors, f"usage_{usage_level(pct)}")
        frags.append((_style(ctx, colors.label), f"{label} "))
        if ctx.style != "plain":
            frags.append((level_color, smooth_bar(pct, width=6, ascii_only=ctx.ascii_only)))
            frags.append(("", " "))
        frags.append((_style(ctx, f"bold {level_color}".strip()), f"{pct}%"))
        if reset_s and reset_s > 0:
            arrow = "@" if ctx.ascii_only else "↻"
            frags.append((_style(ctx, colors.dim), f" {arrow} {format_duration(int(reset_s))}"))
    return frags or None


def _render_clock(ctx: StatusLineContext) -> list[StyleFragment] | None:
    colors = get_statusline_colors()
    return [(_style(ctx, colors.time), ctx.clock)]


# ---------------------------------------------------------------------------
# Display-width helper + per-segment exception isolation + assembler
# ---------------------------------------------------------------------------


def _display_width(text: str) -> int:
    return sum(get_cwidth(c) for c in text)


_warned_segments: set[str] = set()


def _warn_segment_once(seg_id: str) -> None:
    if seg_id not in _warned_segments:
        _warned_segments.add(seg_id)
        logger.exception("statusline: segment {} render failed", seg_id)


_LINE1_GROUP_BREAK_AFTER = {"effort"}  # identity group | location group


def assemble_footer(
    ctx: StatusLineContext, segments: Sequence[str]
) -> tuple[list[StyleFragment], list[StyleFragment]]:
    """Render both footer lines. Pure: no I/O, exceptions isolated per segment."""
    colors = get_statusline_colors()
    plainish = ctx.ascii_only or ctx.style == "plain"
    sep_bar = "  |  " if plainish else "  │  "
    sep_dot = " - " if plainish else " · "
    zones = split_zones(segments)

    def rendered(ids: list[str]) -> list[tuple[str, list[StyleFragment]]]:
        out: list[tuple[str, list[StyleFragment]]] = []
        for seg_id in ids:
            spec = SEGMENT_REGISTRY[seg_id]
            try:
                frags = spec.render(ctx)
            except Exception:
                _warn_segment_once(seg_id)
                continue
            if frags:
                out.append((seg_id, frags))
        return out

    def joined(parts: list[tuple[str, list[StyleFragment]]]) -> list[StyleFragment]:
        frags: list[StyleFragment] = []
        for i, (_, seg_frags) in enumerate(parts):
            if i:
                prev_id = parts[i - 1][0]
                sep = sep_bar if prev_id in _LINE1_GROUP_BREAK_AFTER else sep_dot
                frags.append((_style(ctx, colors.dim), sep))
            frags.extend(seg_frags)
        return frags

    def width(frags: list[StyleFragment]) -> int:
        return sum(_display_width(t) for _, t in frags)

    line1_parts = rendered(zones.line1)
    while width(joined(line1_parts)) > ctx.columns and len(line1_parts) > 1:
        victim = max(line1_parts, key=lambda p: SEGMENT_REGISTRY[p[0]].drop_priority)
        if SEGMENT_REGISTRY[victim[0]].drop_priority == 0:
            break  # only priority-0 left; let prompt.py truncate
        line1_parts.remove(victim)

    line2_ids = zones.line2_right
    # The "context" segment already prints the used/total token ratio, so a
    # standalone "tokens" segment alongside it is pure duplication ("ctx
    # 78k/262k … │ 78k/262k"). Drop the redundant one when both are configured.
    if "context" in line2_ids and "tokens" in line2_ids:
        line2_ids = [seg for seg in line2_ids if seg != "tokens"]
    line2_parts = rendered(line2_ids)
    line2: list[StyleFragment] = []
    for i, (_seg, seg_frags) in enumerate(line2_parts):
        if i:
            line2.append((_style(ctx, colors.dim), sep_bar))
        line2.extend(seg_frags)

    return joined(line1_parts), line2


SEGMENT_REGISTRY: dict[str, SegmentSpec] = {
    "spinner": SegmentSpec("spinner", "line1", _render_spinner, drop_priority=0),
    "model": SegmentSpec("model", "line1", _render_model, drop_priority=1),
    "cost": SegmentSpec("cost", "line1", _render_cost, drop_priority=5),
    "speed": SegmentSpec("speed", "line1", _render_speed, drop_priority=7),
    "effort": SegmentSpec("effort", "line1", _render_effort, drop_priority=4),
    "cwd": SegmentSpec("cwd", "line1", _render_cwd, drop_priority=1),
    "git": SegmentSpec("git", "line1", _render_git, drop_priority=2),
    "diff": SegmentSpec("diff", "line1", _render_diff, drop_priority=6),
    "flags": SegmentSpec("flags", "line1", _render_flags, drop_priority=0),
    "context": SegmentSpec("context", "line2_right", _render_context, drop_priority=0),
    "tokens": SegmentSpec("tokens", "line2_right", _render_tokens, drop_priority=2),
    "elapsed": SegmentSpec("elapsed", "line2_right", _render_elapsed, drop_priority=3),
    "limits": SegmentSpec("limits", "line2_right", _render_limits, drop_priority=1),
    "clock": SegmentSpec("clock", "line2_right", _render_clock, drop_priority=0),
    "command": SegmentSpec("command", "line2_left", _not_rendered, drop_priority=0),
}


def split_zones(segments: Sequence[str]) -> ZoneSplit:
    """Partition the user's ordered segment list by registry zone."""
    z = ZoneSplit(line1=[], line2_right=[], line2_left=[])
    for seg in segments:
        spec = SEGMENT_REGISTRY.get(seg)
        if spec is None:
            continue  # unknown ids stay ignored for forward compat
        getattr(z, spec.zone).append(seg)
    return z


class StatusLineCommandRunner:
    """Runs the user's status command on a cadence and caches one line.

    Fails closed: any timeout, non-zero exit, spawn failure, or empty output
    leaves :attr:`current_line` empty so the footer simply omits the segment.
    The refresh task has an explicit lifecycle (``start``/``stop``) and is
    cancelled cleanly when the prompt session shuts down.
    """

    def __init__(self, command: str, timeout_ms: int, interval_s: float | None = None) -> None:
        self._argv = self._parse_argv(command)
        self._timeout_s = max(timeout_ms, 1) / 1000
        if interval_s is None:
            self._interval_s = max(self._timeout_s, _MIN_REFRESH_INTERVAL_S)
        else:
            self._interval_s = max(interval_s, _MIN_EXPLICIT_INTERVAL_S)
        self._task: asyncio.Task[None] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._warned: set[str] = set()
        self.current_line: str = ""

    @staticmethod
    def _parse_argv(command: str) -> list[str]:
        try:
            return shlex.split(command)
        except ValueError:
            return []

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.is_running:
            return
        self._task = asyncio.get_running_loop().create_task(self._refresh_loop())

    def cancel(self) -> None:
        """Synchronous fire-and-forget cancellation (for sync shutdown paths)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        # Sync shutdown may never re-enter the event loop, so the
        # CancelledError handler in _run_command can't kill the child —
        # do it here as well to avoid orphaning the user's command.
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    async def stop(self) -> None:
        if self._task is None:
            return
        task = self._task
        self.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad refresh must not silently kill the loop — the
                # footer would freeze with no indication of failure.
                logger.exception("statusline: refresh failed (argv={})", self._argv)
                self.current_line = ""
            await asyncio.sleep(self._interval_s)

    async def refresh_once(self) -> None:
        """Run the command once and cache its first stdout line (or '')."""
        self.current_line = await self._run_command()

    async def _run_command(self) -> str:
        if not self._argv:
            self._warn_once("status command is empty or unparsable")
            return ""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            self._warn_once(f"status command failed to start: {exc}")
            return ""
        self._proc = proc
        capped = False
        try:
            assert proc.stdout is not None
            # Bounded read instead of communicate(): a command that streams
            # endlessly can't grow the buffer past the cap. We only need the
            # first line anyway.
            stdout = await asyncio.wait_for(
                proc.stdout.read(_MAX_COMMAND_OUTPUT_BYTES), self._timeout_s
            )
            capped = len(stdout) >= _MAX_COMMAND_OUTPUT_BYTES
            if capped:
                await self._kill_and_reap(proc)
            else:
                try:
                    await asyncio.wait_for(proc.wait(), self._timeout_s)
                except TimeoutError:
                    # Output below the cap but the command keeps running (a
                    # streamer between buffer flushes): keep the captured
                    # first line instead of failing closed.
                    capped = True
                    await self._kill_and_reap(proc)
        except TimeoutError:
            await self._kill_and_reap(proc)
            self._warn_once("status command timed out")
            return ""
        except asyncio.CancelledError:
            # Session shutdown cancels the refresh task mid-read();
            # without this the user's command keeps running as an orphan.
            await self._kill_and_reap(proc)
            raise
        finally:
            self._proc = None
        if proc.returncode != 0 and not capped:
            self._warn_once(f"status command exited with {proc.returncode}")
            return ""
        first_line = stdout.decode("utf-8", errors="replace").split("\n", 1)[0].strip()
        return sanitize_ansi(first_line)[:_MAX_COMMAND_LINE_CHARS]

    async def _kill_and_reap(self, proc: asyncio.subprocess.Process) -> None:
        """Kill the child, then drain stdout so ``proc.wait()`` can finish.

        asyncio resolves ``wait()`` only once the exit status is known AND
        every pipe has reached EOF. The bounded read can leave the stdout
        transport flow-control-paused on a full buffer; without draining it
        the pipe never disconnects and ``wait()`` deadlocks — even after
        SIGKILL.
        """
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        if proc.stdout is not None:
            with contextlib.suppress(Exception):
                while await asyncio.wait_for(
                    proc.stdout.read(_MAX_COMMAND_OUTPUT_BYTES), self._timeout_s
                ):
                    pass
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), self._timeout_s)

    def _warn_once(self, message: str) -> None:
        """Log each distinct failure once so changing errors stay visible
        without spamming the log on every refresh."""
        if message not in self._warned:
            self._warned.add(message)
            logger.warning("statusline: {} (argv={})", message, self._argv)
