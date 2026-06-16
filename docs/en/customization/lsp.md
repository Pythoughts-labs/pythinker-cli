# Language Server Protocol (LSP)

Pythinker Code can connect to [Language Server Protocol](https://microsoft.github.io/language-server-protocol/) servers for semantic code intelligence: go-to-definition, find-references, hover, symbols, and call hierarchy.

## Plugin-only servers

LSP servers are **not** configured in user or project TOML. They come only from installed plugins — inline `lspServers` in `plugin.json` or a plugin-root `.lsp.json` file (same shape as MCP plugin servers). Pythinker does not bundle language-server binaries.

Enable executable plugin artifacts (`plugins.external_exec = true` or `pythinker plugin enable <name>`) so plugin-provided LSP subprocesses are allowed.

## Agent tool

The `LSP` tool is available on the default agent and the `coder` subagent (not on read-only profiles such as `code_reviewer`). It exposes nine operations with 1-based line/character positions (editor-style).

Servers start lazily on first use per language and stay alive for the session. Subagents share the root session's LSP processes.

## Passive diagnostics

After `WriteFile` or `StrReplaceFile`, the session notifies open language servers and surfaces new compiler/linter diagnostics on the next turn via dynamic context injection (budget-capped). Diagnostics are labeled as LSP-reported, not agent-asserted.

## Configuration

Feature switches only — in `~/.pythinker/config.toml`:

```toml
[lsp]
enabled = true
recommendation_disabled = false
recommendation_never = []
```

When you edit a file whose extension matches a discoverable but not-yet-installed plugin server, Pythinker may suggest installing that plugin (respecting `recommendation_never` and auto-disabling after repeated ignores).

## Trust boundary

LSP servers run as subprocesses with the agent's privileges. Hover, symbol, and diagnostic text is treated as untrusted project content (same class as `ReadFile` output).
