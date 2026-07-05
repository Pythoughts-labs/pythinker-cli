from typing import Any, cast

from pythinker_code.ui.shell.focus_model import FocusTuiModel
from pythinker_code.ui.shell.focus_surface import FocusTuiSurface


def test_focus_surface_renders_composer_at_bottom() -> None:
    model = FocusTuiModel()
    model.begin_turn("fix")
    model.append_tool_row("bash-1", "Bash(pytest)", "running", expandable=True)
    surface = FocusTuiSurface(model)

    text = surface.renderer_text(width=80)

    assert "⏺ Bash(pytest)" in text
    assert "❯" in text
    assert text.rfind("❯") > text.find("⏺ Bash(pytest)")


def test_focus_surface_ctrl_f_toggles_files() -> None:
    model = FocusTuiModel()
    model.mark_file("src/a.py", "updated")
    surface = FocusTuiSurface(model)

    assert "src/a.py" not in surface.renderer_text(width=80)
    surface.toggle_files()
    assert "src/a.py" in surface.renderer_text(width=80)


def test_focus_surface_toggle_files_updates_rendered_footer_count() -> None:
    model = FocusTuiModel()
    model.mark_file("src/a.py", "updated")
    surface = FocusTuiSurface(model)

    before = surface.renderer_text(width=80)
    surface.toggle_files()
    after = surface.renderer_text(width=80)

    assert "files: 1" in before
    assert "src/a.py" not in before
    assert "files: 1" in after
    assert "src/a.py" in after


def test_focus_surface_application_is_fullscreen() -> None:
    model = FocusTuiModel()
    surface = FocusTuiSurface(model)

    app = surface.create_application()

    assert app.full_screen is True


def test_focus_surface_modal_delegate_forwards_running_keys() -> None:
    class _Delegate:
        def __init__(self) -> None:
            self.handled: list[str] = []

        def should_handle_running_prompt_key(self, key: str) -> bool:
            return key == "c-s"

        def handle_running_prompt_key(self, key: str, event: Any) -> None:
            _ = event
            self.handled.append(key)

    delegate = _Delegate()
    surface = FocusTuiSurface(FocusTuiModel(), delegate=delegate)

    assert surface.running_prompt_allows_text_input() is True
    assert surface.running_prompt_hides_input_buffer() is False
    assert surface.running_prompt_accepts_submission() is True
    assert surface.should_handle_running_prompt_key("c-f") is True
    assert surface.should_handle_running_prompt_key("c-s") is True

    surface.handle_running_prompt_key("c-s", cast(Any, object()))

    assert delegate.handled == ["c-s"]
