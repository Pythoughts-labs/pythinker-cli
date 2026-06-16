from __future__ import annotations

from pythinker_core.message import Message
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.measure import Measurement
from rich.padding import Padding
from rich.text import Text

from pythinker_code.ui.shell.components.markdown import PythinkerMarkdown
from pythinker_code.ui.shell.prompt import PROMPT_SYMBOL_AGENT_INPUT
from pythinker_code.ui.shell.spacing import BLANK_ROW
from pythinker_code.ui.theme import tui_rich_style
from pythinker_code.utils.message import message_stringify
from pythinker_code.utils.rich.columns import BulletColumns


class UserEcho:
    """Transcript-shaped user input with aligned wrapped continuation rows."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.plain = f"{PROMPT_SYMBOL_AGENT_INPUT} {text}"
        body = BulletColumns(
            PythinkerMarkdown(text), bullet=Text(PROMPT_SYMBOL_AGENT_INPUT), padding=1
        )
        # Echo the submitted prompt inside a full-width tinted band using the
        # shared ``user_message_bg`` palette token, so the user's message reads
        # as a distinct block in the transcript. ``Padding`` expands to the full
        # width and ``tui_rich_style`` yields an empty style on no-color
        # terminals, so this degrades cleanly.
        self._block = Padding(body, (0, 1, 0, 1), style=tui_rich_style("user_message_bg"))

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement.get(console, options, self._block)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        # Leading blank separates from prior scrollback; trailing blank gives one
        # row of breathing room before the agent stream starts in the Live area.
        yield from console.render(Group(BLANK_ROW, self._block, BLANK_ROW), options)


def render_user_echo(message: Message) -> RenderableType:
    """Render a user message as transcript output.

    User input stays transcript-shaped in both styles: the submitted buffer
    is echoed back with the same prompt marker shown in the live input row.
    """
    text = message_stringify(message)
    return UserEcho(text)


def render_user_echo_text(text: str) -> RenderableType:
    """Render submitted local prompt text in the transcript."""
    return UserEcho(text)
