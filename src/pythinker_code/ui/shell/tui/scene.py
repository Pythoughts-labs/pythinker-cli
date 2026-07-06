from __future__ import annotations

from dataclasses import dataclass

from pythinker_code.ui.shell.tui.width import pad_line


@dataclass(frozen=True)
class RunningPromptScene:
    body: str
    top_border: str
    prompt_symbol: str
    placeholder: str = ""

    def render(self, width: int) -> list[str]:
        lines = [pad_line(line, width) for line in self.body.splitlines()]
        lines.append(pad_line(self.top_border, width))
        prompt_line = f"  {self.prompt_symbol} {self.placeholder}".rstrip()
        lines.append(pad_line(prompt_line, width))
        return lines
