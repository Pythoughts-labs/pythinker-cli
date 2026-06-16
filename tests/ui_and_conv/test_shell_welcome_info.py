import io

from rich.console import Console
from rich.text import Text

from pythinker_code.ui import shell as shell_module


def test_shell_welcome_uses_pythinker_code_copy(monkeypatch):
    console = Console(record=True, width=120, color_system=None)
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")

    shell_module._print_welcome_info("Pythinker Code", [])

    output = console.export_text()
    assert "Pythinker Code v9.9.9" in output
    assert "Welcome to Pythinker" in output
    assert "think first" in output


def test_directory_label_uses_info_token():
    from pythinker_code.ui.shell import WelcomeInfoItem, _value_style_for_label
    from pythinker_code.ui.theme import get_tui_tokens, set_active_theme

    set_active_theme("dark")
    style = _value_style_for_label("Directory", WelcomeInfoItem.Level.INFO)
    assert get_tui_tokens("dark").info in style


def test_welcome_banner_chip_shown_in_output(monkeypatch):
    console = Console(record=True, width=120, color_system=None)
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")

    chip = Text("↑ Update available — v1.0.0 · /update")
    shell_module._print_welcome_info("Pythinker Code", [], banner=chip)

    output = console.export_text()
    assert "Update available" in output
    assert "/update" in output
    assert "Welcome to Pythinker" in output


def test_welcome_banner_chip_update_wins_over_whats_new(monkeypatch):
    monkeypatch.setattr(shell_module, "consume_whats_new", lambda: "0.25.0")
    monkeypatch.setattr(shell_module, "welcome_update_target", lambda: "0.26.0")

    chip = shell_module._welcome_banner_chip()

    assert chip is not None
    text = chip.plain
    assert "Update available" in text
    assert "0.26.0" in text
    assert "What's new" not in text


def test_welcome_banner_no_chip_unchanged(monkeypatch):
    console_with = Console(record=True, width=120, color_system=None)
    console_without = Console(record=True, width=120, color_system=None)
    monkeypatch.setattr(shell_module, "get_version", lambda: "0.26.0")

    monkeypatch.setattr(shell_module, "console", console_without)
    shell_module._print_welcome_info("Pythinker Code", [])
    out_without = console_without.export_text()

    monkeypatch.setattr(shell_module, "console", console_with)
    shell_module._print_welcome_info("Pythinker Code", [], banner=None)
    out_with = console_with.export_text()

    # Both paths produce the same output when banner is None.
    assert out_without == out_with
    assert "Welcome to Pythinker" in out_without


def test_welcome_chip_renders_in_footer_not_header(monkeypatch):
    console = Console(record=True, width=120, color_system=None)
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")

    chip = Text("✦ What's new in v9.9.9 · /changelog")
    shell_module._print_welcome_info("Pythinker Code", [], banner=chip)

    lines = [ln for ln in console.export_text().splitlines() if ln.strip()]
    # Chip sits on the bottom border (footer), not in the header.
    assert "changelog" in lines[-1]
    assert all("changelog" not in ln for ln in lines[:3])


def test_welcome_info_grid_has_no_pipe_separator(monkeypatch):
    from pythinker_code.ui.shell import WelcomeInfoItem

    console = Console(record=True, width=120, color_system=None)
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")

    items = [WelcomeInfoItem(name="Directory", value="/tmp/proj")]
    shell_module._print_welcome_info("Pythinker Code", items)

    out = console.export_text()
    dir_line = next(ln for ln in out.splitlines() if "Directory" in ln)
    # Only the two panel-edge pipes remain; the separator column is gone.
    assert dir_line.count("│") == 2
    assert "/tmp/proj" in dir_line


def test_welcome_strapline_and_help_on_separate_lines(monkeypatch):
    console = Console(record=True, width=120, color_system=None)
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")

    shell_module._print_welcome_info("Pythinker Code", [])

    out = console.export_text()
    assert "Build with confidence." in out
    assert "Type /help for commands." in out
    # The strapline and the help line must not share one rendered line.
    assert not any("Build with confidence." in ln and "Type /help" in ln for ln in out.splitlines())


