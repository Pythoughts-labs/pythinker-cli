# Language Server Protocol (LSP)

Pythinker Code can connect to [Language Server Protocol](https://microsoft.github.io/language-server-protocol/) servers for semantic code intelligence: go-to-definition, find-references, hover, symbols, and call hierarchy.

## Plugin-only servers

LSP servers are **not** configured in user or project TOML. They come only from installed plugins — inline `lspServers` in `plugin.json` or a plugin-root `.lsp.json` file. Pythinker does not bundle language-server binaries.

Enable executable plugin artifacts (`plugins.external_exec = true` or `pythinker plugin enable <name>`) so plugin-provided LSP subprocesses are allowed.

### Server config schema

Each entry in `lspServers` (or the root object of `.lsp.json`) maps a server name to a config object:

```json
{
  "my-server": {
    "command": "pylsp",
    "args": ["--check-parent-process"],
    "extensionToLanguage": { ".py": "python" },
    "env": { "VIRTUAL_ENV": "${VIRTUAL_ENV:-}" },
    "initializationOptions": {},
    "startupTimeout": 30.0,
    "maxRestarts": 3
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `command` | yes | Executable to launch |
| `args` | no | Additional CLI arguments |
| `extensionToLanguage` | yes | Maps file extensions to LSP language IDs |
| `env` | no | Extra environment variables for the server process |
| `initializationOptions` | no | Passed verbatim in the LSP `initialize` request |
| `startupTimeout` | no | Seconds to wait for server ready (default `30.0`, must be `> 0`) |
| `maxRestarts` | no | Max automatic restarts on crash (default `3`, `0` disables) |

Values in `command`, `args`, and `env` support `${VAR}` and `${VAR:-default}` expansion against the process environment. Two plugin-local path variables are always available: `${PYTHINKER_PLUGIN_ROOT}` (the plugin directory) and `${PYTHINKER_PLUGIN_DATA}` (a writable per-plugin data directory). The `CLAUDE_PLUGIN_ROOT` / `CLAUDE_PLUGIN_DATA` spellings are accepted as aliases.

## Agent tool

The `LSP` tool is available on the default agent and the `coder` subagent (not on read-only profiles such as `code_reviewer`). It exposes nine operations with 1-based line/character positions (editor-style).

| Operation | LSP method |
|-----------|------------|
| `goToDefinition` | `textDocument/definition` |
| `findReferences` | `textDocument/references` |
| `hover` | `textDocument/hover` |
| `documentSymbol` | `textDocument/documentSymbol` |
| `workspaceSymbol` | `workspace/symbol` |
| `goToImplementation` | `textDocument/implementation` |
| `prepareCallHierarchy` | `textDocument/prepareCallHierarchy` |
| `incomingCalls` | `callHierarchy/incomingCalls` |
| `outgoingCalls` | `callHierarchy/outgoingCalls` |

Results from `findReferences`, `goToDefinition`, `goToImplementation`, and `workspaceSymbol` automatically filter out paths that match the project's `.gitignore`.

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
