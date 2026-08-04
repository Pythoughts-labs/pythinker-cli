---
name: pythinker-code-help
description: Answer Pythinker CLI usage, configuration, and troubleshooting questions. Use when user asks about Pythinker CLI installation, setup, configuration, slash commands, keyboard shortcuts, MCP integration, providers, environment variables, how something works internally, or any questions about Pythinker CLI itself.
---

# Pythinker CLI Help

Help users with Pythinker CLI questions by consulting documentation and source code.

## Strategy

1. **Prefer official documentation** for most questions
2. **Read local source** when in pythinker-code project itself, or when user is developing with pythinker-code as a library (e.g., importing from `pythinker_code` in their code)
3. **Clone and explore source** for complex internals not covered in docs - **ask user for confirmation first**

## Documentation

Base URL: `https://pythoughts-labs.github.io/pythinker-cli/`

Fetch documentation index to find relevant pages:

```
https://pythoughts-labs.github.io/pythinker-cli/llms.txt
```

### Page URL Pattern

- Pages: `https://pythoughts-labs.github.io/pythinker-cli/en/...`

### Topic Mapping

| Topic | Page |
|-------|------|
| Installation, first run | `/en/guides/getting-started.md` |
| Config files | `/en/configuration/config-files.md` |
| Providers, models | `/en/configuration/providers.md` |
| Environment variables | `/en/configuration/env-vars.md` |
| Slash commands | `/en/reference/slash-commands.md` |
| CLI flags | `/en/reference/pythinker-command.md` |
| Keyboard shortcuts | `/en/reference/keyboard.md` |
| MCP | `/en/customization/mcp.md` |
| Agents | `/en/customization/agents.md` |
| Skills | `/en/customization/skills.md` |
| FAQ | `/en/faq.md` |

## Source Code

Repository: `https://github.com/Pythoughts-labs/pythinker-cli`

When to read source:

- In pythinker-code project directory (check `pyproject.toml` for `name = "pythinker-code"`)
- User is importing `pythinker_code` as a library in their project
- Question about internals not covered in docs (ask user before cloning)
