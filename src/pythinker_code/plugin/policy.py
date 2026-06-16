"""Session-scoped plugin activation policy.

The policy decides whether external (Claude/Codex) plugins are activated and
which plugins are enabled. It is set once at startup from config and read by the
artifact collectors in :mod:`pythinker_code.plugin.integration`. A ``ContextVar``
(not a plain global) carries it, mirroring the session-id ContextVar pattern, so
it propagates to subagent tasks and is safe under concurrency.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field


def _empty_options() -> dict[str, dict[str, object]]:
    return {}


@dataclass(frozen=True)
class PluginPolicy:
    """Which plugins a session activates.

    External (Claude/Codex) plugins are auto-detected by default for their *safe*
    artifacts — skills, commands, agents — which are model-invoked, never
    auto-run. Their *executable* artifacts — hooks and MCP servers — auto-run, so
    they stay opt-in behind ``external_exec``.
    """

    discover_external: bool = True
    external_exec: bool = False
    # None enables all discovered plugins; a frozenset enables only those named.
    enabled: frozenset[str] | None = None
    # Names explicitly turned off; excluded even when ``enabled`` would allow them.
    # This is how "disable" works under auto-detect (all-on) defaults.
    disabled: frozenset[str] = frozenset()
    # Per-plugin user-config values ({plugin_name: {option_key: value}}), filled
    # into ``${user_config.KEY}`` references in MCP/hook artifacts.
    options: dict[str, dict[str, object]] = field(default_factory=_empty_options)


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


def policy_from_config(
    discover_external: bool,
    external_exec: bool,
    enabled: list[str],
    disabled: list[str] | None = None,
    options: dict[str, dict[str, object]] | None = None,
) -> PluginPolicy:
    """Build a :class:`PluginPolicy` from config values.

    Blank/whitespace ``enabled`` entries are dropped so a stray ``[""]`` cannot
    silently disable every plugin: an empty or all-blank list means "enable all"
    (``None``), never "enable none". Blanks in ``disabled`` are dropped too.
    """
    names = frozenset(name.strip() for name in enabled if name.strip())
    off = frozenset(name.strip() for name in (disabled or []) if name.strip())
    return PluginPolicy(
        discover_external=discover_external,
        external_exec=external_exec,
        enabled=names or None,
        disabled=off,
        options=options or {},
    )
