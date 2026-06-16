"""Plugin dependency resolution (apt-style presence checks, not an import graph).

A dependency is a *presence guarantee*: a plugin declaring ``dependencies`` is
only activated when each dependency is also present and enabled. Mirrors the
reference design, adapted to pythinker's discovery model where a plugin is
identified by its bare ``name`` (discovery de-dups by name, so a name is unique).
Marketplace qualifiers (``name@marketplace``) are accepted in manifests but
matched by name at the discovery layer, which does not track marketplace origin.

This module is pure: no I/O, no mutation of its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass


def parse_plugin_identifier(plugin: str) -> tuple[str, str | None]:
    """Split ``"name"`` or ``"name@marketplace"`` (only the first ``@`` separates)."""
    if "@" in plugin:
        name, _, marketplace = plugin.partition("@")
        return name, marketplace or None
    return plugin, None


@dataclass(frozen=True)
class DependencyIssue:
    """One unsatisfied dependency that caused a plugin to be demoted."""

    plugin: str
    dependency: str
    reason: str  # "not-enabled" (known but off) | "not-found" (absent everywhere)

    def message(self) -> str:
        if self.reason == "not-enabled":
            return (
                f'Plugin "{self.plugin}" disabled: dependency "{self.dependency}" '
                "is installed but not enabled."
            )
        return (
            f'Plugin "{self.plugin}" disabled: dependency "{self.dependency}" '
            "was not found in any configured plugin."
        )


def verify_and_demote(
    names_with_deps: list[tuple[str, list[str]]], enabled: set[str]
) -> tuple[set[str], list[DependencyIssue]]:
    """Disable plugins whose declared dependencies are not satisfied.

    Fixed-point: demoting a plugin can break a dependent that required it, so the
    scan repeats until no further demotions occur (monotone — a plugin only ever
    leaves the enabled set — so it terminates).

    Args:
        names_with_deps: ``(plugin_name, [dependency_ref, ...])`` for every known
            plugin (enabled or not). Dependency refs are matched by name.
        enabled: names currently enabled (subject to demotion).

    Returns the set of demoted names and one :class:`DependencyIssue` per
    unsatisfied dependency. ``reason`` distinguishes a dependency that is known
    (installed) but disabled from one that is absent entirely.
    """
    known = {name for name, _ in names_with_deps}
    active = set(enabled)
    demoted: set[str] = set()
    issues: list[DependencyIssue] = []

    changed = True
    while changed:
        changed = False
        for name, deps in names_with_deps:
            if name not in active:
                continue
            for dep in deps:
                dep_name = parse_plugin_identifier(dep)[0]
                if dep_name in active:
                    continue
                active.discard(name)
                demoted.add(name)
                issues.append(
                    DependencyIssue(
                        plugin=name,
                        dependency=dep,
                        reason="not-enabled" if dep_name in known else "not-found",
                    )
                )
                changed = True
                break  # stop scanning this plugin's deps; restart the outer pass

    return demoted, issues