def test_welcome_banner_layout_width_matrix(monkeypatch):
    from pythinker_code.ui.shell import WelcomeInfoItem
    from pythinker_code.ui.shell.components.render_utils import cell_width

    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")
    # Pin Unicode glyph rendering so the robot-mark assertion below is
    # deterministic regardless of the ambient locale/stdout encoding.
    monkeypatch.setattr(shell_module, "ascii_glyphs_enabled", lambda: False)
    items = [
        WelcomeInfoItem(name="Directory", value="/home/ai/Projects/pythinker-code-main"),
        WelcomeInfoItem(name="Model", value="gpt-5.1-codex"),
        WelcomeInfoItem(name="Branch", value="feat/welcome-banner-redesign"),
        WelcomeInfoItem(
            name="Tip",
            value="Use /update after release promotion completes and /help for commands.",
        ),
    ]

    for width in (60, 80, 120):
        console = Console(record=True, width=width, color_system=None)
        monkeypatch.setattr(shell_module, "console", console)

        shell_module._print_welcome_info(
            "Pythinker Code",
            items,
            banner=Text("↑ Update available — v9.9.10 · /update"),
        )

        output = console.export_text()
        lines = [line.rstrip() for line in output.splitlines() if line.strip()]
        # The panel spans the full terminal width, like the prompt rules.
        assert any(cell_width(line) == width for line in lines)
        assert all(cell_width(line) <= width for line in lines)
        assert "Pythinker Code v9.9.9" in lines[0]
        assert "Welcome to Pythinker" in output
        assert "Directory" in output
        assert "gpt-5.1-codex" in output
        assert "Tips" in output
        assert "/update" in lines[-1]
        assert "/help" in output
        # The robot mark renders at every width, including the compact 60-col
        # layout where it stacks above the copy instead of being dropped.
        assert "▛" in output


def test_welcome_two_column_layout_when_wide(monkeypatch):
    from pythinker_code.ui.shell import WelcomeInfoItem

    console = Console(record=True, width=120, color_system=None)
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")
    monkeypatch.setattr(shell_module, "ascii_glyphs_enabled", lambda: False)

    items = [
        WelcomeInfoItem(name="Directory", value="/tmp/proj"),
        WelcomeInfoItem(name="Tip", value="Type /help for commands."),
    ]
    shell_module._print_welcome_info("Pythinker Code", items)

    lines = console.export_text().splitlines()
    # Welcome copy and the Tips column share the first content row, separated
    # by the vertical divider (panel edges + divider = 3 pipes).
    head_line = next(ln for ln in lines if "Welcome to Pythinker" in ln)
    assert "Tips" in head_line
    assert head_line.count("│") == 3
    # The robot mark renders in the left column.
    assert any("▛" in ln for ln in lines)
    # Facts sit inside the left column, so the divider crosses their rows too.
    dir_line = next(ln for ln in lines if "Directory" in ln)
    assert dir_line.count("│") == 3
    assert "/tmp/proj" in dir_line


def test_welcome_compact_terminal_stacks_robot_above_copy(monkeypatch):
    """In a compact terminal the robot is prioritized: it stacks centered above
    the welcome copy instead of being dropped (the pre-fix behavior)."""
    from pythinker_code.ui.shell import WelcomeInfoItem

    console = Console(record=True, width=58, color_system=None)
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")
    monkeypatch.setattr(shell_module, "ascii_glyphs_enabled", lambda: False)

    shell_module._print_welcome_info(
        "Pythinker Code",
        [WelcomeInfoItem(name="Tip", value="Type /help for commands.")],
    )

    lines = [ln for ln in console.export_text().splitlines() if ln.strip()]
    robot_line = next(i for i, ln in enumerate(lines) if "▛" in ln)
    welcome_line = next(i for i, ln in enumerate(lines) if "Welcome to Pythinker" in ln)
    # Robot is shown (not dropped) and sits on its own line above the copy.
    assert robot_line < welcome_line
    assert "Welcome to Pythinker" not in lines[robot_line]


def test_welcome_ascii_mode_emits_pure_ascii(monkeypatch):
    from pythinker_code.ui.shell import WelcomeInfoItem

    console = Console(record=True, width=120, color_system=None)
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")
    monkeypatch.setattr(shell_module, "ascii_glyphs_enabled", lambda: True)

    items = [
        WelcomeInfoItem(
            name="Auto-save",
            value="~/.pythinker/sessions/" + "a" * 120 + "/context.json",
        ),
        WelcomeInfoItem(name="Tip", value="No AGENTS.md found — run /init to generate one."),
    ]
    shell_module._print_welcome_info("Pythinker Code", items)

    output = console.export_text()
    assert output.isascii(), [ch for ch in set(output) if not ch.isascii()]
    assert "Welcome to Pythinker" in output
    assert "Tips" in output


def test_welcome_chip_degrades_to_ascii(monkeypatch):
    monkeypatch.setattr(shell_module, "consume_whats_new", lambda: None)
    monkeypatch.setattr(shell_module, "welcome_update_target", lambda: "1.2.3")
    monkeypatch.setattr(shell_module, "ascii_glyphs_enabled", lambda: True)

    chip = shell_module._welcome_banner_chip()

    assert chip is not None
    assert chip.plain.isascii()
    assert "Update available" in chip.plain


