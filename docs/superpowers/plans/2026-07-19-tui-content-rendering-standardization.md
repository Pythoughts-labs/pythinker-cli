# TUI Content Rendering Standardization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure all user-visible TUI content renders according to an explicit semantic contract so authored Markdown never leaks as raw syntax and literal technical payloads remain exact and safe.

**Architecture:** Add one deep `render_tui_content` module that owns semantic dispatch, sanitization, reasoning presentation, and degraded fallback. Migrate lifecycle and adapter call sites to classify strings as prose, reasoning, literal, label, or error before rendering, then enforce that seam with behavior tests and a narrow static tripwire.

**Tech Stack:** Python 3.14, Rich, Pythinker's existing Markdown/report renderer, pytest, Ruff, Pyright, ty, uv, Make.

## Global Constraints

- Ordinary user-visible TUI transcript content must never leak complete Markdown formatting or container syntax.
- Literal commands, diffs, logs, JSON, source, paths, and file contents remain literal after unsafe terminal-control sanitization.
- Content semantics must remain invariant across live preview, finalization, scrollback, and replay.
- Existing report, diff, code, tool-card, theme-token, glyph, spacing, motion, and terminal-capability implementations remain authoritative.
- Add no dependency, telemetry, external service, wire event, persisted-session migration, or parallel styling system.
- Unexpected renderer failure must produce a visible sanitized degraded result and a content-free categorized log entry.
- Use `uv` or repository `make` targets for every Python command.
- Keep the implementation surgical; do not convert application chrome or structured renderables into Markdown.

---

## File Structure

- Create `src/pythinker_code/ui/shell/components/content.py`: semantic content kinds, narrow presentation options, the single rendering interface, sanitization, and degraded fallback.
- Modify `src/pythinker_code/ui/shell/components/__init__.py`: export the shared interface and types.
- Create `tests/ui_and_conv/test_tui_content_rendering.py`: shared interface contract and forced-failure coverage.
- Modify `src/pythinker_code/ui/shell/visualize/_blocks.py`: route composing/reasoning live and final content plus authored notification/progress/suggestion/status prose through the shared interface.
- Modify `tests/ui_and_conv/test_streaming_content_block.py`: lifecycle equivalence and exact raw-header regression tests.
- Modify `src/pythinker_code/ui/shell/components/messages.py`: classify assistant, reasoning, user, custom, and error content.
- Modify `src/pythinker_code/ui/shell/visualize/_transcript.py`: require an explicit content kind when the body is a string.
- Modify `src/pythinker_code/ui/shell/visualize/_approval_panel.py`: render request descriptions and brief display blocks as prose while retaining shell/diff content as literal structured output.
- Modify `src/pythinker_code/ui/shell/components/special_messages.py`: route expanded authored bodies through the shared interface.
- Modify `tests/ui_and_conv/test_tui_card_messages.py`, `tests/ui_and_conv/test_transcript_rows.py`, and `tests/ui_and_conv/test_modal_lifecycle.py`: adapter contract coverage.
- Modify `src/pythinker_code/ui/shell/visualize/_worklog.py` and generic fallback sites in `src/pythinker_code/ui/shell/visualize/_blocks.py`: make prose versus literal tool-result decisions explicit.
- Modify `tests/ui_and_conv/test_worklog_render.py`, `tests/ui_and_conv/test_tui_card_tool_renderers.py`, and `tests/ui_and_conv/test_render_hardening.py`: tool-fallback behavior and architecture tripwire.
- Modify `CHANGELOG.md`: user-visible Unreleased entry.

---

### Task 1: Establish the semantic content-rendering module

**Files:**
- Create: `src/pythinker_code/ui/shell/components/content.py`
- Modify: `src/pythinker_code/ui/shell/components/__init__.py`
- Create: `tests/ui_and_conv/test_tui_content_rendering.py`

**Interfaces:**
- Consumes: `render_agent_body(text: str, *, theme: ThemeName | None = None) -> RenderableType`, `sanitize_ansi(text: str) -> str`, and `tui_rich_style(token: str) -> Style`.
- Produces: `ContentKind`, `ContentPresentation`, and `render_tui_content(text: str, *, kind: ContentKind, presentation: ContentPresentation | None = None) -> RenderableType`.

- [ ] **Step 1: Write the failing semantic-renderer contract tests**

