"""Which subagent types get the git-context prompt prefix.

Reviewer-class agents need branch/dirty/merge-base orientation as much as
explore does — without it every review run burns turns rediscovering its
diff scope from a bare prompt.
"""

from __future__ import annotations

from pythinker_code.subagents.core import GIT_CONTEXT_AGENT_TYPES
from pythinker_code.subagents.review_target import REVIEWER_AGENT_TYPES


def test_explore_and_reviewer_types_receive_git_context() -> None:
    assert frozenset({"explore"}) | REVIEWER_AGENT_TYPES == GIT_CONTEXT_AGENT_TYPES


def test_gate_names_match_registered_profile_keys() -> None:
    """The gate is keyed on spec.type_def.name; a name that is not also a
    profile key would silently never match a real agent type."""
    from pythinker_code.soul.permission import _SUBAGENT_PROFILES

    unmatched = GIT_CONTEXT_AGENT_TYPES - set(_SUBAGENT_PROFILES)
    assert unmatched == set(), unmatched


def test_write_capable_types_do_not() -> None:
    assert {"coder", "implementer", "agent"}.isdisjoint(GIT_CONTEXT_AGENT_TYPES)
