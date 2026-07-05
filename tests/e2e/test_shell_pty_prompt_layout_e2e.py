"""Rendered-screen (pyte) e2e tests for the running-prompt input-card layout.

Unlike the byte-stream PTY helpers, these feed the raw terminal bytes to a pyte
virtual screen so assertions run against the *rendered* frame — the only place
an incomplete-erase "ghost"/duplicate row is visible. They pin the regression
where the input card's top border (``──────── ● off``) + ``❯`` fossilized above
the stream as a duplicate "second prompt" after submitting.

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


def _render(chunks: list[bytes]):
    screen = pyte.Screen(_COLS, _ROWS)
    stream = pyte.ByteStream(screen)
    stream.feed(b"".join(chunks))
    return [line.rstrip() for line in screen.display]


# The input-card top border uniquely carries the effort label (``● <level>``)
# beside the rule — ``_render_input_top_border`` is the only place that renders
# it, so this distinguishes it from the footer's own plain separator. Match every
# effort level, not just the default "off", so the matcher can't silently miss a
# thinking-on session and make the fossil assertion pass vacuously. (This test's
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


def test_input_card_never_fossilizes_above_the_stream(tmp_path: Path) -> None:
    """The input card must never appear between the echoed prompt and the stream.

    Regression: after submitting, the card's border + ``❯`` were fossilized above
    the streamed content as a ghost second prompt (the turn's first scrollback
    commit did not erase the card). The card is hidden until that first commit,
    then repaints below the stream so the user can still see where to steer.

    Invariants checked across every frame of a live turn:
      * no fossil card ever appears above the first content row;
      * the submitted prompt is never duplicated;
      * once the turn has committed, the live card is visible again;
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

            assert not _has_fossil_border_above_content(rows), (
                "ghost input-card border fossilized above the stream:\n"
                + "\n".join(r for r in rows if r.strip())
            )
            assert joined.count(_PROMPT_TEXT) <= 1, "submitted prompt duplicated (ghost)"

            # The card must return WHILE the turn is still streaming (not only at
            # the idle end): the first tool has committed, the second is still
            # running, and the final text has not arrived — yet the card shows.
            mid_turn = "Command executed successfully." in joined and "All done." not in joined
            if mid_turn and any(_is_input_card_border(r) for r in rows):
                output = "\n".join(row for row in rows if _PROMPT_TEXT not in row)
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
