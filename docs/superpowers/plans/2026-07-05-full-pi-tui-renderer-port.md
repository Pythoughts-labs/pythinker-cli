# Full Pi TUI Renderer Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python-native port of Pi's diffed TUI renderer and wire Pythinker's running agent view through it so streaming output updates smoothly without replacing the bottom input card.

**Architecture:** Add a tiny renderer layer under `src/pythinker_code/ui/shell/tui/` with component primitives, line diff planning, and a coalesced scheduler. Then compose Pythinker's existing live-view output and prompt-card chrome into one stable scene used by the running prompt path.

**Tech Stack:** Python 3.12+, prompt_toolkit Application invalidation, Rich-rendered text already produced by Pythinker, no new dependencies.

## Global Constraints

- No new third-party dependency.
- No Node runtime or vendored TypeScript package.
- Preserve the current Pythinker visual design unless a change is required for stable streaming.
- The prompt/input card stays visible during agent runs.
- Avoid unrelated focus-mode/sidebar changes.
- Use `uv` commands under the `pythinker` conda environment for verification.
- Add a `CHANGELOG.md` `## Unreleased` bullet before opening any PR that touches shipped code.

---

## File Structure

- Create `src/pythinker_code/ui/shell/tui/__init__.py`: public exports for the small renderer package.
- Create `src/pythinker_code/ui/shell/tui/components.py`: `Component`, `Text`, `Spacer`, `Container`, `Box` primitives.
- Create `src/pythinker_code/ui/shell/tui/diff.py`: pure line-diff planner and ANSI synchronized-output wrapper.
- Create `src/pythinker_code/ui/shell/tui/scheduler.py`: coalesced render-request helper for prompt_toolkit invalidation.
- Create `src/pythinker_code/ui/shell/tui/scene.py`: running-agent scene composition from existing live-view/prompt outputs.
- Modify `src/pythinker_code/ui/shell/prompt.py`: delegate running prompt body/card rendering through the scene while keeping existing key/input behavior.
- Modify `src/pythinker_code/ui/shell/visualize/_interactive.py`: expose stable stream/body state needed by the scene and keep chrome always visible.
- Create `tests/ui_and_conv/test_tui_renderer.py`: primitive, diff, scheduler, and scene unit tests.
- Extend `tests/ui_and_conv/test_visualize_running_prompt.py`: regressions for prompt-card visibility through scene-rendered running frames.
- Extend `tests/e2e/test_shell_pty_prompt_layout_e2e.py`: PTY smoke coverage for no duplicated/missing prompt marker while streaming.
- Modify `CHANGELOG.md`: one Unreleased bullet for the renderer port.

---

### Task 1: Renderer component primitives

**Files:**
- Create: `src/pythinker_code/ui/shell/tui/__init__.py`
- Create: `src/pythinker_code/ui/shell/tui/components.py`
- Test: `tests/ui_and_conv/test_tui_renderer.py`

**Interfaces:**
- Produces: `Component.render(width: int) -> list[str]`, `Component.invalidate() -> None`, `Text`, `Spacer`, `Container`, `Box`.
- Later tasks consume these classes for scene composition.

- [ ] **Step 1: Write failing component tests**

Add to `tests/ui_and_conv/test_tui_renderer.py`:

```python
from pythinker_code.ui.shell.tui import Box, Container, Spacer, Text


def test_text_wraps_and_pads_to_width() -> None:
    text = Text("hello world", padding_x=1)

    assert text.render(8) == [" hello  ", " world "]


def test_spacer_renders_blank_lines() -> None:
    assert Spacer(2).render(5) == ["     ", "     "]


def test_container_concatenates_children() -> None:
    root = Container([Text("one"), Spacer(1), Text("two")])

    assert root.render(6) == ["one   ", "      ", "two   "]


def test_box_applies_padding_and_background_function() -> None:
    box = Box(Text("run"), padding_x=1, padding_y=1, style=lambda value: f"<{value}>")

    assert box.render(7) == ["<       >", "< run   >", "<       >"]
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
source ~/miniforge3/etc/profile.d/conda.sh && conda activate pythinker && uv run pytest tests/ui_and_conv/test_tui_renderer.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'pythinker_code.ui.shell.tui'`.