Create `tests/ui_and_conv/test_tui_content_rendering.py` with parameterized coverage for prose,
reasoning, literal, label, error, empty input, control-sequence sanitization, and renderer failure:

```python
from __future__ import annotations

import pytest

from tests.ui_and_conv._md_contract_helpers import render_ansi, render_plain


@pytest.mark.parametrize(
    ("markup", "visible", "forbidden"),
    [
        ("**bold**", "bold", "**"),
        ("_italic_", "italic", "_italic_"),
        ("# Heading", "Heading", "# Heading"),
        ("- first\n- second", "first", "- first"),
        ("`value`", "value", "`value`"),
        ("```python\nprint('ok')\n```", "print('ok')", "```"),
    ],
)
def test_prose_renders_complete_markdown(markup: str, visible: str, forbidden: str) -> None:
    from pythinker_code.ui.shell.components.content import ContentKind, render_tui_content

    output = render_plain(render_tui_content(markup, kind=ContentKind.PROSE))
    assert visible in output
    assert forbidden not in output


def test_reasoning_uses_markdown_and_reasoning_style() -> None:
    from pythinker_code.ui.shell.components.content import ContentKind, render_tui_content

    rendered = render_tui_content("**Planning**", kind=ContentKind.REASONING)
    assert "**" not in render_plain(rendered)
    assert "Planning" in render_plain(rendered)
    assert "Planning" in render_ansi(rendered)


@pytest.mark.parametrize("kind_name", ["LITERAL", "LABEL", "ERROR"])
def test_literal_kinds_preserve_markdown_punctuation(kind_name: str) -> None:
    from pythinker_code.ui.shell.components.content import ContentKind, render_tui_content

    text = "path/[x] **literal** `value`"
    assert text in render_plain(render_tui_content(text, kind=ContentKind[kind_name]))


def test_all_kinds_strip_terminal_controls() -> None:
    from pythinker_code.ui.shell.components.content import ContentKind, render_tui_content

    payload = "safe\x1b]0;owned\x07 text\x1b[2J"
    for kind in ContentKind:
        output = render_plain(render_tui_content(payload, kind=kind))
        assert output.strip() == "safe text"
        assert "\x1b" not in output


def test_empty_input_returns_empty_visible_output() -> None:
    from pythinker_code.ui.shell.components.content import ContentKind, render_tui_content

    assert render_plain(render_tui_content("", kind=ContentKind.PROSE)) == "\n"


def test_markdown_failure_is_visible_sanitized_and_logged(monkeypatch) -> None:
    from pythinker_code.ui.shell.components import content

    logged: list[tuple[str, tuple[object, ...]]] = []

    class CapturingLogger:
        def opt(self, **_kwargs):
            return self

        def warning(self, message: str, *args: object) -> None:
            logged.append((message, args))

    def fail(_text: str):
        raise ValueError("parser failed")

    monkeypatch.setattr(content, "render_agent_body", fail)
    monkeypatch.setattr(content, "logger", CapturingLogger())
    payload = "**still visible**\x1b[2J"
    output = render_plain(content.render_tui_content(payload, kind=content.ContentKind.PROSE))
    assert "**still visible**" in output
    assert "\x1b" not in output
    assert logged == [
        ("tui_content_render_degraded kind={} length={}", ("prose", 17))
    ]
    assert all(payload not in message for message, _args in logged)
