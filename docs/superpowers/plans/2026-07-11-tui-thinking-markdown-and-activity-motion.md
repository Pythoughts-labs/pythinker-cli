# TUI Thinking Markdown and Activity Motion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render the live thinking preview as clean Markdown while reserving coral shimmer for the
single bottom verb-spinner/status line.

**Architecture:** Keep the change at two existing display boundaries. The thinking-stream path
removes only complete top-level HTML comment blocks and then reuses `render_agent_body`; the
activity tree renders all detail with its stable muted style while the existing working indicator
remains the sole verb shimmer. Scheduler, wire, subagent-state, and persisted-content paths stay
unchanged.

**Tech Stack:** Python 3.14, Rich renderables, Pythinker's Markdown/report renderer, pytest, Ruff,
Pyright, ty, uv, Make.

## Global Constraints

- Use `uv` for direct Python commands and repository `make` targets for package gates.
- Add no dependency, telemetry, endpoint, configuration key, or persistence migration.
- Do not change `show_thinking_stream` defaults, its six-line preview limit, composing previews,
  subagent scheduling, activity state, or event semantics.
- Hide complete top-level HTML comments, preserve fenced literal examples, and preserve malformed
  or incomplete streaming input as readable text.
- Keep activity-tree lifecycle markers and their running pulse unchanged.
- Keep `activity_status_line` / `_todo_activity_line` as the only coral verb shimmer, including
  reduced-motion and no-color fallbacks.
- Write each regression before its production change and observe the intended failure.
- Every production line must trace directly to one of the two approved rendering defects.
- Before completion, use `pythinker-guard`, `clean-code-guard`, `test-guard`, `docs-guard`, and
  `superpowers:verification-before-completion`.
- Add a non-blank `CHANGELOG.md` `## Unreleased` bullet before any shipped-code PR.
- Never add Codex co-author or generated-by trailers to commits or PR text.

## Root-Cause Evidence

- `_ContentBlock._compose_thinking_stream` constructs `Text(preview, ...)`, bypassing Markdown and
  exposing emphasis markers.
