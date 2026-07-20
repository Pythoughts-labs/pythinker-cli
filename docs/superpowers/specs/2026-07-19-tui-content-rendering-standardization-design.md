# TUI content rendering standardization design

**Status:** Approved by the user on 2026-07-19

## Problem

The shell TUI does not have one content-rendering contract. Individual live, finalized,
scrollback, replay, message, panel, and tool paths independently choose between Rich `Text`, the
Pythinker Markdown renderer, and specialized renderers. The same model-authored content can
therefore render correctly while live and expose raw Markdown delimiters after a lifecycle
transition. For example, a live thinking header renders `**Heading**` as bold text, but starting a
tool finalizes that reasoning through plain `Text` and prints the literal `**` markers.

The absence of a shared semantic classification also makes a blanket Markdown conversion unsafe.
Commands, diffs, logs, JSON, source code, file contents, paths, application labels, and error text
have literal-display requirements that differ from authored prose.

## Goals

- Ensure ordinary user-visible TUI transcript content never leaks raw Markdown container or
  formatting syntax.
- Preserve authored Markdown semantics consistently across live preview, finalization, scrollback,
  and replay.
- Preserve literal technical payloads exactly inside explicit code, diff, log, or output
  presentations without displaying their surrounding Markdown fence markers.
- Centralize semantic content classification, ANSI/control-sequence sanitization, fallback
  behavior, and presentation selection behind one small interface.
- Preserve reports, tables, fenced code, links, lists, inline code, Unicode width, terminal
  capability behavior, streaming motion, and scrollback ownership.
- Prevent new rendering paths from bypassing the shared contract accidentally.

## Non-goals

- Changing wire events, persisted session formats, model prompts, provider output, or whether
  reasoning is emitted or stored.
- Changing prompt input editing, shell command execution, web/dashboard rendering, or ACP wire
  semantics.
- Treating every string as Markdown. Application chrome and literal technical payloads remain
  literal by explicit semantic classification.
- Replacing specialized report, diff, syntax-highlighted code, tool-card, or activity renderers.
- Introducing a new dependency, styling system, theme token family, or parser.

## Rendering contract

Every content-bearing TUI path must classify its input before rendering it. The classification is
semantic rather than lifecycle-specific:

| Content kind | Meaning | Rendering behavior |
| --- | --- | --- |
| `PROSE` | Model-, user-, extension-, or tool-authored explanatory text | Sanitize and render through the existing agent-body/Pythinker Markdown pipeline. |
| `REASONING` | Model reasoning that is configured to be visible | Use the same Markdown semantics as prose with the established muted reasoning presentation. |
| `LITERAL` | Commands, source, diffs, logs, JSON, file content, and raw tool streams | Sanitize and preserve content exactly in an explicit literal/code/output presentation. |
| `LABEL` | Application-owned titles, status words, counters, paths, key hints, and identifiers | Sanitize and render as plain text with caller-selected theme styling. |
| `ERROR` | User-visible failure text that must not interpret payload markup | Sanitize and render literally with the established error presentation. |

The application owns the classification. Untrusted content cannot select its own kind. Callers
must not infer the kind from whether the string happens to contain Markdown punctuation.

The primary interface will be a pure TUI rendering function under `ui/shell/components/`. It will
accept the string, semantic kind, and a narrow presentation descriptor and return a Rich
`RenderableType`. The presentation descriptor may select existing theme styles or literal
presentation variants, but it must not allow callers to substitute an arbitrary parser or bypass
sanitization.

This forms a deep module: callers learn one interface while the implementation owns Markdown
normalization, report promotion, control-sequence sanitization, reasoning styling, literal
preservation, empty-input behavior, and safe fallback.

## Architecture and ownership

The shared content-rendering module owns:

- sanitizing ANSI and unsafe terminal control sequences before interpretation;
- dispatching by semantic content kind;
- routing `PROSE` and `REASONING` through `render_agent_body` and the existing Pythinker Markdown
  pipeline;