```

- [ ] **Step 2: Run the new test module and verify the missing-module failure**

Run:

```bash
uv run pytest tests/ui_and_conv/test_tui_content_rendering.py -q
```

Expected: FAIL during collection with `ModuleNotFoundError` for
`pythinker_code.ui.shell.components.content`.

- [ ] **Step 3: Implement the semantic rendering interface**

Create `src/pythinker_code/ui/shell/components/content.py` with this interface and behavior:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from loguru import logger
from rich.console import RenderableType
from rich.style import Style
from rich.styled import Styled
from rich.text import Text

from pythinker_code.ui.shell.components.render_utils import sanitize_ansi
from pythinker_code.ui.shell.components.report import render_agent_body
from pythinker_code.ui.theme import tui_rich_style


class ContentKind(StrEnum):
    PROSE = "prose"
    REASONING = "reasoning"
    LITERAL = "literal"
    LABEL = "label"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ContentPresentation:
    style_token: str | None = None
    italic: bool = False


def _literal_style(kind: ContentKind, presentation: ContentPresentation) -> Style:
    token = presentation.style_token
    if token is None:
        token = "error" if kind is ContentKind.ERROR else None
    style = tui_rich_style(token) if token is not None else Style()
    return style + Style(italic=presentation.italic)


def render_tui_content(
    text: str,
    *,
    kind: ContentKind,
    presentation: ContentPresentation | None = None,
) -> RenderableType:
    presentation = presentation or ContentPresentation()
    safe_text = sanitize_ansi(text)
    if not safe_text:
        return Text("")
    if kind in {ContentKind.LITERAL, ContentKind.LABEL, ContentKind.ERROR}:
        return Text(safe_text, style=_literal_style(kind, presentation))
    try:
        rendered = render_agent_body(safe_text)
    except Exception:  # noqa: BLE001 - TUI degradation must remain visible
        logger.opt(exception=True).warning(
            "tui_content_render_degraded kind={} length={}",
            kind.value,
            len(safe_text),
        )
        fallback_style = tui_rich_style("thinking_text") if kind is ContentKind.REASONING else Style()
        return Text(safe_text, style=fallback_style + Style(italic=kind is ContentKind.REASONING))
    if kind is ContentKind.REASONING:
        token = presentation.style_token or "thinking_text"
        reasoning_style = tui_rich_style(token) + Style(italic=presentation.italic)
        return Styled(rendered, reasoning_style)
    return rendered
```

The `Styled` wrapper supplies reasoning's muted italic default while nested Markdown elements keep
their own semantic styles. Do not stringify or flatten the renderable.

- [ ] **Step 4: Export the new interface**

Update `components/__init__.py`:

```python
from pythinker_code.ui.shell.components.content import (
    ContentKind,
    ContentPresentation,
    render_tui_content,
)
```

Add all three names to `__all__` in alphabetical order.

- [ ] **Step 5: Run the focused contract tests and static checks**

Run:

```bash
uv run pytest tests/ui_and_conv/test_tui_content_rendering.py -q
uv run ruff check src/pythinker_code/ui/shell/components/content.py tests/ui_and_conv/test_tui_content_rendering.py
uv run pyright src/pythinker_code/ui/shell/components/content.py
```

Expected: all tests pass; Ruff reports `All checks passed!`; Pyright reports zero errors.

- [ ] **Step 6: Commit the shared module**

```bash
git add src/pythinker_code/ui/shell/components/content.py \
  src/pythinker_code/ui/shell/components/__init__.py \
  tests/ui_and_conv/test_tui_content_rendering.py
git commit -m "feat(tui): add semantic content renderer"
```

---

### Task 2: Make streaming, finalization, and scrollback semantically invariant

**Files:**
- Modify: `src/pythinker_code/ui/shell/visualize/_blocks.py:568-590, 808-850, 974-997, 1120-1193`
- Modify: `tests/ui_and_conv/test_streaming_content_block.py:350-443, 623-790`

**Interfaces:**
- Consumes: `ContentKind`, `ContentPresentation`, and `render_tui_content` from Task 1.
- Produces: `_ContentBlock` live/final renderables whose Markdown semantics do not change on `TOOL_START`, `THINK_TO_TEXT`, `TEXT_TO_THINK`, or `TURN_END`.

- [ ] **Step 1: Add the exact regression and lifecycle contract tests**

Add tests that compare live and final reasoning output using the reported header:

```python
@pytest.mark.parametrize(
    "reason",
    [FlushReason.TOOL_START, FlushReason.THINK_TO_TEXT, FlushReason.TURN_END],
)
def test_complete_reasoning_markdown_never_leaks_when_finalized(reason: FlushReason) -> None:
    block = _ContentBlock(is_think=True, show_thinking_stream=True)
    block.append("**Clarifying AGENTS.md file location**")

    live = _plain(block.compose())
    block.prepare_for_finalize(reason)
    final = _plain(block.compose_final())

    for output in (live, final):
        assert "Clarifying AGENTS.md file location" in output
        assert "**Clarifying AGENTS.md file location**" not in output
```

Add corresponding complete-markup tests for composing content finalized at tool start and turn end.
Retain an incomplete-delimiter test proving that `**Planning agent` is readable during streaming;
do not require incomplete syntax to disappear before completion.

- [ ] **Step 2: Run the exact regression and observe final scrollback fail**

Run:

```bash
uv run pytest tests/ui_and_conv/test_streaming_content_block.py \
  -k 'complete_reasoning_markdown_never_leaks_when_finalized' -q
```

