from types import SimpleNamespace
from typing import cast

import pytest
from pythinker_core.message import Message
from rich.text import Text

import pythinker_code.ui.shell as shell_module
from pythinker_code.soul import Soul
from pythinker_code.ui.shell import Shell
from pythinker_code.ui.shell.components import render_plain
from pythinker_code.ui.shell.echo import UserEcho, render_user_echo, render_user_echo_text
from pythinker_code.ui.shell.prompt import PromptMode, UserInput
from pythinker_code.ui.tui_config import get_active_tui_style, set_active_tui_style
from pythinker_code.utils.slashcmd import SlashCommand, SlashCommandCall
from pythinker_code.wire.types import AudioURLPart, ImageURLPart, TextPart, VideoURLPart


@pytest.fixture(autouse=True)
def _legacy_echo_style():
    original = get_active_tui_style()
    set_active_tui_style("pythinker")
    try:
        yield
    finally:
        set_active_tui_style(original)


def _make_user_input(command: str, *, mode: PromptMode = PromptMode.AGENT) -> UserInput:
    return UserInput(
        mode=mode,
        command=command,
        resolved_command=command,
        content=[TextPart(text=command)],
    )


def _noop(app: object, args: str) -> None:
    pass


def _make_shell(*command_names: str) -> Shell:
    commands = [
        SlashCommand(
            name=name,
            description=f"{name} command",
            func=_noop,
            aliases=[],
        )
        for name in command_names
    ]
    return Shell(cast(Soul, SimpleNamespace(available_slash_commands=commands)))


def test_echo_agent_input_prints_stringified_user_message(monkeypatch) -> None:
    printed: list[Text] = []
    monkeypatch.setattr(shell_module.console, "print", lambda text: printed.append(text))

    Shell._echo_agent_input(_make_user_input("hi"))

    assert [text.plain for text in printed] == ["❯ hi"]


def test_echo_agent_input_uses_resolved_command_for_placeholders(monkeypatch) -> None:
    printed: list[Text] = []
    monkeypatch.setattr(shell_module.console, "print", lambda text: printed.append(text))

    user_input = UserInput(
        mode=PromptMode.AGENT,
        command="[Pasted text #1 +3 lines]",
        resolved_command="line1\nline2\nline3",
        content=[TextPart(text="line1\nline2\nline3")],
    )

    Shell._echo_agent_input(user_input)

    assert [text.plain for text in printed] == ["❯ line1\nline2\nline3"]


def test_render_user_echo_preserves_literal_brackets() -> None:
    rendered = render_user_echo(Message(role="user", content=[TextPart(text="[brackets]")]))

    assert isinstance(rendered, UserEcho)
    assert rendered.plain == "❯ [brackets]"


def test_render_user_echo_preserves_image_placeholder_literal() -> None:
    rendered = render_user_echo(
        Message(
            role="user",
            content=[ImageURLPart(image_url=ImageURLPart.ImageURL(url="https://example.com/img"))],
        )
    )

    assert isinstance(rendered, UserEcho)
    assert rendered.plain == "❯ [image]"


def test_render_user_echo_preserves_audio_placeholder_literal() -> None:
    rendered = render_user_echo(
        Message(
            role="user",
            content=[
                AudioURLPart(
                    audio_url=AudioURLPart.AudioURL(url="https://example.com/audio", id="clip")
                )
            ],
        )
    )

    assert isinstance(rendered, UserEcho)
    assert rendered.plain == "❯ [audio:clip]"


def test_render_user_echo_preserves_video_placeholder_literal() -> None:
    rendered = render_user_echo(
        Message(
            role="user",
            content=[
                VideoURLPart(video_url=VideoURLPart.VideoURL(url="https://example.com/video"))
            ],
        )
    )

    assert isinstance(rendered, UserEcho)
    assert rendered.plain == "❯ [video]"


