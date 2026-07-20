"""Rendered-screen (pyte) e2e tests for the running-prompt input-card layout.

Unlike the byte-stream PTY helpers, these feed the raw terminal bytes to a pyte
virtual screen so assertions run against the *rendered* frame — the only place
an incomplete-erase accepted-buffer "ghost"/duplicate row is visible. They pin
Focus TUI fossilization behavior and the normal prompt card's visible
loading/mid-turn contract.

This is a manual/local check, not a CI-enforced one — it is skipped on CI (see
``pytestmark`` below: scripted_echo + prompt_toolkit hang on GitHub Actions'
PTY). It is the only test that actually exercises the real ``run_in_terminal``
erase/redraw timing where the fossil originates; run it locally after any change
to the scrollback-handoff or input-card code. CI's only automated guard against
this regression is the narrower renderer-contract test
(``test_render_agent_prompt_message_honors_input_card_gate`` in
``tests/ui_and_conv/test_visualize_running_prompt.py``), which proves the
renderer reads the hide/show gate but cannot observe real terminal erase
behavior the way this test does.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from tests.e2e.shell_pty_helpers import (
    _set_window_size,
    list_turn_begin_inputs,
    make_home_dir,
    make_work_dir,
    read_until_prompt_ready,
    start_shell_pty,
    write_scripted_config,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or os.environ.get("CI") == "true",
    reason=(
        "Shell PTY E2E tests require a Unix-like PTY; skipped on CI runners "
        "(scripted_echo + prompt_toolkit hang on GitHub Actions)."
    ),
)

pyte = pytest.importorskip("pyte")

_COLS, _ROWS = 120, 40
_PROMPT_TEXT = "this is a prompt to the agent"
_QUEUED_FOLLOW_UP = "queued follow-up ghost regression 7f3a"


def _render(chunks: list[bytes]):
    screen = pyte.Screen(_COLS, _ROWS)
    stream = pyte.ByteStream(screen)
    stream.feed(b"".join(chunks))
    return [line.rstrip() for line in screen.display]


# The input-card top border uniquely carries the effort label (``● <level>``)
# beside the rule — ``_render_input_top_border`` is the only place that renders
# it, so this distinguishes it from the footer's own plain separator. Match every
# effort level, not just the default "off", so the matcher can't silently miss a
# thinking-on session and make prompt-card assertions pass vacuously. (This test's
# scripted model supports non-native thinking, so the label is always present;
# the idle-card assertion below also fails loudly if the matcher ever stops
# matching.)
_INPUT_CARD_EFFORT_LABEL = re.compile(r"●\s*(off|low|medium|high|max)\b")


def _is_input_card_border(row: str) -> bool:
    return "─" in row and bool(_INPUT_CARD_EFFORT_LABEL.search(row))


def _has_fossil_border_above_content(rows: list[str]) -> bool:
    """True if an input-card border sits between the echoed prompt and the first
    committed ``⏺`` content row — i.e. a fossilized ghost card above the stream."""
    echo_i = next((i for i, r in enumerate(rows) if _PROMPT_TEXT in r), None)
    content_i = next((i for i, r in enumerate(rows) if r.strip().startswith("⏺")), None)
    if echo_i is None or content_i is None or content_i <= echo_i:
        return False
    return any(_is_input_card_border(rows[i]) for i in range(echo_i + 1, content_i))


def _queued_text_fossilized_as_card(rows: list[str], text: str) -> bool:
    """True if the queued follow-up rendered as a fossilized accepted-input card.

    The queued-input ghost commits the accepted follow-up into scrollback as a
    bordered ``● <effort>`` input card, so its text ends up wedged between a card
    border directly above and committed ``⏺`` turn content directly below. That
    signature excludes the two healthy renderings: the live queued display (text
    plus the ``↑ to edit`` hint, with no card border directly above) and the
    execute-echo once the queued turn drains (committed like any turn input, again
    with no card border directly above). A/B-verified: fires on the pre-fix code
    and stays silent on the fixed code across every rendered frame.
    """
    for i, row in enumerate(rows):
        if text not in row:
            continue
        border_above = any(_is_input_card_border(rows[j]) for j in range(max(0, i - 2), i))
        content_below = any(
            rows[j].strip().startswith("⏺") for j in range(i + 1, min(len(rows), i + 3))
        )
        if border_above and content_below:
            return True
    return False


def test_focus_tui_hides_files_and_never_fossilizes_prompt(tmp_path: Path) -> None:
    write = {
        "id": "w1",
        "name": "WriteFile",
        "arguments": json.dumps({"path": "src/a.py", "content": "x"}),
    }
    config_path = write_scripted_config(
        tmp_path,
        [f"tool_call: {json.dumps(write)}", "text: Done."],
        capabilities=["thinking"],
        extra_config={"tui": {"focus_mode": True}},
    )
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    shell = start_shell_pty(
        config_path=config_path,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
        columns=_COLS,
        lines=_ROWS,
    )
    try:
        shell.read_until_contains("think first, then code")
        read_until_prompt_ready(shell, after=shell.mark())
        shell.send_line(_PROMPT_TEXT)
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            shell.read_available(timeout=0.08)
            rows = _render(shell._raw_chunks)
            joined = "\n".join(rows)
            assert not _has_fossil_border_above_content(rows)
            assert "src/a.py" not in joined
            if "Done." in shell.normalized_text():
                break
        assert "Done." in shell.normalized_text()
    finally:
        shell.close()


def test_input_card_stays_visible_during_initial_loading_and_mid_turn(tmp_path: Path) -> None:
    """The empty input card stays visible while the agent starts working.

    Invariants checked across every frame of a live turn:
      * the submitted prompt is never duplicated;
      * the empty card is visible before and after the first committed content;
      * the idle card returns after the turn ends.
    """
    fast = {"id": "c1", "name": "Shell", "arguments": json.dumps({"command": "true"})}
    slow = {"id": "c2", "name": "Shell", "arguments": json.dumps({"command": "sleep 3"})}
    config_path = write_scripted_config(
        tmp_path,
        [f"tool_call: {json.dumps(fast)}", f"tool_call: {json.dumps(slow)}", "text: All done."],
        capabilities=["thinking"],
    )
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    shell = start_shell_pty(
        config_path=config_path,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
        columns=_COLS,
        lines=_ROWS,
    )
    try:
        shell.read_until_contains("think first, then code")
        read_until_prompt_ready(shell, after=shell.mark())
        assert any(_is_input_card_border(r) for r in _render(shell._raw_chunks)), (
            "idle input-card border missing before the turn"
        )

        shell.send_line(_PROMPT_TEXT)

        live_card_seen_mid_turn = False
        turn_done = False
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            shell.read_available(timeout=0.08)
            rows = _render(shell._raw_chunks)
            joined = "\n".join(rows)

            assert joined.count(_PROMPT_TEXT) <= 1, "submitted prompt duplicated (ghost)"

            # The card must return WHILE the turn is still streaming (not only at
            # the idle end): the first tool has committed, the second is still
            # running, and the final text has not arrived — yet the card shows.
            mid_turn = "Command executed successfully." in joined and "All done." not in joined
            output = "\n".join(row for row in rows if _PROMPT_TEXT not in row)
            if mid_turn and any(_is_input_card_border(r) for r in rows) and "❯" in output:
                assert output.count("❯") == 1
                assert "────────" in output
                live_card_seen_mid_turn = True
            # Detect completion from the full byte stream (it may scroll off screen).
            if "All done." in shell.normalized_text():
                turn_done = True
                break

        assert turn_done, "turn did not complete in time"
        assert live_card_seen_mid_turn, (
            "live input card never reappeared while the turn was still streaming"
        )

        # Turn ended: the idle card is back once the prompt settles.
        shell.wait_for_quiet(timeout=6.0, quiet_period=0.3)
        assert any(_is_input_card_border(r) for r in _render(shell._raw_chunks)), (
            "idle input-card border did not return after the turn ended"
        )
    finally:
        shell.close()


def _render_sized(chunks: list[bytes], columns: int, rows: int) -> list[str]:
    screen = pyte.Screen(columns, rows)
    stream = pyte.ByteStream(screen)
    stream.feed(b"".join(chunks))
    return [line.rstrip() for line in screen.display]


def test_prompt_scene_survives_shrinking_terminal_heights(tmp_path: Path) -> None:
    """Mid-turn resizes down to tiny heights never crash or fossilize the card.

    Resizes the live PTY through heights 12 → 8 → 6 → 4 while a slow tool keeps
    the running prompt on screen. prompt_toolkit fully redraws on SIGWINCH, so
    each post-resize frame is rendered from only the bytes emitted after that
    resize, on a pyte screen of the new geometry. After restoring the original
    size, the turn must still complete and the idle input card must return.
    """
    slow = {"id": "r1", "name": "Shell", "arguments": json.dumps({"command": "sleep 6"})}
    config_path = write_scripted_config(
        tmp_path,
        [f"tool_call: {json.dumps(slow)}", "text: Resize turn finished."],
        capabilities=["thinking"],
    )
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    shell = start_shell_pty(
        config_path=config_path,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
        columns=_COLS,
        lines=_ROWS,
    )
    try:
        shell.read_until_contains("think first, then code")
        read_until_prompt_ready(shell, after=shell.mark())
        shell.send_line(_PROMPT_TEXT)
        shell.read_until_contains("Bash(sleep 6", timeout=15.0)

        for height in (12, 8, 6, 4):
            resize_chunk_start = len(shell._raw_chunks)
            _set_window_size(shell.master_fd, columns=_COLS, lines=height)
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                shell.read_available(timeout=0.08)
            assert shell.process.poll() is None, f"shell died after resize to {height} rows"
            post_resize = shell._raw_chunks[resize_chunk_start:]
            if post_resize:
                rows = _render_sized(post_resize, _COLS, height)
                assert len(rows) == height
                assert not _has_fossil_border_above_content(rows)

        _set_window_size(shell.master_fd, columns=_COLS, lines=_ROWS)
        shell.read_until_contains("Resize turn finished.", timeout=20.0)
        shell.wait_for_quiet(timeout=6.0, quiet_period=0.3)
        assert any(_is_input_card_border(r) for r in _render(shell._raw_chunks)), (
            "idle input-card border did not return after the resize sequence"
        )
    finally:
        shell.close()


def test_mid_turn_queued_input_renders_once_and_executes_once(tmp_path: Path) -> None:
    slow = {
        "id": "queued-slow",
        "name": "Shell",
        "arguments": json.dumps({"command": "sleep 3"}),
    }
    config_path = write_scripted_config(
        tmp_path,
        [
            f"tool_call: {json.dumps(slow)}",
            "text: First turn finished.",
            "text: Queued follow-up executed.",
        ],
        capabilities=["thinking"],
    )
    work_dir = make_work_dir(tmp_path)
    home_dir = make_home_dir(tmp_path)
    shell = start_shell_pty(
        config_path=config_path,
        work_dir=work_dir,
        home_dir=home_dir,
        yolo=True,
        columns=_COLS,
        lines=_ROWS,
    )
    try:
        shell.read_until_contains("think first, then code")
        read_until_prompt_ready(shell, after=shell.mark())
        assert any(_is_input_card_border(row) for row in _render(shell._raw_chunks))

        first_turn_mark = shell.mark()
        shell.send_line(_PROMPT_TEXT)
        shell.read_until_contains("Bash(sleep 3", after=first_turn_mark, timeout=15.0)
        shell.send_line(_QUEUED_FOLLOW_UP)

        # The queued follow-up must render as the intentional ``❯ … / ↑ to edit``
        # row and never fossilize into a bordered accepted-input card (the ghost).
        # Steady state legitimately shows the text twice — the live queued display
        # and, after drain, the execute-echo — so the guard is "never a fossilized
        # accepted-input card", not a raw occurrence count.
        queued_hint_seen = False
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            shell.read_available(timeout=0.08)
            rows = _render(shell._raw_chunks)
            joined = "\n".join(rows)

            assert not _queued_text_fossilized_as_card(rows, _QUEUED_FOLLOW_UP), (
                "queued follow-up fossilized as a bordered ghost card"
            )
            if _QUEUED_FOLLOW_UP in joined and "↑ to edit · ctrl-s to send immediately" in joined:
                queued_hint_seen = True
            if "First turn finished." in shell.normalized_text():
                break

        assert queued_hint_seen, "intentional queued-message row was never rendered"
        shell.read_until_contains("Queued follow-up executed.", timeout=15.0)
        shell.wait_for_quiet(timeout=6.0, quiet_period=0.3)

        assert not _queued_text_fossilized_as_card(_render(shell._raw_chunks), _QUEUED_FOLLOW_UP), (
            "queued follow-up fossilized as a bordered ghost card in the settled frame"
        )

        turn_inputs = list_turn_begin_inputs(home_dir, work_dir)
        assert turn_inputs == [_PROMPT_TEXT, _QUEUED_FOLLOW_UP]
        assert turn_inputs.count(_QUEUED_FOLLOW_UP) == 1
        assert any(_is_input_card_border(row) for row in _render(shell._raw_chunks)), (
            "idle input-card border did not return after the queued turn ended"
        )
    finally:
        shell.close()
