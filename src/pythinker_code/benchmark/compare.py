from __future__ import annotations

from collections.abc import Sequence

from pythinker_code.benchmark.commands import BenchmarkReportRow


def readiness_warnings(rows: Sequence[BenchmarkReportRow]) -> list[str]:
    if not rows:
        return []
    warnings: list[str] = []
    models = {str(row.run.get("model_key") or "unknown") for row in rows}
    repeats = {int(row.run.get("repeat_index") or 1) for row in rows}
    suites = {str(row.run.get("suite_name") or "") for row in rows}
    if len(models) < 2:
        warnings.append("Single model only: do not describe this as a model comparison.")
    if len(repeats) < 2:
        warnings.append(
            "Single repeat only: report this as a smoke result, not a stable estimate."
        )
    if any(suite == "pythinker-core" or suite.startswith("swe:") for suite in suites):
        warnings.append("Local fixture scope: this is not a full SWE-bench Docker evaluation.")
    if any(_missing_cost(row) for row in rows):
        warnings.append("Cost unavailable for at least one run: omit cost-efficiency claims.")
    if any(_dirty(row) is None for row in rows):
        warnings.append(
            "Dirty git worktree metadata missing: reproducibility claims may be incomplete."
        )
    if any(_dirty(row) is True for row in rows):
        warnings.append(
            "Dirty git worktree recorded: include the diff or rerun from a clean commit."
        )
    return warnings


def _missing_cost(row: BenchmarkReportRow) -> bool:
    usage = row.summary.get("usage")
    if not isinstance(usage, dict):
        return True
    return usage.get("estimated_cost_usd") is None


def _dirty(row: BenchmarkReportRow) -> bool | None:
    environment = row.summary.get("environment")
    if not isinstance(environment, dict):
        return None
    value = environment.get("git_dirty")
    return value if isinstance(value, bool) else None