Expected: FAIL because `compose_final()` currently returns `Text(remaining, ...)` and preserves
the literal `**` delimiters.

- [ ] **Step 3: Route every `_ContentBlock` authored-content path through the shared interface**

Replace the independent prose/reasoning choices with small private adapters:

```python
def _render_reasoning(self, text: str) -> RenderableType:
    return render_tui_content(
        text,
        kind=ContentKind.REASONING,
        presentation=ContentPresentation(style_token="thinking_text", italic=True),
    )

def _render_prose(self, text: str) -> RenderableType:
    return render_tui_content(text, kind=ContentKind.PROSE)
```

Use `_render_reasoning(remaining)` in `compose_final()` instead of `Text(remaining, ...)`. Route
`_render_thinking_preview`, `_flush_committed`, `_render_body`, and final prose through the same
semantic adapter. Preserve:

- report detection and suppression;
- `_has_printed_bullet` state;
- preview caching keys;
- six-line reasoning preview;
- streaming caret and paced reveal behavior;
- existing `BulletColumns`, spacing, and transcript marker styles.

Remove comments that claim final reasoning intentionally bypasses Markdown.

- [ ] **Step 4: Run streaming and transition coverage**

Run:

```bash
uv run pytest tests/ui_and_conv/test_streaming_content_block.py \
  tests/ui_and_conv/test_empty_think_part_indicator.py \
  tests/ui_and_conv/test_tui_blocks_integration.py -q
```

Expected: all tests pass. Inspect failures for intentional raw-preview assertions; update only
assertions contradicted by the approved rendering contract, while retaining incomplete-stream and
literal-code expectations.

- [ ] **Step 5: Run motion and scrollback regressions**

Run:

```bash
uv run pytest tests/ui_and_conv/test_stream_pacing.py \
  tests/ui_and_conv/test_redraw_throttle.py \
  tests/ui_and_conv/test_visualize_running_prompt.py -q
```

Expected: all tests pass with no changed handoff ordering, flicker contract, or pacing behavior.

- [ ] **Step 6: Commit lifecycle standardization**

```bash
git add src/pythinker_code/ui/shell/visualize/_blocks.py \
  tests/ui_and_conv/test_streaming_content_block.py
git commit -m "fix(tui): preserve markdown across transcript lifecycle"
```

---

### Task 3: Migrate message, transcript, replay, and modal adapters

**Files:**
- Modify: `src/pythinker_code/ui/shell/components/messages.py`
- Modify: `src/pythinker_code/ui/shell/components/special_messages.py`
- Modify: `src/pythinker_code/ui/shell/visualize/_transcript.py`
- Modify: `src/pythinker_code/ui/shell/visualize/_approval_panel.py`
- Modify: `src/pythinker_code/ui/shell/visualize/_blocks.py:1849-2095`
- Modify: `tests/ui_and_conv/test_tui_card_messages.py`
- Modify: `tests/ui_and_conv/test_transcript_rows.py`
- Modify: `tests/ui_and_conv/test_modal_lifecycle.py`
- Modify: `tests/ui_and_conv/test_replay.py`

**Interfaces:**
- Consumes: the semantic renderer from Task 1 and lifecycle behavior from Task 2.
- Produces: explicit semantic classification for every generic message, transcript string, replayed content string, approval description/brief, and authored status block.

- [ ] **Step 1: Add adapter-level failing tests**

Add focused assertions:

```python
def test_visible_assistant_thinking_renders_markdown() -> None:
    rendered = render_assistant_message(
        [AssistantContent(kind="thinking", text="**Inspecting state**")]
    )
    output = render_plain(rendered, width=60)
    assert "Inspecting state" in output
    assert "**Inspecting state**" not in output


def test_custom_message_body_renders_markdown_even_with_custom_text_style() -> None:
    output = render_plain(
        render_custom_message(CustomMessageInput(custom_type="notice", text="**Ready**")),
        width=60,
    )
    assert "Ready" in output
    assert "**Ready**" not in output


def test_transcript_string_requires_and_honors_semantic_kind() -> None:
    output = _plain(
        render_transcript_row("assistant", "**Ready**", content_kind=ContentKind.PROSE)
    )
    assert "Ready" in output
    assert "**Ready**" not in output


def test_transcript_literal_body_preserves_markdown_punctuation() -> None:
    text = "git show path/[x] --format=**raw**"
    output = _plain(render_transcript_row("tool", text, content_kind=ContentKind.LITERAL))
    assert text in output
```