def test_logo_antenna_never_uses_sgr_blink(monkeypatch):
    """The boot animation owns blinking now; the logo itself must not carry the
    terminal's infinite slow-blink attribute in either motion mode."""
    for disabled in (False, True):
        monkeypatch.setattr(shell_module, "motion_disabled", lambda d=disabled: d)
        spans = shell_module._logo_text().spans
        assert not any("blink" in str(span.style) for span in spans)


def _terminal_console(width: int = 100, height: int = 50) -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        force_terminal=True,
        width=width,
        height=height,
        color_system="truecolor",
    )
    return console, buffer


def test_welcome_banner_blinks_antenna_seven_times_then_stops(monkeypatch):
    """On an interactive terminal the antenna ball blinks exactly
    _ANTENNA_BLINKS times after the banner prints, then pins steady: each
    blink is an off+on rewrite of the single antenna cell, plus one final
    steady write."""
    console, buffer = _terminal_console()
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")
    monkeypatch.setattr(shell_module, "ascii_glyphs_enabled", lambda: False)
    monkeypatch.setattr(shell_module, "motion_disabled", lambda: False)
    monkeypatch.setattr(shell_module, "_ANTENNA_BLINK_OFF_SECONDS", 0.0)
    monkeypatch.setattr(shell_module, "_ANTENNA_BLINK_ON_SECONDS", 0.0)

    shell_module._print_welcome_info("Pythinker CLI", [])

    output = buffer.getvalue()
    # 1 antenna in the printed logo + (off + on) per blink + 1 final steady.
    assert output.count("●") == 1 + 2 * shell_module._ANTENNA_BLINKS + 1
    # The animation hides and restores the cursor around the rewrites.
    assert "\x1b[?25l" in output
    assert "\x1b[?25h" in output


def test_welcome_banner_skips_blink_when_motion_disabled(monkeypatch):
    console, buffer = _terminal_console()
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")
    monkeypatch.setattr(shell_module, "ascii_glyphs_enabled", lambda: False)
    monkeypatch.setattr(shell_module, "motion_disabled", lambda: True)

    shell_module._print_welcome_info("Pythinker CLI", [])

    assert buffer.getvalue().count("●") == 1


def test_welcome_banner_skips_blink_on_non_terminal(monkeypatch):
    console = Console(record=True, width=100, color_system=None)
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")
    monkeypatch.setattr(shell_module, "ascii_glyphs_enabled", lambda: False)
    monkeypatch.setattr(shell_module, "motion_disabled", lambda: False)

    shell_module._print_welcome_info("Pythinker CLI", [])

    assert console.export_text().count("●") == 1


def test_welcome_banner_skips_blink_when_terminal_too_short(monkeypatch):
    """If the panel is taller than the screen the antenna row may have
    scrolled off; cursor-relative repaints would land on the wrong line."""
    console, buffer = _terminal_console(height=3)
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")
    monkeypatch.setattr(shell_module, "ascii_glyphs_enabled", lambda: False)
    monkeypatch.setattr(shell_module, "motion_disabled", lambda: False)

    shell_module._print_welcome_info("Pythinker CLI", [])

    assert buffer.getvalue().count("●") == 1


def test_welcome_tiny_width_does_not_crash(monkeypatch):
    from pythinker_code.ui.shell import WelcomeInfoItem
    from pythinker_code.ui.shell.components.render_utils import cell_width

    console = Console(record=True, width=30, color_system=None)
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")

    items = [
        WelcomeInfoItem(name="Directory", value="/very/long/path/that/never/ends/project"),
        WelcomeInfoItem(name="Tip", value="Type /help for commands."),
    ]
    shell_module._print_welcome_info("Pythinker Code", items)

    lines = [ln.rstrip() for ln in console.export_text().splitlines() if ln.strip()]
    assert lines
    assert all(cell_width(ln) <= 30 for ln in lines)


def test_welcome_auto_save_path_is_middle_truncated_not_wrapped(monkeypatch):
    from pythinker_code.ui.shell import WelcomeInfoItem

    console = Console(record=True, width=120, color_system=None)
    monkeypatch.setattr(shell_module, "console", console)
    monkeypatch.setattr(shell_module, "get_version", lambda: "9.9.9")

    items = [
        WelcomeInfoItem(
            name="Auto-save",
            value=(
                "~/.pythinker/sessions/91ce869d5afa3e6547c32cb5b58fa943/"
                "6b76c556-cae9-47e2-8233-38ecf986624e/context.json"
            ),
        )
    ]
    shell_module._print_welcome_info("Pythinker Code", items)

    lines = [ln for ln in console.export_text().splitlines() if "Auto-save" in ln]
    assert len(lines) == 1
    assert "…" in lines[0]
    assert "context.json" in lines[0]