- routing `LITERAL`, `LABEL`, and `ERROR` through safe literal renderers;
- applying the established theme tokens without introducing raw colors;
- producing a safe degraded renderable if authored Markdown rendering fails;
- emitting categorized diagnostics without logging the input content.

Specialized modules retain ownership of their established semantics:

- report parsing and presentation remain in `components/report.py`;
- Markdown normalization and element rendering remain in `ui/shell/markdown/`;
- diff layout remains in `components/diff.py` and file-diff tool renderers;
- activity labels, glyphs, spacing, and motion remain in their existing modules;
- tool renderers retain structured argument/result interpretation.

These modules call or sit behind the shared interface as appropriate; the design does not wrap
already-structured Rich renderables in Markdown again.

## Data flow and lifecycle invariance

The normal data flow is:

```text
wire/model/tool content
        -> caller assigns semantic content kind
        -> shared content-rendering interface sanitizes and dispatches
        -> existing Markdown/report or literal/specialized implementation
        -> Rich renderable
        -> live preview / final scrollback / replay adapter
```

Lifecycle state must not change content semantics. A `REASONING` fragment remains `REASONING`
when it moves from the live six-line preview to final scrollback. A `PROSE` fragment remains
`PROSE` when a tool starts, a think-to-text transition occurs, or the turn ends. Live paths may
temporarily limit rows or defer incomplete constructs, but finalization must render the same
authored content with the same Markdown semantics.

The renderable need not be the same object across phases. Its visible text and semantic styling
must be equivalent except for intentional lifecycle chrome such as a spinner, caret, elapsed-time
label, preview truncation, or transcript bullet.

## Streaming and incomplete Markdown

Streaming paths must not flash raw structured payloads or crash when a construct is incomplete.
They continue to use the established commit-boundary and fence-aware buffering rules.

- Complete Markdown inside the visible preview renders normally.
- An incomplete inline delimiter may remain temporarily literal until enough input arrives to
  interpret it safely; once complete, the next render removes the delimiter syntax.
- Open ordinary code fences hide their fence marker and use the existing bounded code-preview
  behavior.
- Open `report` fences must never reveal report JSON before validation and promotion.
- Complete top-level HTML comments remain hidden outside fenced code; comment examples inside
  literal code remain visible.
- Finalized complete Markdown must not use the temporary plain streaming fallback.

## Literal-content behavior

Literal content preserves its meaningful characters, whitespace, line structure, and ordering
after unsafe terminal controls are removed. Markdown punctuation inside a command, path, diff,
log, JSON document, or source file is data and is not interpreted.

When literal content originates inside a Markdown fence, the fence is a container instruction and
is not displayed. The code/output renderer displays only the body, with the established language
label or tool context when available. Existing truncation must remain explicit through an expand
hint or omitted-line count; it must never masquerade as complete output.

## Error and degraded behavior

- Empty input returns an empty renderable and does not manufacture spacing or success output.
- Malformed or incomplete authored Markdown must remain readable and cannot escape the live render
  loop as an exception.
- If the Markdown/report implementation raises unexpectedly, the shared module returns a visibly
  degraded, sanitized literal presentation. It must not return an empty renderable for non-empty
  input or present the fallback as successfully formatted Markdown.
- The failure is logged with a stable category, semantic kind, content length, and rendering phase.
  Logs must not contain the content, credentials, tool arguments, raw output, or a user-visible
  stack trace.
- ANSI, OSC, APC, Rich markup, and other terminal-control input cannot become active terminal
  control through either the primary or fallback path.
- Rendering remains local and deterministic. It adds no retry, network call, telemetry, or
  background lifecycle.

## Migration scope

The first implementation migrates every model- or extension-authored transcript seam and the
generic fallbacks capable of receiving such content:

