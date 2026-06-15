from __future__ import annotations

from pathlib import Path

INIT = (Path(__file__).parent / "init.md").read_text(encoding="utf-8")
COMPACT = (Path(__file__).parent / "compact.md").read_text(encoding="utf-8")
BEST_PRACTICES = (Path(__file__).parent / "best_practices.md").read_text(encoding="utf-8")
LEARN = (Path(__file__).parent / "learn.md").read_text(encoding="utf-8")
GOAL_SET = (Path(__file__).parent / "goal_set.md").read_text(encoding="utf-8")
GOAL_CONTINUATION = (Path(__file__).parent / "goal_continuation.md").read_text(encoding="utf-8")
GOAL_WRAP_UP = (Path(__file__).parent / "goal_wrap_up.md").read_text(encoding="utf-8")
BUDGET_CONTINUATION_NUDGE = (
    "The session has crossed {ratio:.0%} of its configured spend ceiling "
    "(estimated ${spent:.2f} of ${ceiling:.2f}). Continue only if the remaining work is "
    "worth the budget; prefer summarizing progress or asking the user before expensive next steps."
)


def apply_always_on_best_practices(system_prompt: str, *, enabled: bool) -> str:
    """Append the full best-practices guidance to a system prompt when enabled.

    The `/best-practices` profile is normally opt-in per session. When the
    ``best_practices_always`` config flag is set, the root session folds it into
    the system prompt at startup so the guardrails apply without running the
    command. The profile's opening line is phrased for the manual command ("The
    user ran ``/best-practices``."), so strip that lead-in for the always-on path.
    """
    if not enabled:
        return system_prompt
    guidance = BEST_PRACTICES.replace("The user ran `/best-practices`. ", "", 1)
    return f"{system_prompt}\n\n{guidance}"
