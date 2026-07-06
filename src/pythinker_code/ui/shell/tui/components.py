from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from pythinker_code.ui.shell.tui.width import pad_line, wrap_plain_text


class Component(Protocol):
    def render(self, width: int) -> list[str]:
        raise NotImplementedError

    def invalidate(self) -> None:
        raise NotImplementedError


def _empty_children() -> list[Component]:
    return []


@dataclass
class Text:
    value: str
    padding_x: int = 0
    padding_y: int = 0

    def invalidate(self) -> None:
        return None

    def render(self, width: int) -> list[str]:
        content_width = max(1, width - self.padding_x * 2)
        margin = " " * self.padding_x
        lines = [
            pad_line(f"{margin}{line}{margin}", width)
            for line in wrap_plain_text(self.value, content_width)
        ]
        blank = " " * width
        return [blank] * self.padding_y + lines + [blank] * self.padding_y


@dataclass
class Spacer:
    height: int = 1

    def invalidate(self) -> None:
        return None

    def render(self, width: int) -> list[str]:
        return [" " * width for _ in range(max(0, self.height))]


@dataclass
class Container:
    children: list[Component] = field(default_factory=_empty_children)

    def add(self, child: Component) -> None:
        self.children.append(child)

    def clear(self) -> None:
        self.children.clear()

    def invalidate(self) -> None:
        for child in self.children:
            child.invalidate()

    def render(self, width: int) -> list[str]:
        lines: list[str] = []
        for child in self.children:
            lines.extend(child.render(width))
        return lines


@dataclass
class Box:
    child: Component
    padding_x: int = 0
    padding_y: int = 0
    style: Callable[[str], str] | None = None

    def invalidate(self) -> None:
        self.child.invalidate()

    def render(self, width: int) -> list[str]:
        inner_width = max(1, width - self.padding_x * 2)
        blank = " " * width
        lines = [blank] * self.padding_y
        margin = " " * self.padding_x
        for line in self.child.render(inner_width):
            lines.append(pad_line(f"{margin}{line}{margin}", width))
        lines.extend([blank] * self.padding_y)
        if self.style is not None:
            return [self.style(line) for line in lines]
        return lines
