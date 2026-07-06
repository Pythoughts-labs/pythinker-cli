# Documentation Agent Guide

This repository uses VitePress for the documentation site. Most pages now contain real prose; keep existing structure and remove any remaining `Reference Code` scaffolding only when replacing it with finished content.

## Structure

- All pages live under `docs/en/`. The site is English only.
- Main sections (nav + sidebar) are:
  - Guides: getting-started, use-cases, interaction, sessions, ides, integrations
  - Customization: mcp, plugins, hooks, skills, agents, print-mode, wire-mode
  - Configuration: config-files, providers, overrides, env-vars, data-locations
  - Reference: pythinker-command, pythinker-info, pythinker-acp, pythinker-mcp, pythinker-term, pythinker-dashboard, pythinker-web, pythinker-benchmark, slash-commands, keyboard
  - FAQ: faq
  - Release notes: changelog, breaking-changes
- Navigation and sidebar are defined in `docs/.vitepress/config.ts`. Any new or renamed page must be wired there.
- VitePress is configured with Mermaid support and `vitepress-plugin-llms` in `docs/.vitepress/config.ts`.

## Source of truth

- **Changelog page**: `docs/en/release-notes/changelog.md` is auto-synced from the root `CHANGELOG.md` via script. Do not edit it manually.
- **All other pages**: edit directly under `docs/en/`.

## Authoring workflow

- Expand remaining scaffolded sections into prose while preserving the page's established section ordering and sidebar labels.
- For changelog: edit the root `CHANGELOG.md`, then run `npm run sync` to update the English docs.

## Naming conventions

- Filenames are kebab-case.
- Use consistent section labels that match the sidebar titles.
- Use backticks for flags, commands, subcommands, command arguments, file paths, code identifiers, type names, field names, field values, and keyboard shortcuts.

## Wording conventions

- Do not change H1 titles or nav/sidebar labels.
- H2+ headings use sentence case (only the first word capitalized unless it is a proper noun). Treat "Wire" as a proper noun; do not treat "agent", "shell mode", or "print mode" as proper nouns.
- Use `API key`; keep `JSON`, `JSONL`, `OAuth`, `macOS`, and `uv` as-is.
- Use straight double quotes for quoted content (not curly quotes).
- Use "tool call", not "tool use".
- Use inline code for tool names (e.g., `Task`, `ReadFile`, `Shell`).

Proper nouns (capitalized as written): `Agent`, `Shell`, `Wire` (as in Wire mode), `MCP`, `ACP`, `Pythinker Code`, `Agent Skills`, `Prompt Flow`, `Ralph Loop`, `Frontmatter`.

Lowercase common terms: agent, shell, shell mode, print mode, thinking mode, skill, system prompt, prompt, session, context, subagent, API key, approval request, slash command, tool call, user message, assistant message, tool message, turn, provider, diff.

## Typography

- **Code block language**: Always specify language for fenced code blocks (e.g., ` ```sh `, ` ```toml `, ` ```json `). Exception: natural language examples (user prompts) may omit the language.
- **Callout titles**: Use short category titles for callout blocks (`::: tip`, `::: warning`, `::: info`, `::: danger`). Put the detailed description in the block content, not the title. Use no title or short words like `Note` for warning.
- **Version info blocks**: For version change callouts, use `::: info` with a category title (Added/Changed/Removed). The content should be a complete sentence.
  - Good: `::: info Changed` + content `Renamed in Wire 1.1. ...`
  - Bad: `::: info Renamed in Wire 1.1` (title too long)

## Writing style

- **Natural narrative**: Organize content like writing an article, guiding readers smoothly through the material.
- **Avoid fragmentation**: Don't turn every point into a subheading; use paragraph transitions instead.
- **Global perspective**: "Getting Started" introduces core concepts only; detailed usage belongs in later pages.
- **Progressive depth**: Guides → Customization → Configuration → Reference, information deepens gradually.
- **No "next steps"**: VitePress already provides prev/next navigation; don't add manual `::: tip Next` blocks at page end.

### Example: good vs bad

Outline prompt:

```
* Install and upgrade
  * System requirements: Python 3.12+, recommend uv
  * Install, upgrade, uninstall steps
```

**Bad** (mechanical conversion to headings):

```markdown
## Install and upgrade

### System requirements

- Python 3.12+
- Recommend uv

### Install

...

### Upgrade

...
```

**Good** (natural narrative):

```markdown
## Install and upgrade

Pythinker Code requires Python 3.12+. We recommend using uv for installation and management.

If you haven't installed uv yet, please refer to the uv installation docs first. Install Pythinker Code:

(code block)

Verify the installation:

(code block)

Upgrade to the latest version:

(code block)
```

## Build and preview

- Docs are built with VitePress from `docs/`.
- Common commands (run inside `docs/`):
  - `npm install` (or `bun install` if you use bun)
  - `npm run dev`
  - `npm run build`
  - `npm run preview`
- The build output is `docs/.vitepress/dist`.

## Changelog syncing

The English changelog (`docs/en/release-notes/changelog.md`) is auto-generated from the root `CHANGELOG.md`. Do not edit it manually.

- The sync script is `docs/scripts/sync-changelog.mjs`.
- It runs automatically before `npm run dev` and `npm run build`.
- To run manually: `npm run sync` (from the `docs/` directory).
- The script converts title format (`## [0.69] - 2025-12-29` → `## 0.69 (2025-12-29)`) and removes HTML comments.
