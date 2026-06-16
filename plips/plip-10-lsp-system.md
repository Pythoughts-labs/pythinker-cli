---
Author: Mohamed Elkholy
Updated: 2026-6-15
Status: Proposed
---

# PLIP-10: Full LSP (Language Server Protocol) system for Pythinker CLI

## Summary

Port the reference LSP subsystem (`blackbox/pythinker-src/src/services/lsp`,
`src/tools/LSPTool`, `src/utils/plugins/lsp*`) to pythinker-code as a first-class Python
subsystem. The end state gives the agent real code intelligence — go-to-definition,
find-references, hover, document/workspace symbols, go-to-implementation, and the full call
hierarchy (prepare / incoming / outgoing) — backed by long-lived language-server processes, plus
a **passive diagnostics** stream that surfaces compiler/linter errors into the conversation after
file edits, and **plugin-based server discovery/recommendation** so users can add servers without
shipping binaries.

The port is structured in five phases, each independently shippable and testable:

| Phase | Deliverable | Verifies |
| --- | --- | --- |
| 0 | Dependency decision + transport (`LspClient`: JSON-RPC over stdio) | `make check-pythinker-code` + transport unit tests |
| 1 | Server instance + manager (lifecycle, routing, file sync) | manager unit tests against a fake server |
| 2 | `Lsp` agent tool — all 9 operations + formatters | tool tests + agent-spec load |
| 3 | Passive diagnostics via `DynamicInjectionProvider` | injection-provider tests |
| 4 | Plugin-based server config + recommendation | plugin-integration tests |

This is the complete port. No operation, no lifecycle behaviour, and no diagnostic feature from
the reference is dropped. The only thing that does **not** port is the React/Ink UI layer
(recommendation menu, init-notification toasts); its *intent* is re-expressed through the existing
CLI notification + dynamic-injection systems.

## Motivation

* The agent currently navigates code with `Grep`/`Glob`/`SmartSearch` — textual, not semantic. It
  cannot reliably answer "where is this symbol defined", "who calls this function", or "what does
  the type checker say about this edit" without re-deriving it from text, which is slow and wrong
  on overloads, re-exports, and dynamic dispatch.
* The reference implementation already solved this end-to-end (process management, JSON-RPC framing,
  diagnostic aggregation, dedup, volume-limiting, plugin recommendation). Per the
  `reference-source-of-truth` rule we port it rather than reinvent it.
* Passive diagnostics close the edit→verify loop: after the agent writes a file, the language
  server's `publishDiagnostics` is surfaced into the next turn as budgeted context, so the agent
  sees its own type errors without spending a tool call.
* The Host abstraction (`packages/pythinker-host`) and the dynamic-injection system
  (`src/pythinker_code/soul/dynamic_injection.py`) make this a *clean* fit — the two hardest parts
  (long-lived process I/O, unsolicited context injection) already have idiomatic homes.

## The load-bearing decision: dependencies

This is the tightest constraint and gates the whole design, so it leads.

**Confirmed facts (verified, not assumed):**

* There is **no** `lsprotocol`, `pygls`, `jsonrpc`, or `python-lsp-*` dependency anywhere in
  `pyproject.toml` or any workspace package (`grep` over root + `packages/*` + `sdks/*` — empty).
* The ACP subsystem (`src/pythinker_code/acp/`) speaks a JSON-RPC-shaped protocol, but it **does
  not hand-roll framing** — `acp/host.py:67` wraps `asyncio.StreamReader()` and delegates all
  Content-Length/JSON-RPC parsing to the external `acp` library. There is therefore **no reusable
  Content-Length framing code in the target** to mirror.
* New `[project].dependencies` are governed by the **zero-new-bundled-deps** policy (AGENTS.md);
  adding `lsprotocol` would need explicit maintainer approval and the CONTRIBUTING justification
  template.

**Decision (recommended): hand-roll, do not add a dependency.**

* **Framing**: LSP uses `Content-Length: N\r\n\r\n<json>` over stdio. Reading and writing that is
  ~30 lines over `HostProcess.stdin` (`AsyncWritable`) / `HostProcess.stdout` (`AsyncReadable`).
  No library buys us enough to justify the supply-chain cost.
* **Types**: hand-define only the LSP types the system actually sends/receives as small Pydantic
  models in `lsp/protocol.py` (≈12 models — see Phase 0). We use a *fraction* of the LSP spec; a
  full `lsprotocol` type tree is dead weight.

**Alternative (requires approval):** add `lsprotocol` (pure-Python LSP types + framing). Only
pursue if maintainers explicitly prefer spec-complete types over a hand-rolled subset. The phases
below assume the hand-rolled path; swapping in `lsprotocol` would replace `lsp/protocol.py` and the
framing half of `lsp/client.py` and leave everything else unchanged.

## Reference architecture (what we are porting)

Source tree (`blackbox/pythinker-src/`), ~5,400 lines of TypeScript:

