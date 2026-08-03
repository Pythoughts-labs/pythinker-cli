# Contributing to Pythinker CLI

Thank you for being interested in contributing to Pythinker CLI!

We welcome all kinds of contributions, including bug fixes, features, document improvements, typo fixes, etc. To maintain a high-quality codebase and user experience, we provide the following guidelines for contributions:

1. We only merge pull requests that align with our roadmap. For any pull request that introduces changes larger than 100 lines of code, we highly recommend discussing with us by [raising an issue](https://github.com/Pythoughts-labs/pythinker-cli/issues) or in an existing issue before you start working on it. Otherwise your pull request may be closed or ignored without review.
2. We insist on high code quality. Please ensure your code is as good as, if not better than, the code written by frontier coding agents. Changes may be requested before your pull request can be merged.

## Prek hooks

We use [prek](https://github.com/j178/prek) to run formatting and checks via git hooks.

Recommended setup:
1. Run `make prepare` to sync dependencies and install the prek hooks.
2. Optionally run on all files before sending a PR: `prek run --all-files`.

Manual setup (if you do not want to use `make prepare`):
1. Install prek (pick one): `uv tool install prek`, `pipx install prek`, or `pip install prek`.
2. Install the hooks in this repo: `prek install`.

After installation, the hooks run on every commit. The repo uses prek workspace mode, so only the
projects with changed files run their hooks. You can skip them for an intermediate commit with
`git commit --no-verify`, or run them manually with `prek run --all-files`.

The hooks execute the relevant `make format-*` and `make check-*` targets, so ensure dependencies
are installed (`make prepare` or `uv sync`).

## Adding dependencies

This project enforces a **zero-new-bundled-deps** policy: new entries under `[project].dependencies`
in `pyproject.toml` are not accepted unless explicitly approved by a maintainer. Use the standard
library or existing internal modules first; otherwise request an approved exception from a maintainer before adding a package.

If you believe a new runtime dependency is genuinely necessary:

1. Open an issue explaining why no stdlib alternative exists, before writing code.
2. In your PR, add an inline justification comment next to the new `pyproject.toml` entry:

   ```toml
   # Justification: <why no stdlib/hand-rolled alternative exists>
   # Security review: <known CVEs or supply-chain notes, or "none found">
   # Approved by: <link to issue or maintainer sign-off>
   "new-package>=x.y",
   ```

3. The `zero-new-bundled-deps` path instruction in `.coderabbit.yaml` will flag the change
   automatically so reviewers know to look for the justification.

Dev-only dependencies under `[dependency-groups]` are not subject to this policy.

## Tool-execution characterization

Changes to `soul/tool_execution.py`, execution scheduling, deduplication, the reader/writer gate, or
MCP publication should run the deterministic local characterization harness:

```bash
uv run python scripts/benchmark_toolset.py --scenario all --runs 5 --output before-or-after.json
```

For before/after evidence, use the same machine, Python environment, fixture matrix, warm-up count,
and five-run command at both revisions. Keep the raw JSON and report timing regressions as well as
improvements; do not discard samples or use `--smoke` for final evidence. Confirm every scenario has
zero leaked tasks/processes/sessions, all cancellation and recovery flags are true, registry hashes
are stable, and deterministic decision states are derived by the harness. Treat results as directional
local engineering evidence, not universal product telemetry.
