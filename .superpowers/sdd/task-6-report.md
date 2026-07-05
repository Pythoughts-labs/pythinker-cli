# Task 6 Report: Safe Online Source Discovery

## Summary

- Added `src/pythinker_code/benchmark/discovery.py` with allowlisted stdlib-only discovery.
- Wired `/benchmark discover --source <allowlisted> --difficulty hard --limit 5` through the existing benchmark parser and dispatcher.
- Discovery records are always `trusted=False`.
- `--output` writes only when the path suffix is `.jsonl`; benchmark artifact roots are not reused for discovery output.
- Added DeepSWE to the allowlist after checking the official page listed 113 long-horizon tasks updated July 1, 2026.

## Current Sources Checked

- Terminal-Bench: https://www.tbench.ai/
- SWE-bench: https://www.swebench.com/
- CodeClash: https://github.com/codeclash-ai/codeclash and https://codeclash.ai/
- DeepSWE: https://deepswe.datacurve.ai/

Fetched content was treated as untrusted text only.

## TDD Evidence

RED:

```bash
uv run pytest tests/core/test_benchmark_discovery.py -q
```

Result: failed during collection because `discover_benchmark` / discovery support did not exist yet.

GREEN:

```bash
uv run pytest tests/core/test_benchmark_discovery.py tests/core/test_benchmark_slash.py -q
```

Result: 28 passed, 1 existing Loguru deprecation warning.

## Verification

```bash
uv run ruff check src/pythinker_code/benchmark/commands.py src/pythinker_code/benchmark/discovery.py tests/core/test_benchmark_discovery.py
uv run ruff format --check src/pythinker_code/benchmark/commands.py src/pythinker_code/benchmark/discovery.py tests/core/test_benchmark_discovery.py
uv run pyright src/pythinker_code/benchmark/commands.py src/pythinker_code/benchmark/discovery.py tests/core/test_benchmark_discovery.py
uv run ty check
uv run python - <<'PY'
from pythinker_code.benchmark.discovery import discover_benchmark_sources
for source in ("terminal-bench", "deepswe"):
    tasks = discover_benchmark_sources(source=source, difficulty="hard", limit=1)
    print(source, len(tasks), tasks[0].trusted if tasks else "none")
PY
```

Results: focused Ruff, format, touched-file Pyright, ty, and live allowlist smoke passed.

Full `make check-pythinker-code` did not complete because Ruff stopped on a pre-existing import-order issue in `src/pythinker_code/benchmark/runner.py`, which was outside Task 6 and left untouched. Full `uv run pyright` also reports pre-existing errors in `benchmark/compare.py`, `benchmark/report.py`, and `tests/core/test_benchmark_records.py`; touched-file Pyright passed.