Add modal tests proving approval descriptions and `BriefDisplayBlock.text` render Markdown, while
`ShellDisplayBlock.command` and diff content preserve punctuation literally. Add a replay test that
replays assistant/reasoning Markdown and asserts no complete delimiters leak.

- [ ] **Step 2: Run the adapter tests and observe the raw-marker failures**

Run:

```bash
uv run pytest tests/ui_and_conv/test_tui_card_messages.py \
  tests/ui_and_conv/test_transcript_rows.py \
  tests/ui_and_conv/test_modal_lifecycle.py \
  tests/ui_and_conv/test_replay.py -q
```

Expected: new reasoning, custom-message, transcript, approval, or replay assertions fail because
those paths currently construct plain `Text` directly.

- [ ] **Step 3: Migrate message and special-message bodies**

In `messages.py`:

- use `PROSE` for user text, assistant `kind="text"`, and custom message bodies;
- use `REASONING` for visible assistant `kind="thinking"`;
- use `LABEL` for hidden-thinking labels and custom-type labels;
- use `ERROR` for aborted/error messages;
- remove the branch that reconstructs `Text(message.text, ...)` merely because the Markdown
  renderable is a `Text` subclass;
- retain card backgrounds, padding, separators, and stop-reason wording.

In `special_messages.py`, use `PROSE` for expanded skill, compaction, and branch summary bodies,
and keep collapsed names/hints as `LABEL`.

- [ ] **Step 4: Make transcript string classification explicit**

Change the transcript interface to:

```python
def render_transcript_row(
    role: Role,
    content: str | RenderableType,
    *,
    content_kind: ContentKind | None = None,
    status: Status | None = None,
) -> RenderableType:
    ...
```

If `content` is a string and `content_kind` is `None`, raise `ValueError` with an actionable message
instead of guessing. If `content` is already a Rich renderable, reject a non-`None` `content_kind`
as conflicting input. Update every caller to pass the semantic kind. This makes ambiguity explicit
without altering wire payloads.

- [ ] **Step 5: Migrate modal and generic authored-status bodies**

Use `PROSE` for approval request descriptions and `BriefDisplayBlock.text`; retain
`PythinkerSyntax` for `ShellDisplayBlock.command` and existing diff renderers for diffs. Use
`LABEL` for sender, action, option labels, source identifiers, key hints, and feedback UI.

In `_blocks.py`, classify notification bodies, progress notes, suggestions, status explanations,
and compaction prose as `PROSE` only where their contract describes authored explanatory text.
Titles, severity labels, counters, and application-owned status words remain `LABEL`. Preserve
existing preview line budgets and expand behavior.

- [ ] **Step 6: Run adapter, replay, and modal suites**

Run:

```bash
uv run pytest tests/ui_and_conv/test_tui_card_messages.py \
  tests/ui_and_conv/test_transcript_rows.py \
  tests/ui_and_conv/test_modal_lifecycle.py \
  tests/ui_and_conv/test_replay.py \
  tests/ui_and_conv/test_btw.py -q
```

Expected: all tests pass; shell commands and diffs remain byte-equivalent in plain captures.

- [ ] **Step 7: Commit adapter migration**

```bash
git add src/pythinker_code/ui/shell/components/messages.py \
  src/pythinker_code/ui/shell/components/special_messages.py \
  src/pythinker_code/ui/shell/visualize/_transcript.py \
  src/pythinker_code/ui/shell/visualize/_approval_panel.py \
  src/pythinker_code/ui/shell/visualize/_blocks.py \
  tests/ui_and_conv/test_tui_card_messages.py \
  tests/ui_and_conv/test_transcript_rows.py \
  tests/ui_and_conv/test_modal_lifecycle.py \
  tests/ui_and_conv/test_replay.py
git commit -m "refactor(tui): classify visible transcript content"
```

---

### Task 4: Standardize generic tool fallbacks and add the architecture tripwire

**Files:**
- Modify: `src/pythinker_code/ui/shell/visualize/_worklog.py`
- Modify: `src/pythinker_code/ui/shell/visualize/_blocks.py:1300-1685`
- Modify: `tests/ui_and_conv/test_worklog_render.py`
- Modify: `tests/ui_and_conv/test_tui_card_tool_renderers.py`
- Modify: `tests/ui_and_conv/test_render_hardening.py`

