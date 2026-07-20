"""Tests for the customizable status line: config model, segment resolver,
and the external status command runner."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest
from pydantic import ValidationError

from pythinker_code.config import Config, StatusLineConfig, TUIConfig
from pythinker_code.ui.shell.statusline import (
    StatusLineCommandRunner,
    resolve_segments,
)

# ---------------------------------------------------------------------------
# StatusLineConfig model
# ---------------------------------------------------------------------------


def test_config_has_statusline_section_with_defaults():
    cfg = Config()
    sl = cfg.tui.statusline
    assert isinstance(sl, StatusLineConfig)
    assert sl.enabled is True
    assert sl.segments == [
        "spinner",
        "model",
        "cost",
        "speed",
        "effort",
        "cwd",
        "git",
        "diff",
        "flags",
        "context",
        "elapsed",
        "clock",
    ]
    assert sl.command is None
    assert sl.command_timeout_ms == 1000


def test_statusline_unknown_segment_ids_are_dropped():
    sl = StatusLineConfig(segments=["cwd", "bogus-from-the-future", "model"])
    assert sl.segments == ["cwd", "model"]


def test_statusline_duplicate_segment_ids_are_deduped_keeping_first():
    sl = StatusLineConfig(segments=["model", "cwd", "model"])
    assert sl.segments == ["model", "cwd"]


def test_statusline_timeout_must_be_positive():
    with pytest.raises(ValidationError):
        StatusLineConfig(command_timeout_ms=0)


def test_statusline_round_trips_through_dump():
    sl = StatusLineConfig(segments=["model", "git"], command="echo hi")
    cfg = Config(tui=TUIConfig(statusline=sl))
    raw = cfg.model_dump(mode="json")
    restored = Config.model_validate(raw)
    assert restored.tui.statusline.segments == ["model", "git"]
    assert restored.tui.statusline.command == "echo hi"


# ---------------------------------------------------------------------------
# resolve_segments
# ---------------------------------------------------------------------------


def test_resolve_segments_default_layout():
    # The legacy resolver only recognizes the slice-1 zone sets; the new v2
    # default list adds segments it filters out (spinner/cost/speed/etc.), so
    # only cwd/git/flags land on line 1 and model/context on line 2 here.
    # Task 9's renderer swap replaces this path with the registry assembler.
    layout = resolve_segments(StatusLineConfig())
    assert layout.line1 == ["cwd", "git", "flags"]
    assert layout.line2_right == ["model", "context"]
    assert layout.show_command is False


def test_resolve_segments_disabled_master_switch_keeps_everything():
    # enabled=False means "render the stock footer"; resolver reports defaults.
    layout = resolve_segments(StatusLineConfig(enabled=False, segments=["model"]))
    assert layout.line1 == ["cwd", "git", "flags"]
    assert layout.line2_right == ["context", "model"]
    assert layout.show_command is False


def test_resolve_segments_respects_order_and_omissions():
    layout = resolve_segments(StatusLineConfig(segments=["git", "cwd", "model"]))
    assert layout.line1 == ["git", "cwd"]
    assert layout.line2_right == ["model"]


def test_resolve_segments_command_segment_requires_configured_command():
    no_cmd = resolve_segments(StatusLineConfig(segments=["cwd", "command"]))
    assert no_cmd.show_command is False
    with_cmd = resolve_segments(StatusLineConfig(segments=["cwd", "command"], command="echo hi"))
    assert with_cmd.show_command is True


def test_resolve_segments_empty_list_renders_nothing_optional():
    layout = resolve_segments(StatusLineConfig(segments=[]))
    assert layout.line1 == []
    assert layout.line2_right == []
    assert layout.show_command is False


# ---------------------------------------------------------------------------
# StatusLineCommandRunner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_runner_caches_first_stdout_line():
    runner = StatusLineCommandRunner(
        command=f"{sys.executable} -c \"print('line-one'); print('line-two')\"",
        timeout_ms=5000,
    )
    await runner.refresh_once()
    assert runner.current_line == "line-one"


@pytest.mark.asyncio
async def test_command_runner_timeout_fails_closed():
    runner = StatusLineCommandRunner(
        command=f'{sys.executable} -c "import time; time.sleep(10)"',
        timeout_ms=100,
    )
    await runner.refresh_once()
    assert runner.current_line == ""


@pytest.mark.asyncio
async def test_command_runner_nonzero_exit_fails_closed():
    runner = StatusLineCommandRunner(
        command=f'{sys.executable} -c "raise SystemExit(3)"',
        timeout_ms=5000,
    )
    await runner.refresh_once()
    assert runner.current_line == ""


@pytest.mark.asyncio
async def test_command_runner_invalid_command_fails_closed():
    runner = StatusLineCommandRunner(command="definitely-not-a-real-binary-xyz", timeout_ms=1000)
    await runner.refresh_once()
    assert runner.current_line == ""


@pytest.mark.asyncio
async def test_command_runner_output_is_capped():
    runner = StatusLineCommandRunner(
        command=f"{sys.executable} -c \"print('x' * 1000)\"",
        timeout_ms=5000,
    )
    await runner.refresh_once()
    assert 0 < len(runner.current_line) <= 200


@pytest.mark.asyncio
async def test_command_runner_strips_ansi_sequences():
    runner = StatusLineCommandRunner(
        command=f"{sys.executable} -c \"print('\\x1b[31mred\\x1b[0m \\x1b]0;title\\x07done')\"",
        timeout_ms=5000,
    )
    await runner.refresh_once()
    assert runner.current_line == "red done"


@pytest.mark.asyncio
async def test_command_runner_cancellation_kills_subprocess(tmp_path):
    # The child writes its pid then sleeps; cancelling the in-flight refresh
    # must kill it rather than leaving an orphan behind.
    pid_file = tmp_path / "pid"
    runner = StatusLineCommandRunner(
        command=(
            f'{sys.executable} -c "import os, pathlib, time; '
            f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
            'time.sleep(30)"'
        ),
        timeout_ms=60_000,
    )
    task = asyncio.get_running_loop().create_task(runner.refresh_once())
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.02)
    else:
        task.cancel()
        pytest.fail("status command subprocess never wrote its pid")
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    else:
        os.kill(pid, 9)
        pytest.fail("status command subprocess survived cancellation")


@pytest.mark.asyncio
async def test_command_runner_lifecycle_start_stop():
    runner = StatusLineCommandRunner(
        command=f"{sys.executable} -c \"print('tick')\"",
        timeout_ms=5000,
        interval_s=0.05,
    )
    runner.start()
    try:
        for _ in range(100):
            if runner.current_line == "tick":
                break
            await asyncio.sleep(0.02)
        assert runner.current_line == "tick"
    finally:
        await runner.stop()
    assert runner.is_running is False


@pytest.mark.asyncio
async def test_command_runner_repeated_start_and_stop_are_idempotent(monkeypatch):
    runner = StatusLineCommandRunner(command="echo hi", timeout_ms=5000)
    refresh_started = asyncio.Event()

    async def blocking_refresh():
        refresh_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner, "refresh_once", blocking_refresh)
    runner.start()
    first_task = runner._task
    runner.start()
    await refresh_started.wait()

    assert runner._task is first_task
    await runner.stop()
    await runner.stop()
    assert runner.is_running is False


# ---------------------------------------------------------------------------
# Footer render integration
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402
from typing import Any  # noqa: E402

from pythinker_code.soul import StatusSnapshot  # noqa: E402
from pythinker_code.ui.shell import prompt as shell_prompt  # noqa: E402
from pythinker_code.ui.shell.prompt import CustomPromptSession, PromptMode  # noqa: E402


def _make_session(statusline: StatusLineConfig | None = None) -> Any:
    session = object.__new__(CustomPromptSession)
    session._mode = PromptMode.AGENT
    session._model_name = "fast-model"
    session._model_capabilities = set()
    session._thinking = False
    session._status_provider = lambda: StatusSnapshot(context_usage=0.0)
    session._background_task_count_provider = None
    session._tips = []
    session._tip_rotation_index = 0
    session._last_tip_rotate_time = float("inf")
    cfg = statusline if statusline is not None else StatusLineConfig()
    session._statusline_cfg = cfg
    session._statusline_layout = resolve_segments(cfg)
    return session


def _render_card(session: Any, monkeypatch: pytest.MonkeyPatch, width: int = 120) -> str:
    class _DummyOutput:
        @staticmethod
        def get_size() -> Any:
            return SimpleNamespace(columns=width)

    monkeypatch.setenv("PYTHINKER_TUI_STYLE", "card")
    monkeypatch.setattr(
        shell_prompt, "get_app_or_none", lambda: SimpleNamespace(output=_DummyOutput())
    )
    monkeypatch.setattr(shell_prompt, "_get_git_branch", lambda: "main")
    monkeypatch.setattr(shell_prompt, "_get_git_status", lambda: (False, 0, 0))
    monkeypatch.setattr(shell_prompt, "_get_git_diffstat", lambda: None)
    monkeypatch.setattr(shell_prompt, "_shorten_cwd", lambda _: "~/proj")
    monkeypatch.setattr("pythinker_code.extensions.footer_statuses", lambda: {})
    fragments = session._render_bottom_toolbar()
    return "".join(fragment[1] for fragment in fragments)


def test_card_footer_default_layout_shows_all_segments(monkeypatch: pytest.MonkeyPatch):
    plain = _render_card(_make_session(StatusLineConfig()), monkeypatch)
    assert "~/proj" in plain
    assert "main" in plain
    assert "fast-model" in plain


def test_card_footer_segments_can_be_hidden(monkeypatch: pytest.MonkeyPatch):
    plain = _render_card(
        _make_session(StatusLineConfig(segments=["context", "model"])), monkeypatch
    )
    assert "~/proj" not in plain
    assert "main" not in plain
    assert "fast-model" in plain


def test_card_footer_disabled_customization_renders_stock_footer(monkeypatch: pytest.MonkeyPatch):
    disabled = _render_card(
        _make_session(StatusLineConfig(enabled=False, segments=["model"])), monkeypatch
    )
    # Disabling customization renders the plain stock footer regardless of the
    # configured segments: the default stock segments (cwd/git/model) still show
    # despite segments=["model"], and the fancy spinner glyph is absent.
    assert "~/proj" in disabled
    assert "main" in disabled
    assert "fast-model" in disabled
    assert "◇" not in disabled


def test_card_footer_shows_external_command_line(monkeypatch: pytest.MonkeyPatch):
    cfg = StatusLineConfig(
        segments=["cwd", "git", "flags", "context", "tokens", "model", "command"],
        command="echo hi",
    )
    session = _make_session(cfg)
    runner = StatusLineCommandRunner(command="echo hi", timeout_ms=1000)
    runner.current_line = "build: green"
    session._statusline_runner = runner
    plain = _render_card(session, monkeypatch)
    assert "build: green" in plain


@pytest.mark.asyncio
async def test_refresh_loop_survives_refresh_exception(monkeypatch):
    """One bad refresh must not kill the loop — the next tick still runs."""
    runner = StatusLineCommandRunner(command="echo hi", timeout_ms=5000, interval_s=0.01)
    calls: list[int] = []

    async def flaky_refresh():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("boom")
        runner.current_line = "recovered"

    monkeypatch.setattr(runner, "refresh_once", flaky_refresh)
    runner.start()
    try:
        for _ in range(200):
            if runner.current_line == "recovered":
                break
            await asyncio.sleep(0.01)
        assert runner.current_line == "recovered"
        assert len(calls) >= 2
    finally:
        await runner.stop()


def test_explicit_interval_is_clamped_to_positive_floor():
    runner = StatusLineCommandRunner(command="echo hi", timeout_ms=5000, interval_s=0.0)
    assert runner._interval_s > 0
    negative = StatusLineCommandRunner(command="echo hi", timeout_ms=5000, interval_s=-5.0)
    assert negative._interval_s > 0


@pytest.mark.asyncio
async def test_command_output_is_capped_not_buffered_unbounded():
    """A command spewing endless output still yields its first line promptly."""
    runner = StatusLineCommandRunner(
        command=(
            f'{sys.executable} -c "import sys\n'
            "print('first line')\n"
            "while True: sys.stdout.write('x' * 8192)\""
        ),
        timeout_ms=5000,
    )
    await asyncio.wait_for(runner.refresh_once(), 10)
    assert runner.current_line == "first line"


def test_warn_once_logs_each_distinct_message(monkeypatch):
    from pythinker_code.ui.shell import statusline as statusline_mod

    warnings: list[str] = []
    monkeypatch.setattr(
        statusline_mod.logger, "warning", lambda msg, *a, **kw: warnings.append(str(a[0]))
    )
    runner = StatusLineCommandRunner(command="echo hi", timeout_ms=5000)
    runner._warn_once("first failure")
    runner._warn_once("first failure")
    runner._warn_once("second failure")
    assert warnings == ["first failure", "second failure"]
