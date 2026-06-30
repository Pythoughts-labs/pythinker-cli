Execute a deterministic Python workflow that orchestrates multiple subagents with `agent()`, `parallel()`, and `pipeline()`. Use this only when the user explicitly asks for a workflow, fan-out, or multi-agent orchestration, or when a task decomposes into many independent subtasks worth running concurrently and synthesizing.

`script` is required raw Python (no Markdown fences). Rules:

- The first statement MUST be `meta = {"name": "short_snake_case", "description": "non-empty description"}`. `meta` must be a literal dict (no function calls or interpolation). `meta["phases"]` is optional documentation; live progress is driven by `phase(title)` at runtime.
- After `meta`, write plain Python. Do NOT use `import`, `time`, `random`, `datetime`, `os`, `sys`, `open`, `eval`, or `exec` — scripts must be deterministic.
- The script must call `agent()` at least once. End by `return`-ing a compact JSON-serializable value.

Available globals:

- `agent(prompt, opts=None)` — spawn one subagent; returns its final text, or a validated dict when `opts={"schema": <json-schema-dict>}`. Other opts: `label` (short, 2-5 words), `phase`, `model`, `agent_type` (a built-in subagent type, e.g. `"explore"`, `"coder"`, `"review"`). Always `await` it. A failed agent returns `None` and logs — check for `None` before synthesizing.
- `parallel([awaitables])` — run awaitables concurrently, results in input order: `await parallel([agent("a"), agent("b")])`. Pass awaitables, NOT functions.
- `pipeline(items, *stages)` — run each item through sequential stages while items fan out. Each stage is called `(prev, original, index)` and may be sync or async.
- `phase(title)` — start a progress group. Names may be conditional or built in a loop; do not predeclare speculative phases.
- `log(message)`, `args` (the optional JSON `args` input), `cwd`, `budget` (`.total`, `.spent()`, `.remaining()`).

Include enough context and file paths in each `agent()` prompt — subagents do NOT inherit the parent conversation. Add a final synthesis `agent()` (or a plain return) that combines results into `{ "ok": ..., ... }`.