**Interfaces:**
- Consumes: `render_tui_content` and content kinds from Task 1.
- Produces: explicit prose/literal classification for generic tool output and a static guard against new direct content-bearing `Text(...)` paths.

- [ ] **Step 1: Add tool-fallback semantic tests**

Add tests for both TUI styles:

```python
def test_authored_report_fallback_renders_markdown() -> None:
    output = _render_worklog_result(tool="Report", text="**Summary**")
    assert "Summary" in output
    assert "**Summary**" not in output


@pytest.mark.parametrize("tool", ["Shell", "ReadFile", "Grep", "FetchURL"])
def test_literal_tool_fallback_preserves_markdown_punctuation(tool: str) -> None:
    text = "path/[x] **literal** `value`"
    assert text in _render_tool_fallback(tool=tool, text=text)
```

Use existing test factories and style fixtures rather than introducing parallel fake wire types.
Cover streamed stdout and stderr separately; stderr uses `ERROR` styling but remains literal.

- [ ] **Step 2: Run fallback tests and record which existing paths misclassify content**

Run:

```bash
uv run pytest tests/ui_and_conv/test_worklog_render.py \
  tests/ui_and_conv/test_tui_card_tool_renderers.py \
  -k 'fallback or markdown_punctuation or authored_report' -q
```

Expected: at least the authored prose fallback fails by exposing Markdown or the literal fallback
fails if it is indiscriminately converted to Markdown.

- [ ] **Step 3: Centralize fallback classification without hard-coding provider lists**

Add a small internal tool-output classifier adjacent to the existing tool-style metadata. It must
classify by established tool/display contract, not by searching payload punctuation:

```python
_PROSE_RESULT_TOOLS = frozenset({"Report", "Agent", "RunAgents"})


def _fallback_content_kind(tool_name: str, *, is_error: bool, streamed: bool) -> ContentKind:
    if is_error:
        return ContentKind.ERROR
    if streamed:
        return ContentKind.LITERAL
    if tool_name in _PROSE_RESULT_TOOLS:
        return ContentKind.PROSE
    return ContentKind.LITERAL
```

Before accepting the final set, inspect every registered built-in renderer and existing worklog
style entry. Prefer structured display blocks and registered renderers over this fallback. Keep the
set restricted to tools whose contract explicitly returns authored prose; do not classify unknown
MCP tools as prose. Unknown and unregistered tool output fails safe as `LITERAL`.

Use the classifier for legacy worklog results and `_ToolCallBlock` streamed/generic fallback
children. Do not route diff, syntax, activity-tree, file-listing, or structured display renderables
through Markdown.

- [ ] **Step 4: Add a narrow AST-based architecture tripwire**

Extend `tests/ui_and_conv/test_render_hardening.py` with an AST visitor that detects direct
`Text(variable)` calls where the variable name is one of the known content-bearing names:

```python
_CONTENT_VARIABLE_NAMES = {
    "body",
    "content",
    "description",
    "message_text",
    "preview",
    "remaining",
    "response",
    "summary",
}


def test_content_bearing_strings_use_semantic_renderer() -> None:
    violations: list[str] = []
    for path in _shell_render_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not _is_direct_text_call_for_content_name(node, _CONTENT_VARIABLE_NAMES):
                continue
            key = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
            if key not in _DIRECT_TEXT_ALLOWLIST:
                violations.append(key)
    assert not violations, "content bypassed render_tui_content:\n" + "\n".join(violations)
```

Implement `_is_direct_text_call_for_content_name` against `ast.Call`, `ast.Name`, and
`ast.Attribute`; include keyword arguments such as `Text(text=message.body)`. Keep an exact
path-and-line allowlist only for verified literal/specialized implementation internals, with an
inline semantic explanation for each entry. Do not exempt an entire file or every `Text` call.

- [ ] **Step 5: Run the tripwire and review every allowlist entry**

Run:

```bash
uv run pytest tests/ui_and_conv/test_render_hardening.py -q
```

Expected: PASS. Read each allowlisted source line and confirm it is application chrome,
already-sanitized literal content, or a structured renderer. Rename ambiguous local variables to
semantic names where that makes the classification self-evident; do not broaden the allowlist to
silence a real authored-content bypass.

- [ ] **Step 6: Run both TUI style suites**

Run:

