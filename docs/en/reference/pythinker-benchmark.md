# Pythinker Benchmark

Pythinker Benchmark is the native local-fixture harness for comparing how a configured Pythinker model handles small, deterministic coding tasks. It runs inside the current Pythinker session, uses the active toolset and approval runtime, materializes each task into an isolated workspace, executes the agent turn, then runs the task's verification command and writes replayable artifacts.

It is designed for repeatable local checks, not hosted leaderboard scoring. SWE-style input is supported as trusted local fixtures; it is not a full SWE-bench Docker runner.

## Commands

Run the default core suite:

```sh
/benchmark start
```

Run a single task or named suite:

```sh
/benchmark start --task core-safe-path-join
/benchmark start --suite pythinker-smoke
```

Estimate a run without making model calls:

```sh
/benchmark estimate --suite pythinker-core
```

Inspect available tasks and saved runs:

```sh
/benchmark list
/benchmark show <run-id>
/benchmark report --suite pythinker-core
```

Namespaced aliases are available for interactive completion: `/benchmark:start`, `/benchmark:all`, `/benchmark:estimate`, `/benchmark:list`, `/benchmark:show`, `/benchmark:report`, and `/benchmark:swe`.

The supported flags are:

| Flag | Applies to | Behavior |
| --- | --- | --- |
| `--model <model-key>` | `start`, `estimate`, `swe` | Uses a configured model instead of the active/default model. |
| `--task <task-id>` | `start`, `estimate` | Runs or estimates one bundled task. Mutually exclusive with `--suite`. |
| `--suite <suite-name>` | `start`, `estimate`, `report` | Selects a bundled suite or filters report output. |
| `--repeat <n>` | `start`, `estimate`, `swe` | Runs each selected task multiple times. |
| `--timeout-seconds <n>` | `start`, `swe` | Overrides the task timeout for the agent turn. |
| `--output <path>` | all commands that read or write runs | Uses a custom benchmark artifact root instead of the Pythinker share directory. |
| `--dataset <path.jsonl>` | `swe` | Loads SWE-style local fixture records from a JSONL file. |
| `--instance <instance-id>` | `swe` | Runs only one instance from the dataset. |
| `--trusted-dataset true` | `swe` | Required acknowledgement before dataset verification commands can run. |

`--max-concurrency` is parsed but must remain `1` in the current implementation. `--judges` is parsed but only `off` is supported.

## Architecture

The benchmark integration has four layers:

1. Slash commands in `src/pythinker_code/soul/slash.py` register `/benchmark` and the namespaced aliases.
2. `src/pythinker_code/benchmark/commands.py` parses arguments, resolves the active model, expands tasks or suites, and dispatches each run.
3. `src/pythinker_code/benchmark/runner.py` prepares the workspace, temporarily applies the task's `max_steps` to the soul loop, overrides the runtime work directory, calls `PythinkerSoul.turn`, runs verification, and restores the previous runtime state in a `finally` block.
4. `src/pythinker_code/benchmark/records.py` and `src/pythinker_code/benchmark/report.py` persist run metadata, traces, summaries, context and Wire slices, and Markdown reports.

Task and suite definitions are regular bundled JSON files under `src/pythinker_code/benchmark/bundled/`. `src/pythinker_code/benchmark/tasks.py` validates bundled task shape and rejects unsafe workspace paths before files are materialized.

## Bundled suites

`pythinker-core` is the default suite. It covers deterministic local coding tasks that expose common agent failure modes:

- `core-atomic-transfer`: transactional rollback, missing accounts, insufficient funds, and non-positive transfer amounts.
- `core-dedup-order`: ordered de-duplication for lists and generators, including falsey values.
- `core-explicit-none-metadata`: explicit `None` handling without losing valid falsey metadata values.
- `core-safe-path-join`: path traversal defense, absolute-path rejection, sibling-prefix escapes, and symlink escapes.

`pythinker-smoke` contains smaller tasks for validating the runner itself:

- `smoke-edit-readme`
- `smoke-fix-python-test`
- `smoke-add-small-function`

## Task schema

A bundled task JSON object contains:

```json
{
  "id": "core-safe-path-join",
  "title": "Safe Path Join",
  "description": "Reject path traversal while allowing paths inside the root.",
  "prompt": "Fix paths.safe_join ...",
  "workspace": {
    "files": {
      "paths.py": "...",
      "test_paths.py": "..."
    }
  },
  "verification": {
    "type": "command",
    "command": "python -m pytest test_paths.py -q"
  },
  "limits": {
    "timeout_seconds": 120,
    "max_steps": 60
  },
  "tags": ["core", "security"]
}
```

Workspace paths must be relative, non-empty, and must not contain `..` path segments. Verification type is currently `command`.

## Online discovery and quiz fixtures

`/benchmark discover` can write provisional JSONL records from allowlisted online benchmark sources. These records preserve the source URL and are marked `trusted: false`. Online quiz fixture records use deterministic review metadata:

```json
{
  "verification": {
    "type": "answer_contains",
    "expected_substrings": ["terminal-bench", "hard"]
  },
  "trusted": false,
  "workspace": {
    "files": {}
  }
}
```

These manifests are for dataset review and offline conversion first. They are not runnable through `/benchmark:swe`, and `answer_contains` is not executed by the benchmark runner. Convert reviewed tasks into trusted local fixtures with workspace files and a local verification command before running them.

## SWE-style local fixtures

`/benchmark:swe` loads newline-delimited JSON records with local workspace files and a verification command. The command is executed on the local machine after the agent turn, so the slash command refuses to run unless `--trusted-dataset true` is present:

```sh
/benchmark:swe --dataset ./cases.jsonl --trusted-dataset true
```

Each record must include `instance_id`, `repo`, `base_commit`, `problem_statement`, `workspace.files`, and `verification.command`. Optional `FAIL_TO_PASS`, `PASS_TO_PASS`, and `limits` fields are folded into the generated local fixture task.

## Artifacts

By default, benchmark runs are written under the Pythinker share directory in `benchmarks/<run-id>/`. A custom root can be supplied with `--output <path>`.

Each run directory contains:

| File or directory | Contents |
| --- | --- |
| `run.json` | Run metadata: command, model key, provider key, task id, suite name, repeat index, timestamps, and final status. |
| `summary.json` | Runtime summary: duration, step count, tool calls, changed files, token counts, verification status, and exit reason. |
| `report.md` | Human-readable report for the run. |
| `trace.jsonl` | Benchmark event trace, including workspace preparation, model message, and verification result. |
| `workspace/` | The materialized local task workspace after the run. |
| `context.jsonl` and `wire.jsonl` | Slices copied from the active session for replay and debugging. |

Generated verification caches such as `__pycache__`, `.pytest_cache`, `.ruff_cache`, and `.mypy_cache` are excluded from changed-file summaries.