- `_ContentBlock` live composing, live reasoning, final reasoning, final assistant prose, and
  transition flushes;
- assistant, reasoning, user, and custom-message renderers;
- transcript and session replay helpers;
- progress notes, suggestions, notifications, compaction/status prose, and question/approval
  explanatory prose;
- card-style and legacy-worklog generic result fallbacks when the tool contract identifies prose;
- existing literal fallbacks where migration is required to make their classification explicit or
  to guarantee sanitization.

Structured labels, spinners, glyphs, counters, timestamps, paths, identifiers, diff rows, and
syntax-highlighted payload bodies do not become Markdown. They either remain in their specialized
renderer or use `LABEL`/`LITERAL` explicitly.

The migration must remove obsolete direct `Text(content)` decisions rather than layering the new
module in front of and behind old policy branches.

## Enforcement

A focused static architecture test will scan the shell TUI modules for direct construction of
plain Rich `Text` from known content-bearing fields and variables. A narrow allowlist will cover
application chrome, already-sanitized literal implementations, and specialized renderers whose
interface guarantees literal data.

The static test is a tripwire, not the primary correctness proof. Runtime contract tests exercise
the shared interface and every lifecycle adapter. Any allowlist entry must name the semantic reason
it cannot use the shared interface; file-wide exemptions are prohibited.

## Test design

Tests are written before production changes and observed failing for the expected raw-Markdown
leak or missing interface.

### Shared interface contract

Parameterized fixtures cover:

- bold, italic, strikethrough, headings, links, lists, block quotes, tables, and inline code;
- fenced code with and without a language;
- report blocks, malformed report blocks, and open report fences;
- complete and incomplete emphasis delimiters and code fences;
- complete top-level HTML comments and comments inside fenced code;
- ANSI, OSC, APC, Rich-markup-looking text, control characters, and Unicode-width cases;
- empty, whitespace-only, multiline, and large bounded inputs;
- forced Markdown-renderer failure and the visible degraded literal result.

### Lifecycle adapter contract

The same authored fixtures run through live preview, tool-start finalization, think-to-text,
text-to-think, turn-end finalization, cancellation/abort, scrollback emission, and session replay.
Tests assert that raw formatting delimiters do not appear after the construct is complete and that
semantic styling remains equivalent across transitions.

The exact reported regression is permanent coverage: both live and finalized rendering of
`**Clarifying AGENTS.md file location**` display `Clarifying AGENTS.md file location` without
literal `**` markers.

### Literal-content contract

Commands, diffs, logs, JSON, paths, source, and file contents containing Markdown punctuation are
preserved exactly after control-sequence sanitization. Fenced literal fixtures prove that the body
remains exact while opening and closing fence markers are absent from visible output.

### Compatibility contract

Existing tests continue to cover no-color, reduced-motion, static output, ASCII/safe glyphs,
narrow/wide terminal widths, stream pacing, redraw throttling, scrollback handoff, report
suppression, tool cards, and legacy worklog style.

Focused verification runs the shared rendering, streaming content block, replay, transcript,
message, modal, tool-card, and worklog tests. Because the implementation changes shipped shell
code, completion also requires `make check-pythinker-code` and `make test-pythinker-code`.

## Documentation and compatibility

This is a presentation correction, not a wire or persistence migration. Public configuration keys
and `show_thinking_stream` semantics remain compatible. User-visible behavior changes only where
raw Markdown syntax was previously exposed or authored prose was incorrectly treated as literal.

Implementation must add a user-facing bullet under `## Unreleased` in `CHANGELOG.md`. The generated
docs changelog is updated only through the documented `npm run sync` workflow if that workflow is
part of the eventual PR preparation.

## Rollback

The change is isolated to the shared rendering module, migrated shell adapters, tests, and
changelog. It introduces no persisted state or dependency migration. Rollback is a code revert;
wire data and saved sessions remain readable because their stored content is unchanged.