- A local probe shows `render_agent_body` handles emphasis but prints HTML comments literally. The
  [official Rich Markdown reference](https://github.com/Textualize/rich/blob/master/docs/source/reference/markdown.md)
  documents `rich.markdown.Markdown` as the Markdown renderable but promises no HTML-comment
  suppression, so this unsupported construct needs a narrow filter.
- `render_activity_tree` calls `shimmer_text` for each `running` detail, directly causing one tree
  row to look specially animated.
- `_LiveView._working_indicator` already routes the bottom active-subagent label through
  `activity_status_line`; existing tests pin its coral shimmer.

## File Map

- Modify `src/pythinker_code/ui/shell/visualize/_blocks.py`: clean and Markdown-render the bounded
  thinking preview.
- Modify `tests/ui_and_conv/test_streaming_content_block.py`: cover complete Markdown, hidden
  comments, malformed input, and fenced literal examples at `_ContentBlock.compose()`.
- Modify `src/pythinker_code/ui/shell/visualize/_activity_tree.py`: remove shimmer from row details.
- Modify `tests/ui_and_conv/test_activity_tree.py`: assert stable muted running detail.
- Modify `CHANGELOG.md`: add one user-facing Unreleased bullet.
- Modify `tasks/todo.md`: track execution and record exact final evidence.
- Modify `tasks/lessons.md` only if execution produces another concrete surprise or correction.

---

### Task 1: Render the bounded thinking preview as clean Markdown

**Files:**
- Modify: `tests/ui_and_conv/test_streaming_content_block.py:381-405,535-690`
- Modify: `src/pythinker_code/ui/shell/visualize/_blocks.py:1-75,1119-1135`

**Interfaces:**
- Consumes: `render_agent_body(text: str, *, theme: ThemeName | None = None) -> RenderableType`,
  `iter_fence_aware_lines(markup: str, *, keepends: bool = True, strict_close: bool = False)`, and
  `_ContentBlock._build_preview(text: str, *, max_lines: int, reserve_caret: bool = False) -> str`.
- Produces: private `_render_thinking_preview(preview: str) -> RenderableType | None`; no public API.

- [ ] **Step 1: Reconfirm current Rich behavior**

Use Context7 library `/textualize/rich` and official Rich Markdown documentation, then run:

```bash
uv run python - <<'PY'
from rich.console import Console
from pythinker_code.ui.shell.components.report import render_agent_body

for sample in ("**Planning**", "<!-- hidden -->"):
    console = Console(record=True, width=80, color_system=None)
    console.print(render_agent_body(sample))
    print(repr(console.export_text()))
PY
```

Expected baseline: emphasis delimiters disappear, while `<!-- hidden -->` remains literal. If the
installed dependency differs, stop and revise the boundary design before coding.

- [ ] **Step 2: Add failing thinking-preview tests**

Replace the existing literal-marker assertion and add these tests beside it:

```python
def test_thinking_stream_preview_renders_complete_markdown():
    block = _ContentBlock(is_think=True, show_thinking_stream=True)
    block.append("**Preparing report generation**")
    console = Console(record=True, width=120, color_system=None)
    console.print(block.compose())
    output = console.export_text()

    assert "Thinking" in output
    assert "\n\n⏺ Preparing report generation" in output
    assert "**Preparing report generation**" not in output


def test_thinking_stream_preview_hides_complete_top_level_html_comments():
    block = _ContentBlock(is_think=True, show_thinking_stream=True)
    block.append("Visible before.\n\n<!-- internal separator -->\n\nVisible after.")
    console = Console(record=True, width=120, color_system=None)
    console.print(block.compose())
    output = console.export_text()

    assert "Visible before." in output
    assert "Visible after." in output
    assert "internal separator" not in output
    assert "<!--" not in output
    assert "-->" not in output


def test_thinking_stream_preview_preserves_incomplete_markup():
    block = _ContentBlock(is_think=True, show_thinking_stream=True)
    block.append("**Planning agent\n\n<!-- incomplete")
    console = Console(record=True, width=120, color_system=None)
    console.print(block.compose())
    output = console.export_text()

    assert "**Planning agent" in output
    assert "<!-- incomplete" in output


def test_thinking_stream_preview_preserves_comment_example_in_fenced_code():
    block = _ContentBlock(is_think=True, show_thinking_stream=True)
    block.append("```markdown\n<!-- literal example -->\n```")
    console = Console(record=True, width=120, color_system=None)
    console.print(block.compose())

    assert "<!-- literal example -->" in console.export_text()
```

- [ ] **Step 3: Run the regression seam and observe RED**

```bash
uv run pytest \
  tests/ui_and_conv/test_streaming_content_block.py::test_thinking_stream_preview_renders_complete_markdown \
  tests/ui_and_conv/test_streaming_content_block.py::test_thinking_stream_preview_hides_complete_top_level_html_comments \
  tests/ui_and_conv/test_streaming_content_block.py::test_thinking_stream_preview_preserves_incomplete_markup \
  tests/ui_and_conv/test_streaming_content_block.py::test_thinking_stream_preview_preserves_comment_example_in_fenced_code \
  -q
```

Expected: the first two fail because the live path prints `**` and `<!-- ... -->`; the malformed
and fenced characterization tests pass. If a target regression passes before code changes, correct
the seam rather than accepting an unproven test.

- [ ] **Step 4: Add the minimal fence-aware preview boundary**

Import `iter_fence_aware_lines` and add this private helper near the preview helpers:

```python
from pythinker_code.ui.shell.markdown.fences import iter_fence_aware_lines

_COMPLETE_HTML_COMMENT_BLOCK_RE = re.compile(
    r"(?ms)^[ \t]*<!--.*?-->[ \t]*(?=\r?$)"
)


def _render_thinking_preview(preview: str) -> RenderableType | None:
    segments: list[str] = []
    unfenced: list[str] = []

    def flush_unfenced() -> None:
        if not unfenced:
            return
        segments.append(_COMPLETE_HTML_COMMENT_BLOCK_RE.sub("", "".join(unfenced)))
        unfenced.clear()

    for line, inside_fence in iter_fence_aware_lines(preview):
        if inside_fence:
            flush_unfenced()
            segments.append(line)
        else:
            unfenced.append(line)
    flush_unfenced()

    cleaned = "".join(segments)
    if not cleaned.strip():
        return None
    return render_agent_body(cleaned)
```

Replace the plain `Text(preview, style=preview_style)` body in `_compose_thinking_stream` with:

```python
        rendered_preview = _render_thinking_preview(preview)
        if rendered_preview is None:
            return spinner
        preview_style = tui_rich_style("thinking_text") + Style(italic=True)
        return Group(
            spinner,
            BLANK_ROW,
            BulletColumns(
                rendered_preview,
                bullet=Text(TRANSCRIPT_ASSISTANT_MARKER, style=preview_style),
            ),
        )
```

Do not alter `_compose_composing`, `compose_final`, `_THINKING_PREVIEW_LINES`, or stored content.

- [ ] **Step 5: Run the focused module and observe GREEN**

```bash
uv run pytest tests/ui_and_conv/test_streaming_content_block.py -q
```

Expected: all tests pass. Composing-preview tests must still expect literal streaming markers; only
the opt-in thinking-stream preview changes.

- [ ] **Step 6: Commit Task 1**

After `pythinker-guard` confirms surgical scope:

```bash
git add src/pythinker_code/ui/shell/visualize/_blocks.py \
  tests/ui_and_conv/test_streaming_content_block.py
git commit -m "fix(tui): render thinking preview markdown"
```

---

### Task 2: Reserve coral shimmer for the verb spinner

**Files:**
- Modify: `tests/ui_and_conv/test_activity_tree.py:1-75`
- Modify: `src/pythinker_code/ui/shell/visualize/_activity_tree.py:1-65`
- Verify unchanged: `src/pythinker_code/ui/shell/visualize/_live_view.py:968-1080`
- Verify unchanged: `src/pythinker_code/ui/shell/motion.py:250-420`

**Interfaces:**
- Consumes: `shell_style(ShellTone.MUTED) -> Style`, `_row_marker(state, now) -> Text`, and existing
  bottom working-indicator shimmer paths.
- Produces: unchanged `render_activity_tree(...) -> RenderableType`; only detail styling changes.

- [ ] **Step 1: Add a failing static-detail regression**

Add the required Rich, design-system, and theme imports, then add:

```python
def _detail_style(renderable: RenderableType, detail: str) -> Style:
    assert isinstance(renderable, Group)
    row = renderable.renderables[0]
    assert isinstance(row, Text)
    console = Console(color_system="truecolor")
    return row.get_style_at_offset(console, row.plain.index(detail))


def test_running_activity_detail_is_static_muted_text(monkeypatch):
    for flag in (
        "NO_COLOR",
        "PYTHINKER_REDUCED_MOTION",
        "PYTHINKER_NO_ANIMATION",
        "PYTHINKER_STATIC_OUTPUT",
    ):
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    rows = [ActivityRow(label="agent", detail="Shell uv run pytest", state="running")]

    first = _detail_style(render_activity_tree(rows, width=80, now=0.88), "Shell")
    later = _detail_style(render_activity_tree(rows, width=80, now=1.18), "Shell")

    assert first.color == shell_style(ShellTone.MUTED).color
    assert later.color == shell_style(ShellTone.MUTED).color
    assert first.color != tui_rich_style("activity_verb").color
    assert later.color != tui_rich_style("activity_verb").color
```

- [ ] **Step 2: Run it and observe RED**

```bash
uv run pytest \
  tests/ui_and_conv/test_activity_tree.py::test_running_activity_detail_is_static_muted_text \
  -q
```

Expected: FAIL because the running detail is composed from shimmer spans. If `NO_COLOR` remains in
the fixture, fix the fixture before interpreting the result.

- [ ] **Step 3: Remove shimmer from tree details only**

Remove `shimmer_text` from `_activity_tree.py` imports and replace the conditional detail block with:

```python
        detail = truncate_to_width(row.detail, available)
        text.append(detail, style=shell_style(ShellTone.MUTED))
```

Keep `_row_marker`, marker pulse, branches, truncation, hidden-row accounting, and states unchanged.

- [ ] **Step 4: Run tree and verb-spinner tests and observe GREEN**

```bash
uv run pytest \
  tests/ui_and_conv/test_activity_tree.py \
  tests/ui_and_conv/test_live_view_todos.py::test_todo_activity_line_uses_standard_spinner_shimmer_for_generic_verbs \
  tests/ui_and_conv/test_shell_motion.py::test_activity_status_line_uses_platinum_spinner_and_champagne_verb \
  tests/ui_and_conv/test_shell_motion_shimmer.py \
  -q
```

Expected: all pass. Do not change `_working_indicator`, `activity_status_line`, `shimmer_text`, or
palette tokens to make this gate pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/pythinker_code/ui/shell/visualize/_activity_tree.py \
  tests/ui_and_conv/test_activity_tree.py
git commit -m "fix(tui): keep activity rows visually stable"
```

---

### Task 3: Document the correction and run focused quality reviews

**Files:**
- Modify: `CHANGELOG.md:15-40`
- Modify: `tasks/todo.md` TUI task section
- Review: all Task 1 and Task 2 files

**Interfaces:**
- Consumes: completed Task 1 and Task 2 behavior.
- Produces: one Unreleased bullet and current task tracking; no runtime API.

- [ ] **Step 1: Add the required Unreleased entry**

Insert at the top of `## Unreleased`:

```markdown
- **Thinking and subagent activity now render cleanly in the terminal.** Live reasoning previews
  render complete Markdown without exposing top-level HTML comments, activity-tree rows remain
  visually stable, and the coral shimmer is reserved for the active verb spinner.
```

- [ ] **Step 2: Update task tracking truthfully**

Check only implementation items proven by focused tests. Leave full-gate and final-review items
unchecked until Task 4 produces terminal evidence.

- [ ] **Step 3: Run reactive quality skills**

Use `clean-code-guard` on production, `test-guard` on tests, `docs-guard` on changed prose, and
`pythinker-guard` on the complete diff. Fix only findings tracing to the approved behavior; record
unrelated findings under Out of scope in `tasks/todo.md`.

- [ ] **Step 4: Run the complete focused TUI set**

```bash
uv run pytest \
  tests/ui_and_conv/test_streaming_content_block.py \
  tests/ui_and_conv/test_activity_tree.py \
  tests/ui_and_conv/test_live_view_todos.py \
  tests/ui_and_conv/test_shell_motion.py \
  tests/ui_and_conv/test_shell_motion_shimmer.py \
  tests/ui_and_conv/test_terminal_capabilities.py \
  -q
git diff --check
```

Expected: all selected tests pass and `git diff --check` emits no output.

- [ ] **Step 5: Commit Task 3**

```bash
git add CHANGELOG.md tasks/todo.md tasks/lessons.md \
  docs/superpowers/specs/2026-07-11-tui-thinking-markdown-and-activity-motion-design.md \
  docs/superpowers/plans/2026-07-11-tui-thinking-markdown-and-activity-motion.md
git commit -m "docs(tui): record clean activity rendering"
```

If `docs/superpowers/` is ignored, use `git add -f` only for the two explicitly named reviewed
files, never the directory broadly.

---

### Task 4: Run release-grade verification and record the outcome

**Files:**
- Modify: `tasks/todo.md` Review subsection only after all commands finish
- Review: complete branch diff from implementation base `a20bfda0`

**Interfaces:**
- Consumes: Tasks 1-3 commits.
- Produces: verified completion evidence; no runtime API.

- [ ] **Step 1: Run the full static gate**

```bash
make check-pythinker-code
```

Expected: Ruff says `All checks passed!`, formatting reports files already formatted, Pyright says
`0 errors`, ty passes, and the command exits 0. A green Ruff line alone is insufficient.

- [ ] **Step 2: Run package and E2E tests**

```bash
make test-pythinker-code
```

Expected: package and separate `tests_e2e` summaries both pass. Any real failure blocks completion
and must be diagnosed; dependency warnings, known skips, and expected failures are not hidden.

- [ ] **Step 3: Verify exact branch scope**

```bash
git log a20bfda0..HEAD --oneline
git diff a20bfda0...HEAD --stat
git diff a20bfda0...HEAD -- \
  src/pythinker_code/ui/shell/visualize/_blocks.py \
  src/pythinker_code/ui/shell/visualize/_activity_tree.py \
  tests/ui_and_conv/test_streaming_content_block.py \
  tests/ui_and_conv/test_activity_tree.py \
  CHANGELOG.md tasks/todo.md tasks/lessons.md
git status --short
```

Expected: only planned files and commits appear, with no unexplained working-tree change.

- [ ] **Step 4: Run final verification and diff review**

Use `superpowers:verification-before-completion` with fresh outputs. Review C01-C15, especially
silent exceptions, alternate render paths, unbounded parsing, and style-blind tests. Confirm auth,
approval, persistence, providers, tool execution, and scheduling are untouched.

Expected: PASS with no Critical or Important finding. Any unresolved finding keeps the task active.

- [ ] **Step 5: Add the final task Review subsection**

Add `#### Review: TUI thinking Markdown and activity motion` under the active task. Record:

- The exact resulting behavior for complete Markdown, complete top-level comments, malformed input,
  fenced literal examples, stable tree rows, and verb-spinner shimmer.
- The two confirmed root causes: plain `Text` in the thinking preview and `shimmer_text` in running
  activity-tree details.
- The literal focused pytest count, full package count, separate E2E count, static-gate verdict, and
  `git diff --check` verdict copied from fresh terminal output.
- The verdict from each named quality review, every approved deviation, and any remaining blocker.

Do not write a count or PASS claim from memory. Missing evidence leaves the corresponding checkbox
open and prevents completion.

- [ ] **Step 6: Commit the verified review record**

```bash
git add tasks/todo.md
git commit -m "docs(tasks): record TUI rendering verification"
```

Run `pythinker-guard` again first and confirm `git status --short` contains only the review update.