- [ ] **Step 3: Implement minimal primitives**

Create `src/pythinker_code/ui/shell/tui/components.py`:

```python
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from pythinker_code.ui.shell.tui.width import pad_line, wrap_plain_text


class Component(Protocol):
    def render(self, width: int) -> list[str]: ...

    def invalidate(self) -> None: ...


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
        lines = [pad_line(f"{margin}{line}{margin}", width) for line in wrap_plain_text(self.value, content_width)]
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
    children: list[Component] = field(default_factory=list)

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
```

Create `src/pythinker_code/ui/shell/tui/width.py`:

```python
from __future__ import annotations

import textwrap


def pad_line(value: str, width: int) -> str:
    return value[:width].ljust(width)


def wrap_plain_text(value: str, width: int) -> list[str]:
    if not value:
        return [""]
    wrapped: list[str] = []
    for raw_line in value.splitlines() or [value]:
        wrapped.extend(textwrap.wrap(raw_line, width=width, replace_whitespace=False) or [""])
    return wrapped
```

Create `src/pythinker_code/ui/shell/tui/__init__.py`:

```python
from pythinker_code.ui.shell.tui.components import Box, Component, Container, Spacer, Text

__all__ = ["Box", "Component", "Container", "Spacer", "Text"]
```

- [ ] **Step 4: Run component tests**

Run:

```bash
source ~/miniforge3/etc/profile.d/conda.sh && conda activate pythinker && uv run pytest tests/ui_and_conv/test_tui_renderer.py -q
```

Expected: PASS for four component tests.

- [ ] **Step 5: Commit**

```bash
git add src/pythinker_code/ui/shell/tui tests/ui_and_conv/test_tui_renderer.py
git commit -m "feat(tui): add renderer primitives"
```

---

### Task 2: Line diff renderer and synchronized output wrapper

**Files:**
- Create: `src/pythinker_code/ui/shell/tui/diff.py`
- Modify: `src/pythinker_code/ui/shell/tui/__init__.py`
- Test: `tests/ui_and_conv/test_tui_renderer.py`

**Interfaces:**
- Produces: `LinePatch(start: int, delete: int, insert: tuple[str, ...])`, `plan_line_diff(old: Sequence[str], new: Sequence[str]) -> list[LinePatch]`, `synchronized_output(payload: str) -> str`.
- Later tasks use the planner to prove only changed regions are emitted.

- [ ] **Step 1: Add failing diff tests**

Append:

```python
from pythinker_code.ui.shell.tui import LinePatch, plan_line_diff, synchronized_output


def test_plan_line_diff_replaces_changed_middle_run() -> None:
    old = ["top", "old", "same"]
    new = ["top", "new", "same"]

    assert plan_line_diff(old, new) == [LinePatch(start=1, delete=1, insert=("new",))]


def test_plan_line_diff_handles_growth_and_shrink() -> None:
    assert plan_line_diff(["a"], ["a", "b"]) == [LinePatch(start=1, delete=0, insert=("b",))]
    assert plan_line_diff(["a", "b"], ["a"]) == [LinePatch(start=1, delete=1, insert=())]


def test_synchronized_output_wraps_payload() -> None:
    assert synchronized_output("abc") == "\x1b[?2026habc\x1b[?2026l"
```

- [ ] **Step 2: Run tests to verify failure**

Run same `uv run pytest tests/ui_and_conv/test_tui_renderer.py -q`.

Expected: FAIL importing `LinePatch`.

- [ ] **Step 3: Implement diff planner**

Create `src/pythinker_code/ui/shell/tui/diff.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from collections.abc import Sequence

SYNC_OUTPUT_START = "\x1b[?2026h"
SYNC_OUTPUT_END = "\x1b[?2026l"


@dataclass(frozen=True)
class LinePatch:
    start: int
    delete: int
    insert: tuple[str, ...]


def plan_line_diff(old: Sequence[str], new: Sequence[str]) -> list[LinePatch]:
    patches: list[LinePatch] = []
    matcher = SequenceMatcher(a=list(old), b=list(new), autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        patches.append(
            LinePatch(
                start=old_start,
                delete=old_end - old_start,
                insert=tuple(new[new_start:new_end]),
            )
        )
    return patches


def synchronized_output(payload: str) -> str:
    if not payload:
        return payload
    return f"{SYNC_OUTPUT_START}{payload}{SYNC_OUTPUT_END}"
```

