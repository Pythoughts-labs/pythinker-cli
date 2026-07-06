from __future__ import annotations

from typing import Protocol

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import AnyFormattedText, FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.layout import Layout

from pythinker_code.ui.shell.focus_model import FocusTuiModel


class _KeyDelegate(Protocol):
    def should_handle_running_prompt_key(self, key: str) -> bool:
        raise NotImplementedError

    def handle_running_prompt_key(self, key: str, event: KeyPressEvent) -> None:
        raise NotImplementedError


class FocusTuiSurface:
    modal_priority = 1

    def __init__(self, model: FocusTuiModel, *, delegate: _KeyDelegate | None = None) -> None:
        self.model = model
        self._delegate = delegate
        self._app: Application[str] | None = None

    def renderer_text(self, width: int) -> str:
        body = "\n".join(self.model.render_rows(width))
        footer = f"model · cwd · ctx · files: {self.model.visible_file_count()}"
        return f"{body}\n────────────────\n❯\n{footer}"

    def _render_width(self) -> int:
        if self._app is None:
            return 80
        return max(20, self._app.output.get_size().columns)

    def toggle_files(self) -> None:
        self.model.toggle_files()
        self.invalidate()

    def invalidate(self) -> None:
        if self._app is not None:
            self._app.invalidate()

    def render_running_prompt_body(self, columns: int) -> AnyFormattedText:
        return FormattedText([("", self.renderer_text(columns))])

    def running_prompt_placeholder(self) -> AnyFormattedText | None:
        return None

    def running_prompt_allows_text_input(self) -> bool:
        return True

    def running_prompt_hides_input_buffer(self) -> bool:
        return False

    def running_prompt_accepts_submission(self) -> bool:
        return True

    def should_handle_running_prompt_key(self, key: str) -> bool:
        return key == "c-f" or bool(
            self._delegate is not None and self._delegate.should_handle_running_prompt_key(key)
        )

    def handle_running_prompt_key(self, key: str, event: KeyPressEvent) -> None:
        if key == "c-f":
            self.toggle_files()
            return
        if self._delegate is not None:
            self._delegate.handle_running_prompt_key(key, event)

    def create_application(self) -> Application[str]:
        kb = KeyBindings()

        @kb.add("c-f", eager=True)
        def _toggle_files(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
            self.toggle_files()
            event.app.invalidate()

        control = FormattedTextControl(
            lambda: FormattedText([("", self.renderer_text(self._render_width()))])
        )
        app: Application[str] = Application(
            layout=Layout(HSplit([Window(content=control, wrap_lines=False)])),
            key_bindings=kb,
            full_screen=True,
        )
        self._app = app
        return app
