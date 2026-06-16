"""Plugin user-config value substitution.

Plugins may declare ``userConfig`` options whose values the user fills in (via
``[plugins.options.<plugin>]`` in config) and references as ``${user_config.KEY}``
in executable artifacts — MCP server configs and hook commands. Substitution
mirrors the reference: a referenced key with no value is an error, so the caller
skips that artifact rather than running it with a silent blank.

Deferred (each needs changes outside the plugin package):
  * substitution in skill/agent/command *content* — those artifacts don't carry
    their originating plugin id, so the value source can't be resolved there yet;
  * the ``PYTHINKER_PLUGIN_OPTION_*`` hook environment variables — ``HookDef`` has
    no ``env`` field;
  * keychain-backed storage for ``sensitive`` options — values come from config
    today;
  * the interactive enable-time prompt + typed-schema validation.
See docs/en/customization/plugins.md.
"""

from __future__ import annotations

import re

_USER_CONFIG_VAR = re.compile(r"\$\{user_config\.([^}]+)\}")


class UserConfigError(KeyError):
    """A ``${user_config.KEY}`` referenced a value that is not configured."""


def substitute_user_config_vars(text: str, values: dict[str, object]) -> str:
    """Replace ``${user_config.KEY}`` in *text* with the configured value.

    Values are coerced with ``str()``. Raises :class:`UserConfigError` on the
    first key with no configured value — callers skip the affected artifact
    instead of running it with a blank.
    """

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise UserConfigError(key)
        return str(values[key])

    return _USER_CONFIG_VAR.sub(_replace, text)