Update `src/pythinker_code/ui/shell/tui/__init__.py`:

```python
from pythinker_code.ui.shell.tui.components import Box, Component, Container, Spacer, Text
from pythinker_code.ui.shell.tui.diff import LinePatch, plan_line_diff, synchronized_output

__all__ = [
    "Box",
    "Component",
    "Container",
    "LinePatch",
    "Spacer",
    "Text",
    "plan_line_diff",
    "synchronized_output",
]
```

- [ ] **Step 4: Run diff tests**

Run:

```bash
source ~/miniforge3/etc/profile.d/conda.sh && conda activate pythinker && uv run pytest tests/ui_and_conv/test_tui_renderer.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pythinker_code/ui/shell/tui tests/ui_and_conv/test_tui_renderer.py
git commit -m "feat(tui): add line diff planner"
```

---

### Task 3: Coalesced render scheduler

**Files:**
- Create: `src/pythinker_code/ui/shell/tui/scheduler.py`
- Modify: `src/pythinker_code/ui/shell/tui/__init__.py`
- Test: `tests/ui_and_conv/test_tui_renderer.py`

**Interfaces:**
- Produces: `RenderScheduler(request_invalidate: Callable[[], None], min_interval_seconds: float = 1 / 30)` with `request_render(now: float | None = None) -> bool`.
- Later tasks use it to coalesce token-stream invalidations.

- [ ] **Step 1: Add failing scheduler tests**

Append:

```python
from pythinker_code.ui.shell.tui import RenderScheduler


def test_render_scheduler_coalesces_fast_requests() -> None:
    calls: list[str] = []
    scheduler = RenderScheduler(lambda: calls.append("invalidate"), min_interval_seconds=0.1)

    assert scheduler.request_render(now=1.0) is True
    assert scheduler.request_render(now=1.05) is False
    assert scheduler.request_render(now=1.11) is True
    assert calls == ["invalidate", "invalidate"]
```

- [ ] **Step 2: Run tests to verify failure**

Run `uv run pytest tests/ui_and_conv/test_tui_renderer.py -q`.

Expected: FAIL importing `RenderScheduler`.

- [ ] **Step 3: Implement scheduler**

Create `src/pythinker_code/ui/shell/tui/scheduler.py`:

```python
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class RenderScheduler:
    request_invalidate: Callable[[], None]
    min_interval_seconds: float = 1 / 30
    _last_render_request: float | None = None

    def request_render(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        if (
            self._last_render_request is not None
            and current - self._last_render_request < self.min_interval_seconds
        ):
            return False
        self._last_render_request = current
        self.request_invalidate()
        return True
```

Update `__init__.py` to export `RenderScheduler`.

- [ ] **Step 4: Run scheduler tests**

Run `uv run pytest tests/ui_and_conv/test_tui_renderer.py -q`.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pythinker_code/ui/shell/tui tests/ui_and_conv/test_tui_renderer.py
git commit -m "feat(tui): coalesce render invalidations"
```

---

### Task 4: Running agent scene composition

**Files:**
- Create: `src/pythinker_code/ui/shell/tui/scene.py`
- Modify: `src/pythinker_code/ui/shell/tui/__init__.py`
- Test: `tests/ui_and_conv/test_tui_renderer.py`

**Interfaces:**
- Produces: `RunningPromptScene(body: str, top_border: str, prompt_symbol: str, placeholder: str = "")` with `render(width: int) -> list[str]`.
- Later prompt integration consumes `RunningPromptScene` for one stable scene.

- [ ] **Step 1: Add failing scene tests**

Append:

```python
from pythinker_code.ui.shell.tui import RunningPromptScene


def test_running_prompt_scene_keeps_input_card_after_stream_body() -> None:
    scene = RunningPromptScene(body="streaming\ntext", top_border="──────── ● off", prompt_symbol="❯")

    assert scene.render(16) == [
        "streaming        ",
        "text             ",
        "──────── ● off   ",
        "  ❯             ",
    ]


