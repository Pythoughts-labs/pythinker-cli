"""Session-scoped plugin activation policy.

The policy decides whether external (Claude/Codex) plugins are activated and
which plugins are enabled. It is set once at startup from config and read by the
artifact collectors in :mod:`pythinker_code.plugin.integration`. A ``ContextVar``
(not a plain global) carries it, mirroring the session-id ContextVar pattern, so
it propagates to subagent tasks and is safe under concurrency.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class PluginPolicy:
    """Which plugins a session activates."""

    include_external: bool = False
    # None enables all discovered plugins; a frozenset enables only those named.
    enabled: frozenset[str] | None = None


_DEFAULT_POLICY = PluginPolicy()
_current_policy: ContextVar[PluginPolicy] = ContextVar("plugin_policy", default=_DEFAULT_POLICY)


def current_plugin_policy() -> PluginPolicy:
    """The active plugin policy (defaults to pythinker-only, all enabled)."""
    return _current_policy.get()


def set_plugin_policy(policy: PluginPolicy) -> Token[PluginPolicy]:
    """Install *policy* for the current context; returns a token for reset."""
    return _current_policy.set(policy)


def reset_plugin_policy(token: Token[PluginPolicy]) -> None:
    """Restore the policy replaced by :func:`set_plugin_policy`."""
    _current_policy.reset(token)


def policy_from_config(include_external: bool, enabled: list[str]) -> PluginPolicy:
    """Build a :class:`PluginPolicy` from config values."""
    return PluginPolicy(
        include_external=include_external,
        enabled=frozenset(enabled) if enabled else None,
    )