def test_render_user_echo_preserves_mixed_content_order() -> None:
    rendered = render_user_echo(
        Message(
            role="user",
            content=[
                TextPart(text="look "),
                ImageURLPart(image_url=ImageURLPart.ImageURL(url="https://example.com/img")),
                AudioURLPart(audio_url=AudioURLPart.AudioURL(url="https://example.com/audio")),
                VideoURLPart(video_url=VideoURLPart.VideoURL(url="https://example.com/video")),
            ],
        )
    )

    assert isinstance(rendered, UserEcho)
    assert rendered.plain == "❯ look [image][audio][video]"


def test_card_style_user_echo_renders_transcript_prompt_symbol() -> None:
    original = get_active_tui_style()
    try:
        set_active_tui_style("card")
        rendered = render_user_echo_text("apply")
        plain = render_plain(rendered, width=80)
    finally:
        set_active_tui_style(original)

    lines = plain.split("\n")
    assert lines[0] == ""
    # The submitted prompt sits in a full-width tinted block, so the row carries
    # surrounding padding around the marker and message.
    assert lines[1].strip() == "❯ apply"
    assert "✨" not in plain


def test_user_echo_wraps_continuation_under_text_start() -> None:
    rendered = render_user_echo_text("abcdefghij klmnopqrst uvwxyz")
    plain = render_plain(rendered, width=16)
    lines = plain.splitlines()

    assert lines[0] == ""
    assert lines[1].lstrip().startswith("❯ ")
    # Continuation wraps under the message text, not under the prompt marker,
    # and stays column-aligned with the first line inside the tinted block.
    assert not lines[2].lstrip().startswith("❯")
    assert lines[1].index("abcdefghij") == lines[2].index("klmnopqrst")


def test_user_echo_renders_pasted_markdown_tables() -> None:
    rendered = render_user_echo_text(
        "| Step | Level | Hex |\n"
        "| --- | --- | --- |\n"
        "| off | Off | #475569 |\n"
        "| high | High | #cc704b |\n"
    )
    plain = render_plain(rendered, width=72)
    lines = plain.splitlines()

    assert lines[0] == ""
    assert lines[1].lstrip().startswith("❯ ┌")
    assert "│ Step" in plain
    assert "#475569" in plain
    assert "| --- |" not in plain


def test_user_echo_leaves_trailing_blank_before_agent_stream() -> None:
    from rich.console import Console

    console = Console(record=True, width=40, color_system=None)
    console.print(render_user_echo_text("apply"))
    lines = console.export_text().splitlines()
    assert lines[-1] == ""


def test_user_echo_wraps_message_in_tinted_block() -> None:
    from rich.console import Console

    from pythinker_code.ui.theme import tui_rich_style

    expected = tui_rich_style("user_message_bg").bgcolor
    assert expected is not None

    console = Console(force_terminal=True, color_system="truecolor", width=40)
    lines = console.render_lines(render_user_echo_text("apply"), console.options)

    # The submitted message itself, not just surrounding padding, is painted on
    # the shared user_message_bg block.
    apply_segment = next(segment for line in lines for segment in line if "apply" in segment.text)
    assert apply_segment.style is not None
    assert apply_segment.style.bgcolor == expected


def test_should_echo_agent_input_for_plain_agent_message() -> None:
    shell = _make_shell()
    assert shell._should_echo_agent_input(_make_user_input("hi")) is True


def test_should_not_echo_agent_input_for_exit_commands() -> None:
    shell = _make_shell()
    assert shell._should_echo_agent_input(_make_user_input("exit")) is False
    assert shell._should_echo_agent_input(_make_user_input("/exit")) is False
    assert shell._should_echo_agent_input(_make_user_input("/quit")) is False


def test_should_echo_agent_input_for_skill_slash_commands() -> None:
    shell = _make_shell("skill:demo")
    assert shell._should_echo_agent_input(_make_user_input("/skill:demo")) is True
    assert shell._should_echo_agent_input(_make_user_input("/skill:demo fix login")) is True


def test_should_echo_unregistered_skill_like_slash_commands() -> None:
    shell = _make_shell("skill:demo")
    assert shell._should_echo_agent_input(_make_user_input("/skill:not-found")) is True