def test_running_prompt_scene_keeps_card_when_body_empty() -> None:
    scene = RunningPromptScene(body="", top_border="──────── ● off", prompt_symbol="❯")

    assert scene.render(16) == ["──────── ● off   ", "  ❯             "]
```

- [ ] **Step 2: Run tests to verify failure**

Run `uv run pytest tests/ui_and_conv/test_tui_renderer.py -q`.

Expected: FAIL importing `RunningPromptScene`.

- [ ] **Step 3: Implement scene**

Create `src/pythinker_code/ui/shell/tui/scene.py`:

```python
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
        lines: list[str] = []
        for line in self.body.splitlines():
            if line:
                lines.append(pad_line(line, width))
        lines.append(pad_line(self.top_border, width))
        prompt_line = f"  {self.prompt_symbol} {self.placeholder}".rstrip()
        lines.append(pad_line(prompt_line, width))
        return lines
```

Update `__init__.py` to export `RunningPromptScene`.

- [ ] **Step 4: Run scene tests**

Run `uv run pytest tests/ui_and_conv/test_tui_renderer.py -q`.

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pythinker_code/ui/shell/tui tests/ui_and_conv/test_tui_renderer.py
git commit -m "feat(tui): compose running prompt scene"
```

---

### Task 5: Prompt integration for scene-rendered running frames

**Files:**
- Modify: `src/pythinker_code/ui/shell/prompt.py`
- Modify: `src/pythinker_code/ui/shell/visualize/_interactive.py`
- Test: `tests/ui_and_conv/test_visualize_running_prompt.py`

**Interfaces:**
- Consumes: `RunningPromptScene.render(width: int) -> list[str]`.
- Produces: running prompt frames where body/status and input-card chrome are one stable scene.

- [ ] **Step 1: Add failing integration test**

Add to `tests/ui_and_conv/test_visualize_running_prompt.py` near the existing running prompt card tests:

```python
def test_render_agent_prompt_message_uses_scene_order_for_stream_and_input_card(monkeypatch) -> None:
    from types import SimpleNamespace

    from prompt_toolkit.formatted_text import FormattedText

    import pythinker_code.ui.shell.prompt as prompt_module
    from pythinker_code.ui.shell.prompt import PROMPT_SYMBOL_AGENT_INPUT

    border = "──────── ● off"
    session = _card_session(delegate=_body_delegate("assistant chunk"))
    session._shortcut_help_open = False
    monkeypatch.setattr(session, "_render_agent_status", lambda _c: FormattedText())
    monkeypatch.setattr(session, "_render_interactive_body", lambda _c: FormattedText())
    monkeypatch.setattr(session, "_render_pinned_status_tail", lambda _c: FormattedText())
    monkeypatch.setattr(session, "_render_input_top_border", lambda _c, _f: [("", border)])
    monkeypatch.setattr(prompt_module, "is_card_style", lambda: True)
    monkeypatch.setattr(prompt_module, "get_toolbar_colors", lambda: SimpleNamespace(separator=""))

    frame = "".join(text for _style, text, *_ in session._render_agent_prompt_message())

    assert frame == f"assistant chunk\n{border}\n  {PROMPT_SYMBOL_AGENT_INPUT} "
```

If `_body_delegate` does not exist in the test file, add this helper near `_hiding_delegate`:

```python
def _body_delegate(body: str):
    class _Delegate:
        def render_running_prompt_body(self, columns: int) -> str:
            return body

        def running_prompt_placeholder(self) -> None:
            return None

        def running_prompt_allows_text_input(self) -> bool:
            return False

        def running_prompt_hides_input_buffer(self) -> bool:
            return True

        def running_prompt_accepts_submission(self) -> bool:
            return False

        def should_handle_running_prompt_key(self, key: str) -> bool:
            return False

        def handle_running_prompt_key(self, key: str, event) -> None:  # noqa: ANN001
            raise AssertionError("not expected")

    return _Delegate()
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
source ~/miniforge3/etc/profile.d/conda.sh && conda activate pythinker && uv run pytest tests/ui_and_conv/test_visualize_running_prompt.py::test_render_agent_prompt_message_uses_scene_order_for_stream_and_input_card -q
```

Expected: FAIL until prompt rendering uses the scene path.

- [ ] **Step 3: Implement prompt scene bridge**

