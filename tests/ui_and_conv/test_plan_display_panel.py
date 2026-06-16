from rich.console import Console
from rich.panel import Panel

from pythinker_code.ui.shell.visualize import _live_view
from pythinker_code.ui.theme import tui_rich_style
from pythinker_code.wire.types import PlanDisplay, StatusUpdate


def test_plan_display_uses_worklog_plan_title(monkeypatch):
    printed = []
    card_call = {}

    def fake_render_worklog_card(title, body, *, subtitle=None, border_style="grey50"):
        panel = Panel(body, title=title, subtitle=subtitle, border_style=border_style)
        card_call.update(
            {
                "title": title,
                "body": body,
                "subtitle": subtitle,
                "border_style": border_style,
                "panel": panel,
            }
        )
        return panel

    monkeypatch.setattr(_live_view, "render_worklog_card", fake_render_worklog_card)
    monkeypatch.setattr(_live_view.console, "print", lambda *args, **kwargs: printed.extend(args))
    view = _live_view._LiveView(StatusUpdate())

    view.display_plan(PlanDisplay(content="# Steps\n\n- Step one", file_path="plans/one.md"))

    assert card_call["title"] == "Plan"
    assert card_call["subtitle"] == "plans/one.md"
    assert card_call["border_style"] == tui_rich_style("border")
    # _print_action_block commits the panel with a trailing blank row; only the
    # panel is captured because the zero-arg console.print() calls have no args.
    assert printed == [card_call["panel"]]

    console = Console(record=True, width=120, color_system=None)
    console.print(card_call["body"])
    output = console.export_text()

    assert "Step one" in output