def test_should_echo_flow_slash_commands() -> None:
    shell = _make_shell("flow:demo")
    assert shell._should_echo_agent_input(_make_user_input("/flow:demo")) is True
    assert shell._should_echo_agent_input(_make_user_input("/flow:demo run it")) is True


def test_should_echo_unregistered_flow_like_slash_commands() -> None:
    shell = _make_shell("flow:demo")
    assert shell._should_echo_agent_input(_make_user_input("/flow:not-found")) is True


def test_should_not_echo_agent_input_for_non_skill_slash_commands() -> None:
    shell = _make_shell("skill:demo")
    assert shell._should_echo_agent_input(_make_user_input("/help")) is False
    assert shell._should_echo_agent_input(_make_user_input("/theme dark")) is False
    assert shell._should_echo_agent_input(_make_user_input("/clear")) is False


def test_hidden_slash_in_placeholder_is_not_treated_as_local_command() -> None:
    shell = _make_shell()
    user_input = UserInput(
        mode=PromptMode.AGENT,
        command="[Pasted text #1 +3 lines]",
        resolved_command="/quit\nnot really",
        content=[TextPart(text="/quit\nnot really")],
    )

    assert Shell._should_exit_input(user_input) is False
    assert Shell._agent_slash_command_call(user_input) is None
    assert shell._should_echo_agent_input(user_input) is True


def test_should_exit_input_is_mode_independent_for_visible_exit_commands() -> None:
    assert Shell._should_exit_input(_make_user_input("exit")) is True
    assert Shell._should_exit_input(_make_user_input("/quit")) is True
    assert Shell._should_exit_input(_make_user_input("exit", mode=PromptMode.SHELL)) is True
    assert Shell._should_exit_input(_make_user_input("/exit", mode=PromptMode.SHELL)) is True


def test_visible_slash_command_keeps_expanded_placeholder_args() -> None:
    shell = _make_shell()
    user_input = UserInput(
        mode=PromptMode.AGENT,
        command="/echo [Pasted text #1 +3 lines]",
        resolved_command="/echo line1\nline2\nline3",
        content=[TextPart(text="line1\nline2\nline3")],
    )

    assert Shell._agent_slash_command_call(user_input) == SlashCommandCall(
        name="echo",
        args="line1\nline2\nline3",
        raw_input="/echo line1\nline2\nline3",
    )
    assert shell._should_echo_agent_input(user_input) is False


def test_visible_skill_slash_command_keeps_expanded_placeholder_args_and_still_echoes() -> None:
    shell = _make_shell("skill:demo")
    user_input = UserInput(
        mode=PromptMode.AGENT,
        command="/skill:demo [Pasted text #1 +3 lines]",
        resolved_command="/skill:demo line1\nline2\nline3",
        content=[TextPart(text="line1\nline2\nline3")],
    )

    assert Shell._agent_slash_command_call(user_input) == SlashCommandCall(
        name="skill:demo",
        args="line1\nline2\nline3",
        raw_input="/skill:demo line1\nline2\nline3",
    )
    assert shell._should_echo_agent_input(user_input) is True


def test_visible_flow_slash_command_keeps_expanded_placeholder_args_and_still_echoes() -> None:
    shell = _make_shell("flow:demo")
    user_input = UserInput(
        mode=PromptMode.AGENT,
        command="/flow:demo [Pasted text #1 +3 lines]",
        resolved_command="/flow:demo line1\nline2\nline3",
        content=[TextPart(text="line1\nline2\nline3")],
    )

    assert Shell._agent_slash_command_call(user_input) == SlashCommandCall(
        name="flow:demo",
        args="line1\nline2\nline3",
        raw_input="/flow:demo line1\nline2\nline3",
    )
    assert shell._should_echo_agent_input(user_input) is True


def test_should_not_echo_non_agent_input() -> None:
    shell = _make_shell()
    assert shell._should_echo_agent_input(_make_user_input("ls", mode=PromptMode.SHELL)) is False