```bash
PYTHINKER_TUI_STYLE=card uv run pytest tests/ui_and_conv/test_tui_card_tool_renderers.py -q
PYTHINKER_TUI_STYLE=pythinker uv run pytest tests/ui_and_conv/test_worklog_render.py \
  tests/ui_and_conv/test_tui_blocks_integration.py -q
```

Expected: all tests pass. Literal output remains exact, authored fallback prose has no raw complete
Markdown markers, and unknown tools remain literal.

- [ ] **Step 7: Commit fallback standardization and enforcement**

```bash
git add src/pythinker_code/ui/shell/visualize/_worklog.py \
  src/pythinker_code/ui/shell/visualize/_blocks.py \
  tests/ui_and_conv/test_worklog_render.py \
  tests/ui_and_conv/test_tui_card_tool_renderers.py \
  tests/ui_and_conv/test_render_hardening.py
git commit -m "refactor(tui): enforce semantic rendering fallbacks"
```

---

### Task 5: Changelog, compatibility audit, and full verification

**Files:**
- Modify: `CHANGELOG.md`
- Verify only: `docs/en/release-notes/changelog.md` through its documented generator if preparing a PR

**Interfaces:**
- Consumes: completed behavior from Tasks 1-4.
- Produces: user-facing release note and evidence that the full Pythinker package gates pass.

- [ ] **Step 1: Add the required Unreleased changelog entry**

Add under `## Unreleased` in `CHANGELOG.md`:

```markdown
- Standardize shell TUI content rendering so Markdown remains formatted across live, finalized,
  scrollback, and replay views while commands, diffs, logs, JSON, and source output remain literal.
```

Do not edit `docs/en/release-notes/changelog.md` manually.

- [ ] **Step 2: Run the complete focused TUI verification set**

Run:

```bash
uv run pytest \
  tests/ui_and_conv/test_tui_content_rendering.py \
  tests/ui_and_conv/test_streaming_content_block.py \
  tests/ui_and_conv/test_empty_think_part_indicator.py \
  tests/ui_and_conv/test_tui_card_messages.py \
  tests/ui_and_conv/test_transcript_rows.py \
  tests/ui_and_conv/test_modal_lifecycle.py \
  tests/ui_and_conv/test_replay.py \
  tests/ui_and_conv/test_worklog_render.py \
  tests/ui_and_conv/test_tui_card_tool_renderers.py \
  tests/ui_and_conv/test_render_hardening.py \
  tests/ui_and_conv/test_stream_pacing.py \
  tests/ui_and_conv/test_redraw_throttle.py \
  tests/ui_and_conv/test_visualize_running_prompt.py -q
```

Expected: all selected tests pass with no warnings introduced by changed code.

- [ ] **Step 3: Verify terminal capability and Markdown compatibility**

Run:

```bash
uv run pytest tests/ui/test_shell_markdown.py \
  tests/ui/test_console_theme.py \
  tests/ui_and_conv/test_md_normalization_matrix.py \
  tests/ui_and_conv/test_md_wrapping_contract.py \
  tests/ui_and_conv/test_code_theme_opt_in.py \
  tests/ui_and_conv/test_spacing_primitives.py -q
```

Expected: all tests pass in the existing dark/light, width, no-color, and formatting contracts.

- [ ] **Step 4: Run the full package quality gate**

Run:

```bash
make check-pythinker-code
make test-pythinker-code
```

Expected: check target prints its successful Ruff format/check, Pyright, and ty summaries;
test target reports all `tests` and `tests_e2e` passing. If a required system tool is missing,
record the exact unavailable command and error rather than claiming success.

- [ ] **Step 5: Inspect the complete diff and architecture surface**

Run:

```bash
git diff --check
git diff --stat HEAD~4..HEAD
git diff HEAD~4..HEAD -- src/pythinker_code/ui/shell tests/ui_and_conv CHANGELOG.md
git status --short
```

Expected: no whitespace errors; only the planned TUI, tests, and changelog files changed; no direct
authored-content `Text(...)` bypass or generated changelog edit appears.

- [ ] **Step 6: Commit the changelog and any verification-only corrections**

```bash
git add CHANGELOG.md
git commit -m "docs: note standardized TUI content rendering"
```

Do not fold unrelated formatter churn or pre-existing worktree changes into this commit.

- [ ] **Step 7: Request final code review before integration**

Use `superpowers:requesting-code-review` against the complete implementation diff. Resolve every
confirmed correctness, security, compatibility, or maintainability finding immediately, then rerun
the smallest affected focused tests and both full package gates before claiming completion.