```
src/services/lsp/
  config.ts                (79)   load server configs from enabled plugins
  LSPClient.ts            (447)   JSON-RPC 2.0 client over stdio (framing, handshake, requests)
  LSPServerInstance.ts    (511)   one server: lifecycle, health, retry, state machine
  LSPServerManager.ts     (420)   many servers: ext→server routing, didOpen/Change/Save/Close
  manager.ts              (289)   global singleton, lazy async init, reinit on plugin refresh
  LSPDiagnosticRegistry.ts(386)   store/dedup/volume-limit diagnostics, cross-turn LRU
  passiveFeedback.ts      (328)   register publishDiagnostics handlers → registry
src/tools/LSPTool/
  LSPTool.ts              (860)   the agent tool: 9 ops, validation, dispatch, gitignore filter
  schemas.ts              (215)   zod input schema (discriminated by operation)
  formatters.ts           (592)   LSP results → human-readable text
  symbolContext.ts         (90)   extract symbol context around a position
  prompt.ts                (21)   tool description
src/utils/plugins/
  lspPluginIntegration.ts (387)   load LSP servers from plugin manifests/.lsp.json, env resolution
  lspRecommendation.ts    (374)   match file-ext → recommendable plugin server, install gating
src/{hooks,components}/...        React UI (recommendation menu, init notifications) — intent only
```

### Reference behavioural contract (preserved verbatim in the port)

These are the non-obvious behaviours that make the system robust. Each is reproduced in the port
and cited to the reference line that defines it.