In `src/pythinker_code/ui/shell/prompt.py`, import:

```python
from pythinker_code.ui.shell.tui import RunningPromptScene
```

In `_render_agent_prompt_message`, replace only the running-prompt/card-style body assembly branch with:

```python
body_text = fragment_list_to_text(self._render_running_prompt_body(width))
top_border = fragment_list_to_text(self._render_input_top_border(width, focused=False)).rstrip("\n")
scene = RunningPromptScene(
    body=body_text,
    top_border=top_border,
    prompt_symbol=PROMPT_SYMBOL_AGENT_INPUT,
    placeholder=fragment_list_to_text(self._running_prompt_placeholder() or FormattedText()).strip(),
)
for index, line in enumerate(scene.render(width)):
    if index:
        fragments.append(("", "\n"))
    fragments.append(("", line.rstrip()))
```

Use the existing helper names if they differ. Do not change keyboard handling, modal routing, approval behavior, or prompt buffer mutation.

In `_interactive.py`, keep:

```python
def running_prompt_hide_input_card_chrome(self) -> bool:
    # NEVER hide this chrome: the prompt card is a stable, always-mounted surface
    # so users always see the input area during agent runs.
    return False
```

- [ ] **Step 4: Run integration tests**

Run:

```bash
source ~/miniforge3/etc/profile.d/conda.sh && conda activate pythinker && uv run pytest tests/ui_and_conv/test_visualize_running_prompt.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pythinker_code/ui/shell/prompt.py src/pythinker_code/ui/shell/visualize/_interactive.py tests/ui_and_conv/test_visualize_running_prompt.py
git commit -m "feat(tui): render running prompt as stable scene"
```

---

### Task 6: Changelog and PTY regression

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `tests/e2e/test_shell_pty_prompt_layout_e2e.py`

**Interfaces:**
- Consumes: scene-rendered running prompt behavior from Task 5.
- Produces: user-facing release note and PTY coverage.

- [ ] **Step 1: Add changelog bullet**

Under `## Unreleased` in `CHANGELOG.md`, add:

```markdown
- Ported the running agent TUI toward Pi's stable diff-rendered scene model so streamed output keeps the input card visible without prompt jumps.
```

- [ ] **Step 2: Add PTY assertion**

In `tests/e2e/test_shell_pty_prompt_layout_e2e.py`, extend the existing running prompt layout test to assert the captured frame contains exactly one prompt marker row while streaming:

```python
assert output.count("❯") == 1
assert "────────" in output
```

Use the test file's existing captured output variable name.

- [ ] **Step 3: Run PTY test**

Run:

```bash
source ~/miniforge3/etc/profile.d/conda.sh && conda activate pythinker && uv run pytest tests/e2e/test_shell_pty_prompt_layout_e2e.py -q
```

Expected: PASS or documented skip if PTY support is unavailable.

- [ ] **Step 4: Run focused suite and package check**

Run:

```bash
source ~/miniforge3/etc/profile.d/conda.sh && conda activate pythinker && uv run pytest tests/ui_and_conv/test_tui_renderer.py tests/ui_and_conv/test_visualize_running_prompt.py tests/e2e/test_shell_pty_prompt_layout_e2e.py -q
source ~/miniforge3/etc/profile.d/conda.sh && conda activate pythinker && make check-pythinker-code
```

Expected: all tests pass and `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add CHANGELOG.md tests/e2e/test_shell_pty_prompt_layout_e2e.py
git commit -m "test(tui): cover stable streaming prompt scene"
```

---

## Self-Review

- Spec coverage: component tree, diff planner, synchronized output wrapper, coalesced invalidation, scene composition, prompt-card visibility, PTY regression, and changelog are covered.
- Placeholder scan: no `TBD`, `TODO`, or unspecified implementation steps remain.
- Type consistency: exported names in `__init__.py` match the task interfaces: `Text`, `Spacer`, `Container`, `Box`, `LinePatch`, `plan_line_diff`, `synchronized_output`, `RenderScheduler`, `RunningPromptScene`.
- Intentional simplification: the first pass ports Pi's renderer model into Pythinker as a Python-native scene and diff layer, but only wires the running agent prompt path. Other shell surfaces can move to the renderer later if this path proves stable.