* **Spawn race guard** — after `spawn`, wait for the `spawn` event before writing to stdio,
  because ENOENT (command not found) fires asynchronously; writing first yields unhandled
  rejections. `LSPClient.ts:111-131`. *(Python: `Host.exec` raises synchronously on ENOENT, so the
  port's equivalent is a try/except around `exec` + a post-spawn `initialize` timeout.)*
* **Initialize handshake** — `initialize` request → store `capabilities` → `initialized`
  notification, with `processId`, `workspaceFolders` (LSP 3.16+, required by Pyright/gopls),
  deprecated `rootUri`/`rootPath` (some servers still need them), and
  `general.positionEncodings: ['utf-16']`. `LSPServerInstance.ts:167-272`.
* **Transient-error retry** — retry LSP error `-32801` ("content modified", emitted by
  rust-analyzer/Pyright during indexing) up to 3× with exponential backoff (500·2^n ms).
  `LSPServerInstance.ts:369-394`.
* **Crash recovery cap** — `maxRestarts` default 3; exceeding it stops retrying and parks the
  server in `error`. `LSPServerInstance.ts:142-150, 314-320`.
* **Startup timeout** — wrap `initialize` in a timeout so a hung server never blocks.
  `LSPServerInstance.ts:240-248, 499-511`.
* **Extension routing** — `extensionToLanguage` keys build an `ext → [serverName]` map; first
  match wins. `LSPServerManager.ts:106-117, 192-207`.
* **File sync state machine** — `didOpen` tracks `fileUri → serverName`; `didChange` falls back to
  `didOpen` if the file was never opened; `didSave` triggers diagnostics; `didClose` untracks.
  `LSPServerManager.ts:270-400`.
* **workspace/configuration shim** — register a handler returning `[null, …]` because some servers
  (tsserver) send the request even when told `configuration: false`. `LSPServerManager.ts:125-135`.
* **Lazy async singleton** — init is fire-and-forget; startup never blocks; a `generation` counter
  invalidates stale init promises; reinit on plugin refresh. `manager.ts:154-253`.
* **Diagnostic dedup** — key = `{message, severity, range, source, code}`; dedup within a batch
  *and* cross-turn via a 500-entry LRU keyed by file URI. `LSPDiagnosticRegistry.ts:54-56,
  110-124, 136-184`.
* **Diagnostic volume limit** — sort by severity (errors first), cap 10/file and 30 total.
  `LSPDiagnosticRegistry.ts:42-43, 257-288`.
* **Diagnostic handler isolation** — the `publishDiagnostics` handler is fully wrapped in
  try/except; 3 consecutive failures on a server logs a warning but never breaks the notify loop.
  `passiveFeedback.ts:232-276`.
* **Tool input** — 1-based `line`/`character` (editor-style) converted to 0-based for LSP;
  validate file exists + is a regular file; reject UNC paths (NTLM leak); reject files >10 MB.
  `LSPTool.ts:224-414`, `schemas.ts:8-191`.
* **Result safety** — `maxResultSizeChars` 100 000; results filtered through `git check-ignore`
  (batched ≤50 paths, 5 s timeout) before formatting. `LSPTool.ts`, `formatters.ts:24-72`.
* **Read-only + deferred** — `isReadOnly: true`, `shouldDefer: true` (tool disabled until LSP init
  completes), `isConcurrencySafe: true`. `LSPTool.ts:127-151`.

### LSP wire methods used (the complete set the port must speak)

Requests (client→server): `initialize`, `textDocument/definition`,
`textDocument/references`, `textDocument/hover`, `textDocument/documentSymbol`,
`workspace/symbol`, `textDocument/implementation`, `textDocument/prepareCallHierarchy`,
`callHierarchy/incomingCalls`, `callHierarchy/outgoingCalls`, `shutdown`.
Notifications (client→server): `initialized`, `textDocument/didOpen`, `textDocument/didChange`,
`textDocument/didSave`, `textDocument/didClose`, `exit`.
Reverse (server→client): request `workspace/configuration` (shimmed); notification
`textDocument/publishDiagnostics`, `window/logMessage` (logged).

## Target integration map (verified facts)

Every claim below was checked against the live tree.

* **Tool base class**: `CallableTool2[Params: BaseModel]`
  (`packages/pythinker-core/src/pythinker_core/tooling/__init__.py:232`). Tools declare `name`,
  `description`, `params`, `supports_parallel`; implement `async def __call__(self, params) ->
  ToolReturnValue`. DI is by constructor type annotation. Example skeleton:
  `src/pythinker_code/tools/web/search.py:44-75`.
* **Tool registration**: add the `module:Class` import path to the `tools:` list in
  `src/pythinker_code/agents/default/agent.yaml`. Loader splits on `:`, imports, and injects
  constructor deps by type from a `tool_deps` map; raise `SkipThisTool()` to disable the tool when
  unavailable (`src/pythinker_code/tools/__init__.py`). Loader:
  `src/pythinker_code/soul/toolset.py`; deps wired in `src/pythinker_code/soul/agent.py`.
* **Long-lived process primitive**: `Host.exec(*args, env, cwd) -> HostProcess`
  (`packages/pythinker-host/src/pythinker_host/__init__.py:223-225`). `HostProcess`
  (`:105-129`) exposes `stdin: AsyncWritable`, `stdout: AsyncReadable`, `stderr: AsyncReadable`,
  `pid`, `returncode`, `async wait()`, `async kill()`. **This is the LSP transport substrate** —
  building on it gives Local/SSH/ACP backends for free instead of raw
  `asyncio.create_subprocess_exec`.
* **No reusable JSON-RPC framing** exists in target (see dependency decision). We add it.
* **Config**: `Config` is a Pydantic `BaseModel` with nested sections (`src/pythinker_code/config.py`).
  Add an `lsp: LspConfig` section the same way `web`/`services` are modelled. Secret-bearing
  sections are scope-locked via `SCOPE_LOCKED_PATHS`; LSP commands are not secrets, so they stay
  project-overridable.
* **Approval / read-only**: a tool is read-only simply by never calling
  `self._runtime.approval.request(...)`; gating happens at the execution-policy layer
  (`resolve_execution_policy`, see `tools/web/search.py:62-75`). LSP queries are read-only.
* **Passive-context injection point**: `src/pythinker_code/soul/dynamic_injection.py` defines
  `DynamicInjectionProvider(ABC)` with `async def get_injections(...)`, `DynamicInjection`,
  `ContextBudget`/`injection_budget_from_runtime`, and `collect_within_budget`. Providers are
  registered in `PythinkerSoul` (`soul/pythinkersoul.py:82-96` — `git_status`, `goal_mode`,
  `agent_list`, … under `soul/dynamic_injections/`). **Passive diagnostics become one provider
  here**, budget-aware by construction. `Runtime.rearm_injection` (`soul/agent.py:255-256`) lets a
  tool refresh injections after an edit.
* **Plugin system**: discovery/loading under `src/pythinker_code/plugin/`; manifests are
  `plugin.json`. The reference's `.lsp.json` / `manifest.lspServers` recommendation flow maps onto
  this loader.
* **Tests + gate**: tool tests under `tests/tools/test_*.py` (async, fixtures). Minimum gate for
  this work: `make check-pythinker-code && make test-pythinker-code`. Add a `## Unreleased`
  CHANGELOG entry (required check).

## Reference → target mapping

| Reference (TS) | Target (Python) | Notes |
| --- | --- | --- |
| `services/lsp/LSPClient.ts` | `src/pythinker_code/lsp/client.py` | framing + handshake over `HostProcess` |
| (vscode-jsonrpc framing) | `lsp/framing.py` | hand-rolled Content-Length read/write |
| (vscode-languageserver-protocol types) | `lsp/protocol.py` | ~12 Pydantic models, only what we use |
| `services/lsp/LSPServerInstance.ts` | `lsp/instance.py` | state machine, retry, restart |
| `services/lsp/LSPServerManager.ts` | `lsp/manager.py` | routing + file sync |
| `services/lsp/manager.ts` | `lsp/service.py` | session-scoped service (not a global singleton) |
| `services/lsp/config.ts` | `lsp/config_loader.py` | merge built-in + plugin server configs |
| `services/lsp/LSPDiagnosticRegistry.ts` | `lsp/diagnostics.py` | store/dedup/volume-limit |
| `services/lsp/passiveFeedback.ts` | `lsp/diagnostics.py` (handler) + injection provider | split: capture vs surface |
| `tools/LSPTool/{LSPTool,schemas,formatters,symbolContext,prompt}.ts` | `src/pythinker_code/tools/lsp/` | the agent tool |
| `utils/plugins/lspPluginIntegration.ts` | `lsp/plugin_servers.py` | load servers from plugins |
| `utils/plugins/lspRecommendation.ts` | `lsp/recommend.py` | ext→server recommendation gating |
| React UI (hooks/components) | CLI notification + injection | intent only, no direct port |

**Singleton → session-scoped.** The reference uses a *global* singleton (`manager.ts`) because the
TUI is one process serving one workspace. Pythinker is multi-instance and session-oriented
(`pythinker-multi-instance-invariants`), so the port owns the LSP service on the **`Runtime`**
(one service per session), constructed in `Runtime.create()` and torn down in session cleanup. No
module-global mutable state.

## Target module layout

```
src/pythinker_code/lsp/
  __init__.py            public: LspService, LspConfig re-exports
  framing.py             read_message / write_message (Content-Length over Async streams)
  protocol.py            Pydantic models: Position, Range, Location, Diagnostic, InitializeParams…
  client.py              LspClient: one process, request/notify/on_notify, handshake, shutdown
  instance.py            LspServerInstance: lifecycle + state machine + retry + restart
  manager.py             LspServerManager: ext routing + open/change/save/close file sync
  service.py             LspService: session-scoped facade, lazy init, status, shutdown
  config_loader.py       merge built-in defaults + user config + plugin servers
  diagnostics.py         DiagnosticRegistry (store/dedup/limit) + publishDiagnostics handler
  plugin_servers.py      load LSP server configs from installed plugins
  recommend.py           file-ext → recommendable plugin server, install gating

src/pythinker_code/tools/lsp/
  __init__.py            Lsp tool class export
  tool.py                Lsp(CallableTool2[Params]) — validation, dispatch, gitignore filter
  schemas.py             Params (discriminated by operation), 1-based→0-based
  formatters.py          results → text (definition/refs/hover/symbols/call-hierarchy)
  symbol_context.py      extract symbol context around a position
  tool.md                tool description (load_desc)

src/pythinker_code/soul/dynamic_injections/
  lsp_diagnostics.py     LspDiagnosticsInjectionProvider(DynamicInjectionProvider)
```

---

## Phase 0 — Dependency decision + transport

**Goal:** a `LspClient` that can spawn a server via `Host.exec`, complete the initialize handshake,
send requests/notifications, and dispatch server notifications — with JSON-RPC framing hand-rolled.

### Step 0.1 — `lsp/framing.py`

Content-Length framing over the Host async streams.

```python
# Reads/writes LSP base-protocol frames: "Content-Length: N\r\n\r\n<utf-8 json>".
async def read_message(stdout: AsyncReadable) -> dict[str, Any]:
    # read header lines until blank line; parse Content-Length; read exactly N bytes; json.loads
    ...

async def write_message(stdin: AsyncWritable, message: dict[str, Any]) -> None:
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    stdin.write(header + body)
    await stdin.drain()
```

* Use `AsyncReadable.readuntil(b"\r\n\r\n")` if available, else read line-by-line; then
  `readexactly(content_length)`. Mirror whatever `acp/host.py` does with `asyncio.StreamReader`
  for consistency. Reject frames with no/invalid `Content-Length` (fail-closed, typed error).
* **Tripwire C03/C06**: a malformed frame raises a typed `LspProtocolError`, never returns `{}`.

### Step 0.2 — `lsp/protocol.py`

Hand-define exactly the models used (do not import the LSP spec wholesale):

`Position`, `Range`, `Location`, `LocationLink`, `Diagnostic` (+ `DiagnosticSeverity` enum),
`DocumentSymbol`, `SymbolInformation` (+ `SymbolKind` enum), `Hover`/`MarkupContent`,
`CallHierarchyItem`, `CallHierarchyIncomingCall`, `CallHierarchyOutgoingCall`, `InitializeParams`,
`InitializeResult`/`ServerCapabilities`, `PublishDiagnosticsParams`. All `pydantic.BaseModel` with
`model_config = ConfigDict(extra="ignore")` (servers send extra fields we ignore).

### Step 0.3 — `lsp/client.py`

```python
class LspClient:
    def __init__(self, host: Host, *, logger=...): ...
    @property
    def capabilities(self) -> ServerCapabilities | None: ...
    @property
    def is_initialized(self) -> bool: ...

    async def start(self, command: str, args: list[str], *, env=None, cwd=None) -> None:
        # self._proc = await host.exec(command, *args, env=env, cwd=cwd)
        # spawn read loop: while returncode is None: msg = await read_message(stdout); dispatch(msg)
        # ENOENT/exec failure -> LspStartError (typed). drain stderr to logger.
    async def initialize(self, params: InitializeParams) -> InitializeResult: ...
    async def send_request(self, method: str, params: Any) -> Any: ...      # correlate by id
    async def send_notification(self, method: str, params: Any) -> None: ...
    def on_notification(self, method: str, handler: Callable[[Any], None]) -> None: ...
    def on_request(self, method: str, handler) -> None: ...                  # for workspace/configuration
    async def stop(self) -> None:                                           # shutdown + exit + kill
```

* **Request correlation**: monotonically-increasing int `id`; a `dict[int, asyncio.Future]` pending
  map; the read loop resolves the future on a matching `id`, routes `method`-only messages to
  notification handlers, and answers server→client requests via `on_request` handlers.
* **Read loop**: a single `asyncio.Task` reading frames; on `returncode is not None` or
  `IncompleteReadError`, fail all pending futures with `LspServerDown` (C01/C10 — never resolve a
  pending request as success after the process died) and mark the client stopped.
* **`stop()`**: `send_request("shutdown")` → `send_notification("exit")` → cancel read loop →
  `proc.kill()` with a short grace; idempotent; suppress errors during teardown.

### Verification (Phase 0)

`tests/tools/test_lsp_client.py`: a fake stdio server (an in-process `asyncio` pipe pair or a tiny
Python echo server speaking LSP framing) proves: framing round-trips; initialize handshake stores
capabilities; a request resolves on matching id; a notification reaches its handler; process death
fails pending requests with a typed error (not a hang, not a false success). Gate:
`make check-pythinker-code` + these tests.

---

## Phase 1 — Server instance + manager

### Step 1.1 — `lsp/instance.py`

`LspServerInstance` wraps one `LspClient` with the lifecycle contract:

```python
class LspState(StrEnum): STOPPED; STARTING; RUNNING; ERROR

class LspServerInstance:
    def __init__(self, name: str, config: LspServerConfig, host: Host): ...
    state: LspState; start_time; last_error; restart_count
    async def start(self) -> None:    # idempotent; build InitializeParams; startup timeout; crash cap
    async def stop(self) -> None:     # idempotent
    async def restart(self) -> None:  # enforces maxRestarts
    def is_healthy(self) -> bool:     # RUNNING and client.is_initialized
    async def send_request(self, method, params) -> Any:  # retry -32801 w/ backoff (3x)
    async def send_notification(self, method, params) -> None
    def on_notification(self, method, handler) -> None
    def on_request(self, method, handler) -> None
```

* `InitializeParams` built exactly as the reference (`processId=os.getpid()`, `workspaceFolders`,
  `rootUri`/`rootPath`, `capabilities` with sync/hover/definition/references/documentSymbol/
  callHierarchy/publishDiagnostics, `general.positionEncodings=['utf-16']`,
  `initializationOptions` from config). Cite parity to `LSPServerInstance.ts:167-237`.
* Startup timeout via `asyncio.wait_for(client.initialize(...), config.startup_timeout)`.
* Transient retry: catch LSP error code `-32801`, backoff `0.5 * 2**attempt`, ≤3 attempts.
* Crash cap: track `crash_recovery_count`; over `max_restarts` (default 3) → `ERROR`, stop retrying.

### Step 1.2 — `lsp/manager.py`

`LspServerManager` owns many instances and the file-sync state:

```python
class LspServerManager:
    def __init__(self, host: Host, servers: dict[str, LspServerConfig]): ...
    async def initialize(self) -> None:   # build ext->[server] map; register workspace/configuration shim
    async def shutdown(self) -> None:     # stop all (gather, isolate failures)
    def server_for_file(self, path: str) -> LspServerInstance | None    # ext lookup, first match
    async def ensure_started(self, path: str) -> LspServerInstance | None
    async def send_request(self, path, method, params) -> Any | None
    async def open_file(self, path, content) -> None      # didOpen, track fileUri->server
    async def change_file(self, path, content) -> None    # didChange, fallback to open_file
    async def save_file(self, path) -> None               # didSave (triggers diagnostics)
    async def close_file(self, path) -> None              # didClose, untrack
    def is_file_open(self, path) -> bool
    def all_servers(self) -> dict[str, LspServerInstance]
```

* `ext_map: dict[str, list[str]]` from each server's `extension_to_language` keys.
* `opened_files: dict[str, str]` (fileUri → serverName).
* `workspace/configuration` shim returns `[None] * len(params.items)`.
* Per-server init failure is isolated (continue with others); aggregate errors logged.

### Step 1.3 — `lsp/service.py`

Session-scoped facade (replaces the reference's global `manager.ts`):

```python
class LspService:
    @classmethod
    async def create(cls, runtime: Runtime) -> "LspService":   # lazy: kicks off init as a task
    def status(self) -> LspInitStatus                          # not_started|pending|success|failed
    def is_connected(self) -> bool
    async def wait_for_init(self) -> None
    async def reinitialize(self) -> None                       # on plugin refresh
    async def shutdown(self) -> None
    @property
    def manager(self) -> LspServerManager | None
    @property
    def diagnostics(self) -> DiagnosticRegistry
```

* Init is fire-and-forget (`asyncio.create_task`); startup never blocks the agent. A `generation`
  int guards against stale init completing after a reinit.
* On init success, wire the `publishDiagnostics` handlers (Phase 3).
* Held on `Runtime` (new field `lsp: LspService | None`), constructed in `Runtime.create()`,
  shut down in session teardown alongside other session resources.

### Step 1.4 — `lsp/config_loader.py` + config model

`src/pythinker_code/config.py`:

```python
class LspServerConfig(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)
    extension_to_language: dict[str, str]          # ".py": "python"
    env: dict[str, str] = Field(default_factory=dict)
    initialization_options: dict[str, Any] | None = None
    startup_timeout: float = 30.0
    max_restarts: int = 3

class LspConfig(BaseModel):
    enabled: bool = True
    servers: dict[str, LspServerConfig] = Field(default_factory=dict)

class Config(BaseModel):
    ...
    lsp: LspConfig = Field(default_factory=LspConfig)
```

* `config_loader.py` merges: built-in defaults (e.g. `pyright --stdio` for `.py` if present on
  PATH) ← user `config.lsp.servers` ← plugin-provided servers (Phase 4). Later wins.
* Ship a sane built-in default for Python only (pyright if discoverable); everything else is
  user/plugin-supplied. Do **not** bundle server binaries.

TOML shape:

```toml
[lsp]
enabled = true
[lsp.servers.python]
command = "pyright-langserver"
args = ["--stdio"]
extension_to_language = { ".py" = "python", ".pyi" = "python" }
```

### Verification (Phase 1)

`tests/tools/test_lsp_manager.py` against a fake server: ext routing picks the right server;
`open_file`/`change_file` fallback chain; `didSave` emits; `shutdown` stops all and isolates a
failing server; crash cap parks at `ERROR` after `max_restarts`; transient `-32801` retries then
succeeds. Gate: `make check-pythinker-code && make test-pythinker-code`.

---

## Phase 2 — The `Lsp` agent tool (all 9 operations)

### Step 2.1 — `tools/lsp/schemas.py`

```python
class Operation(StrEnum):
    GO_TO_DEFINITION; FIND_REFERENCES; HOVER; DOCUMENT_SYMBOL; WORKSPACE_SYMBOL
    GO_TO_IMPLEMENTATION; PREPARE_CALL_HIERARCHY; INCOMING_CALLS; OUTGOING_CALLS

class Params(BaseModel):
    operation: Operation
    file_path: str = Field(description="File to operate on")
    line: int = Field(ge=1, description="1-based line, as shown in editors")
    character: int = Field(ge=1, description="1-based character offset")
```

Validation in the tool (not the schema, so errors are typed tool results): file exists + is a
regular file; reject UNC (`\\`/`//` prefix); reject >10 MB. Convert to 0-based LSP position
(`line-1`, `character-1`) at the boundary.

### Step 2.2 — `tools/lsp/tool.py`

```python
class Lsp(CallableTool2[Params]):
    name = "Lsp"
    description = load_desc(Path(__file__).parent / "tool.md", {})
    params = Params
    supports_parallel = True       # isConcurrencySafe

    def __init__(self, runtime: Runtime):
        super().__init__()
        if not runtime.config.lsp.enabled or runtime.lsp is None:
            raise SkipThisTool()
        self._runtime = runtime
        self._lsp = runtime.lsp

    async def __call__(self, params: Params) -> ToolReturnValue:
        builder = ToolResultBuilder()
        # 0. shouldDefer: if status != success, return a clear "LSP still initializing / unavailable"
        # 1. validate file (exists/regular/UNC/size) -> typed builder.error on failure
        # 2. read content; manager.open_file(path, content) to ensure server has the doc
        # 3. dispatch by operation -> manager.send_request(path, <lsp method>, position params)
        # 4. None/empty -> operation-specific guidance message (not a bare empty)
        # 5. gitignore-filter result paths (git check-ignore, batched<=50, 5s timeout)
        # 6. format via formatters; cap at 100_000 chars
        return builder.ok(text, brief=user_facing_name(params))
```

Dispatch table (operation → LSP method(s)):

| Operation | LSP call(s) |
| --- | --- |
| go_to_definition | `textDocument/definition` |
| find_references | `textDocument/references` (`context.includeDeclaration=true`) |
| hover | `textDocument/hover` |
| document_symbol | `textDocument/documentSymbol` |
| workspace_symbol | `workspace/symbol` (query="") |
| go_to_implementation | `textDocument/implementation` |
| prepare_call_hierarchy | `textDocument/prepareCallHierarchy` |
| incoming_calls | `prepareCallHierarchy` → `callHierarchy/incomingCalls` |
| outgoing_calls | `prepareCallHierarchy` → `callHierarchy/outgoingCalls` |

* **Read-only**: never calls `approval.request`. Optionally gate on execution policy if the active
  profile forbids subprocess (LSP spawns a process) — mirror `search.py`'s policy check.
* **Untrusted output**: hover/symbol text comes from project files; pass through
  `ToolResultBuilder.mark_untrusted()` so it is wrapped, consistent with `utils/trust.py`.

### Step 2.3 — `tools/lsp/formatters.py` + `symbol_context.py`

Port `formatters.ts` faithfully: relative-path normalization (decode percent-encoding, `\`→`/`,
prefer relative if shorter and not `../../`), group references/symbols/calls by file with
`line:char`, `SymbolKind`/`DiagnosticSeverity` enum→label maps, recursive `DocumentSymbol` child
counting, call-hierarchy `fromRanges` rendering, and the exact empty-result guidance strings
(`formatters.ts:127-592`). `symbol_context.py` ports `symbolContext.ts` (extract the symbol +
surrounding lines for context).

### Step 2.4 — `tools/lsp/tool.md` + registration

`tool.md` = the reference `prompt.ts` text (9 operations, 1-based note, "server must be configured"
caveat). Register in `src/pythinker_code/agents/default/agent.yaml` under `tools:`:

```yaml
    - "pythinker_code.tools.lsp:Lsp"
```

Add the same line to any subagent spec that should have code-intelligence (e.g. `coder.yaml`,
`code_reviewer.yaml`) — decision: include in `coder`/`code-reviewer`, exclude from `explore`/`scout`
(they are textual-recon by design). Wire `Runtime` into the tool deps if not already present
(it is — `search.py` receives it).

### Verification (Phase 2)

`tests/tools/test_lsp_tool.py` against a fake server returning canned LSP responses: each of the 9
operations dispatches the right method and formats correctly; 1-based→0-based conversion; UNC
rejection; >10 MB rejection; empty-result guidance; deferred behaviour when init not done; agent
spec loads with the tool present (`tests/core` agent-load test). Inline-snapshot the formatter
output (`pytest --inline-snapshot=fix`). Gate: `make check-pythinker-code && make test-pythinker-code`,
and the wire handshake snapshot in `tests_e2e/` if it pins the tool list
(`full-test-scope-includes-tests-e2e`).

---

## Phase 3 — Passive diagnostics

### Step 3.1 — `lsp/diagnostics.py`

Port `LSPDiagnosticRegistry.ts` + the capture half of `passiveFeedback.ts`:

```python
class DiagnosticRegistry:
    def register_pending(self, server_name: str, files: list[DiagnosticFile]) -> None
    def check_for_diagnostics(self) -> list[ServerDiagnostics]   # dedup + volume-limit, mark sent
    def clear_all(self) -> None
    def clear_for_file(self, file_uri: str) -> None
    def pending_count(self) -> int
```

* Cross-turn dedup: `OrderedDict`-based LRU (cap 500 files) keyed by file URI → set of diagnostic
  keys; key = json of `{message, severity, range, source, code}`.
* Volume limit: sort by severity (errors first), cap 10/file and 30 total.
* `publishDiagnostics` handler (registered by `LspService` on init) maps LSP severity 1-4 →
  Error/Warning/Info/Hint, parses URIs via `file://`→path, and `register_pending(...)`. Handler is
  fully try/except-wrapped; 3 consecutive failures on a server logs once (C08 observability) but
  never breaks the notify loop.

### Step 3.2 — `soul/dynamic_injections/lsp_diagnostics.py`

The *surface* half — a provider mirroring `git_status.py`:

```python
class LspDiagnosticsInjectionProvider(DynamicInjectionProvider):
    def __init__(self, runtime: Runtime): self._runtime = runtime
    async def get_injections(self, budget: ContextBudget, ...) -> list[DynamicInjection]:
        if self._runtime.lsp is None or not self._runtime.lsp.is_connected(): return []
        groups = self._runtime.lsp.diagnostics.check_for_diagnostics()
        if not groups: return []
        text = render_diagnostics_block(groups)        # "<system-reminder> LSP diagnostics …"
        return [DynamicInjection(text=text, ...)]       # collect_within_budget truncates if needed
```

* Register it in `PythinkerSoul` alongside the other providers (`soul/pythinkersoul.py:82-96`).
* The provider is **budget-governed** automatically by `collect_within_budget` /
  `injection_budget_from_runtime` — no separate volume logic needed beyond the registry's own caps.
* **Edit→save→diagnose loop**: after `WriteFile`/`StrReplaceFile` mutate a file, call
  `runtime.lsp.manager.save_file(path)` (and `change_file` with new content) so the server
  re-diagnoses, then `runtime.rearm_injection(...)` so the next turn picks up fresh diagnostics.
  Wire this in the file tools' success path (a 3-line hook guarded by `runtime.lsp is not None`).
  This is the single cross-cutting touch into existing tools — keep it surgical.

### Verification (Phase 3)

`tests/tools/test_lsp_diagnostics.py`: registry dedups within a batch and across turns; volume caps
hold; severity sort; the injection provider returns nothing when disconnected, returns a budgeted
block when diagnostics pending, and is truncated under a small budget; the file-tool hook calls
`save_file` + `rearm_injection` only when LSP is present. Gate: `make check-pythinker-code &&
make test-pythinker-code`.

---

## Phase 4 — Plugin-based server discovery + recommendation

### Step 4.1 — `lsp/plugin_servers.py`

Port `lspPluginIntegration.ts`: read LSP server configs from installed plugins via two sources —
inline `manifest.lspServers` and an external `<plugin_root>/.lsp.json` (same schema). Resolve env
placeholders: `${PYTHINKER_PLUGIN_ROOT}`, `${PYTHINKER_PLUGIN_DATA}`, `${user_config.KEY}`, and
standard `${VAR}`. Scope names as `plugin:<plugin>:<server>` to avoid collisions. Feed the result
into `config_loader.py`'s merge (highest precedence after explicit user config — decision: user
config wins over plugin, so a user can override a plugin's command).

### Step 4.2 — `lsp/recommend.py`

Port `lspRecommendation.ts`: on a file edit, match the extension against discoverable plugin
servers (inline manifests only — `.lsp.json` is post-install, not pre-install readable); filter by:
server supports ext ∧ binary on PATH ∧ plugin not installed ∧ not in
`config.lsp.recommendation_never` ∧ recommendations not disabled. Sort official-marketplace plugins
first. Surface **once per session**.

### Step 4.3 — Recommendation surface (UI intent, not React)

The reference shows a React menu (`LspRecommendationMenu.tsx`) with Yes/No/Never/Disable +
30 s auto-dismiss, and a polling init-error notification (`useLspInitializationNotification.tsx`).
Re-express the intent on the CLI:

* **Recommendation**: emit a one-line `Suggest`-style hint (the existing `tools/suggest` /
  notification path) — "Install plugin X for Python code intelligence (`pythinker plugin add X`)".
  No interactive blocking menu; the agent/user acts via existing plugin commands. Track
  never/disable in config (`lsp.recommendation_never: list[str]`, `lsp.recommendation_disabled:
  bool`), and auto-disable after N ignores.
* **Init errors**: when `LspService.status()` is `failed` or a server is in `ERROR`, log to the
  plugins-error channel surfaced by `/doctor` (dedup by `source:message`), exactly as the reference
  feeds `appState.plugins.errors`.

### Step 4.4 — Reinit on plugin refresh

When plugins are added/removed/refreshed, call `runtime.lsp.reinitialize()` (the generation guard
makes this safe). Hook into the existing plugin-refresh path in `src/pythinker_code/plugin/`.

### Verification (Phase 4)

`tests/tools/test_lsp_plugins.py`: inline + `.lsp.json` server loading; env-placeholder resolution;
scope-prefixing; recommendation filter matrix (ext/binary/installed/never/disabled); official-first
sort; reinit generation guard ignores a stale init. Gate: `make check-pythinker-code &&
make test-pythinker-code`.

---

## Cross-cutting concerns

* **C-tripwire compliance** (AGENTS.md): C01/C10 — process death fails pending requests, never a
  false success; init failure parks the service in `failed`, never reports connected. C03/C06 —
  framing/protocol errors are typed (`LspProtocolError`/`LspServerDown`), never swallowed or blurred
  with empty results. C08 — the read loop and server processes have explicit lifecycle
  (start/stop/restart), timeout (startup + per-request), cancellation (read-loop task cancelled on
  stop), and observability (stderr drained to logger, consecutive-failure warnings). C13 — passive
  diagnostics carry their `source` (server name) and are clearly framed as LSP-reported, not
  agent-asserted.
* **Security**: LSP servers are subprocesses with the agent's privileges. Honour the execution
  profile (don't spawn under a no-subprocess profile). Server *output* (hover/symbol text,
  diagnostic messages) is untrusted project content → `mark_untrusted`. Reject UNC paths. Never log
  server stdout at info (could contain source); stderr→debug only.
* **Performance**: servers are lazy (spawned on first file touch per language) and long-lived
  (reused across tool calls). `documentSymbol`/`workspace/symbol` can be large → the 100 000-char
  cap + spill. Diagnostics are budget-capped by the injection system.
* **Compatibility**: new config keys (`config.lsp.*`), a new tool name (`Lsp`), and a new dynamic
  injection. The tool addition changes the agent's advertised tool list → update the wire-handshake
  inline snapshot in `tests_e2e/` (`full-test-scope-includes-tests-e2e`). Add a `## Unreleased`
  CHANGELOG bullet. No persisted-session schema change (LSP state is ephemeral per session).
* **Docs**: add an LSP page under `docs/en/customization/` (config shape, built-in Python default,
  how to add a server, plugin recommendation), and update the architecture repo-map
  (`docs/en/customization/architecture.md`) with the `lsp/` subsystem and its trust boundary.

## What does NOT port

* React/Ink components and hooks (`components/LspRecommendation`, `hooks/useLsp*`) — their *intent*
  is re-expressed via the CLI notification/suggest path and `/doctor` error surface (Phase 4.3).
* The global-singleton lifetime model — replaced by session-scoped ownership on `Runtime`.

## Open questions (resolve before/while implementing)

1. **Built-in default servers**: ship only a Python default (pyright if on PATH), or none at all
   (everything user/plugin-supplied)? Recommendation: Python-only default, gated on binary presence.
2. **`lsprotocol` vs hand-rolled**: this plan recommends hand-rolled (no new dep). Confirm with
   maintainers; if they prefer `lsprotocol`, it replaces `protocol.py` + framing only.
3. **Server output trust level**: confirm `mark_untrusted` is the right wrapper for hover/symbol
   text (it is project source, same trust class as `ReadFile` output).
4. **Execution-profile gating**: should LSP be disabled under read-only/review profiles (they
   shouldn't spawn processes), or allowed because it's read-only? Recommendation: allow in
   `coder`/default, disable where the profile forbids subprocess.

## Verification matrix (per AGENTS.md)

| Phase | Command |
| --- | --- |
| 0–4 (per phase) | `make check-pythinker-code && make test-pythinker-code` |
| Tool list change | rebuild wire-handshake snapshot in `tests_e2e/` (`--inline-snapshot=fix`) |
| Before PR | `## Unreleased` CHANGELOG entry; `pythinker-guard` skill; CodeRabbit green |

## File-by-file checklist (the complete port, in build order)

- [ ] `src/pythinker_code/lsp/framing.py` — Content-Length read/write + `LspProtocolError`
- [ ] `src/pythinker_code/lsp/protocol.py` — ~12 Pydantic models + enums
- [ ] `src/pythinker_code/lsp/client.py` — `LspClient` (spawn via `Host.exec`, handshake, requests)
- [ ] `tests/tools/test_lsp_client.py`
- [ ] `src/pythinker_code/lsp/instance.py` — `LspServerInstance` (state machine, retry, restart)
- [ ] `src/pythinker_code/lsp/manager.py` — `LspServerManager` (routing, file sync)
- [ ] `src/pythinker_code/lsp/service.py` — `LspService` (session-scoped, lazy init, reinit)
- [ ] `src/pythinker_code/lsp/config_loader.py` — merge defaults/user/plugin
- [ ] `src/pythinker_code/config.py` — add `LspConfig`/`LspServerConfig` + `Config.lsp`
- [ ] `src/pythinker_code/soul/agent.py` — `Runtime.lsp` field + construct in `Runtime.create` + teardown
- [ ] `tests/tools/test_lsp_manager.py`
- [ ] `src/pythinker_code/tools/lsp/schemas.py`
- [ ] `src/pythinker_code/tools/lsp/formatters.py`
- [ ] `src/pythinker_code/tools/lsp/symbol_context.py`
- [ ] `src/pythinker_code/tools/lsp/tool.py` — `Lsp(CallableTool2)`
- [ ] `src/pythinker_code/tools/lsp/tool.md`
- [ ] `src/pythinker_code/agents/default/agent.yaml` (+ `coder.yaml`, `code_reviewer.yaml`)
- [ ] `tests/tools/test_lsp_tool.py`
- [ ] `src/pythinker_code/lsp/diagnostics.py` — registry + publishDiagnostics handler
- [ ] `src/pythinker_code/soul/dynamic_injections/lsp_diagnostics.py` — injection provider
- [ ] `src/pythinker_code/soul/pythinkersoul.py` — register provider
- [ ] file tools (`tools/file/*`) — `save_file` + `rearm_injection` hook (guarded, surgical)
- [ ] `tests/tools/test_lsp_diagnostics.py`
- [ ] `src/pythinker_code/lsp/plugin_servers.py`
- [ ] `src/pythinker_code/lsp/recommend.py`
- [ ] `src/pythinker_code/plugin/` — reinit-on-refresh hook
- [ ] `tests/tools/test_lsp_plugins.py`
- [ ] `CHANGELOG.md` — `## Unreleased` entry
- [ ] `docs/en/customization/` — LSP page + architecture repo-map update
- [ ] `tests_e2e/` — wire-handshake snapshot refresh
