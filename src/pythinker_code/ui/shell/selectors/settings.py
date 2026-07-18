"""Interactive `/settings` selector.

Exposes only settings with real backing ``Config`` fields; settings that
require additional runtime plumbing are intentionally deferred rather than
shown as no-op toggles.
"""

from __future__ import annotations

from typing import Any, cast

from pythinker_code.config import AutoUpdateMode, Config
from pythinker_code.llm import derive_model_capabilities
from pythinker_code.thinking import (
    EXTENDED_THINKING_LEVELS,
    effective_config_thinking_effort,
    model_uses_native_thinking,
)
from pythinker_code.ui.shell.components.settings_list import (
    SettingItem,
    SettingsListConfig,
    run_settings_list,
)

_BOOL_VALUES = ("true", "false")
_AUTO_UPDATE_VALUES = tuple(mode.value for mode in AutoUpdateMode)
_NONE_MODEL_VALUE = "(none)"


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _with_current(current: str, values: list[str]) -> list[str]:
    if current not in values:
        return [current, *values]
    return values


def _int_values(current: int, presets: list[int]) -> list[str]:
    values = sorted({current, *presets})
    return [str(value) for value in values]


def _float_values(current: float, presets: list[float]) -> list[str]:
    values = sorted({current, *presets})
    return [f"{value:g}" for value in values]


def _build_settings_config(config: Config) -> SettingsListConfig:
    """Build the settings-list config from a Pythinker ``Config`` object."""
    from pythinker_code.update_policy import auto_update_override_reason

    _auto_update_override = auto_update_override_reason()
    model_values = [_NONE_MODEL_VALUE, *sorted(config.models)]
    current_model = config.default_model or _NONE_MODEL_VALUE
    current_model_cfg = config.models.get(config.default_model) if config.default_model else None
    default_model_uses_native_thinking = bool(
        current_model_cfg
        and model_uses_native_thinking(derive_model_capabilities(current_model_cfg))
    )
    default_thinking_value = (
        "native reasoning"
        if default_model_uses_native_thinking
        else effective_config_thinking_effort(
            config.default_thinking, config.default_thinking_effort
        )
    )
    default_thinking_values = (
        None
        if default_model_uses_native_thinking
        else (
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        )
    )
    default_thinking_description = (
        "Current default model uses built-in reasoning; no effort toggle is available."
        if default_model_uses_native_thinking
        else "Default reasoning effort for thinking-capable models."
    )

    items = [
        SettingItem(
            id="theme",
            label="Theme",
            description="Terminal color theme. Reloads the shell after applying.",
            current_value=config.theme,
            values=("dark", "light", "auto"),
        ),
        SettingItem(
            id="tui.style",
            label="TUI style",
            description=("card uses message/tool cards; pythinker is the legacy worklog UI."),
            current_value=config.tui.style,
            values=("card", "pythinker"),
        ),
        SettingItem(
            id="tui.turn_recaps",
            label="Turn recaps",
            description="Show a compact recap line after completed shell turns.",
            current_value=_bool(config.tui.turn_recaps),
            values=_BOOL_VALUES,
        ),
        SettingItem(
            id="default_model",
            label="Default model",
            description=(
                "Default model key from config.models. Use /model for richer model details."
            ),
            current_value=current_model,
            values=tuple(_with_current(current_model, model_values)),
        ),
        SettingItem(
            id="default_thinking",
            label="Default thinking",
            description=default_thinking_description,
            current_value=default_thinking_value,
            values=default_thinking_values,
        ),
        SettingItem(
            id="show_thinking_stream",
            label="Show thinking stream",
            description="Show raw thinking stream in the live area when models provide it.",
            current_value=_bool(config.show_thinking_stream),
            values=_BOOL_VALUES,
        ),
        SettingItem(
            id="default_yolo",
            label="Default yolo",
            description="Start sessions in auto-approve/yolo mode by default.",
            current_value=_bool(config.default_yolo),
            values=_BOOL_VALUES,
        ),
        SettingItem(
            id="auto_deliberate_destructive_actions",
            label="Destructive deliberation",
            description="Bounce destructive auto-approved actions once for deliberation.",
            current_value=_bool(config.auto_deliberate_destructive_actions),
            values=_BOOL_VALUES,
        ),
        SettingItem(
            id="default_plan_mode",
            label="Default plan mode",
            description="Start sessions in read-only planning mode by default.",
            current_value=_bool(config.default_plan_mode),
            values=_BOOL_VALUES,
        ),
        SettingItem(
            id="skip_auto_prompt_injection",
            label="Skip auto prompt reminder",
            description="Suppress the auto-mode system reminder.",
            current_value=_bool(config.skip_auto_prompt_injection),
            values=_BOOL_VALUES,
        ),
        SettingItem(
            id="telemetry",
            label="Telemetry",
            description="Enable anonymous telemetry to help improve pythinker-code.",
            current_value=_bool(config.telemetry),
            values=_BOOL_VALUES,
        ),
        SettingItem(
            id="auto_update",
            label="Auto-update",
            description=(
                "Startup update policy: off, notify, download (stage in background, "
                "apply on restart), or apply_on_exit."
                if _auto_update_override is None
                else f"Auto-update is {_auto_update_override}; that override outranks this setting."
            ),
            # Show the *effective* state, and make the row read-only when an
            # override (env kill-switch / source checkout) forces it off, so the
            # panel never offers a no-op toggle.
            current_value=(config.auto_update.value if _auto_update_override is None else "off"),
            values=_AUTO_UPDATE_VALUES if _auto_update_override is None else None,
        ),
        SettingItem(
            id="merge_all_available_skills",
            label="Merge all skills",
            description=(
                "Discover skills from all known brand directories instead of first match only."
            ),
            current_value=_bool(config.merge_all_available_skills),
            values=_BOOL_VALUES,
        ),
        SettingItem(
            id="loop_control.max_steps_per_turn",
            label="Max steps per turn",
            description="Maximum agent/tool steps in one turn.",
            current_value=str(config.loop_control.max_steps_per_turn),
            values=tuple(
                _int_values(
                    config.loop_control.max_steps_per_turn,
                    [25, 50, 100, 250, 500, 1000],
                )
            ),
        ),
        SettingItem(
            id="loop_control.max_retries_per_step",
            label="Max retries per step",
            description="Maximum retry attempts for one failed step.",
            current_value=str(config.loop_control.max_retries_per_step),
            values=tuple(_int_values(config.loop_control.max_retries_per_step, [1, 2, 3, 5, 10])),
        ),
        SettingItem(
            id="loop_control.max_ralph_iterations",
            label="Ralph iterations",
            description="Extra iterations after the first Ralph turn. -1 means unlimited.",
            current_value=str(config.loop_control.max_ralph_iterations),
            values=tuple(
                _int_values(config.loop_control.max_ralph_iterations, [-1, 0, 1, 3, 5, 10])
            ),
        ),
        SettingItem(
            id="loop_control.compaction_trigger_ratio",
            label="Compaction trigger ratio",
            description="Context usage ratio that triggers auto-compaction.",
            current_value=f"{config.loop_control.compaction_trigger_ratio:g}",
            values=tuple(
                _float_values(
                    config.loop_control.compaction_trigger_ratio,
                    [0.7, 0.8, 0.85, 0.9, 0.95],
                )
            ),
        ),
        SettingItem(
            id="loop_control.reserved_context_size",
            label="Reserved context size",
            description="Token budget reserved for response generation before compaction.",
            current_value=str(config.loop_control.reserved_context_size),
            values=tuple(
                _int_values(
                    config.loop_control.reserved_context_size,
                    [10_000, 25_000, 50_000, 75_000, 100_000],
                )
            ),
        ),
        SettingItem(
            id="background.max_running_tasks",
            label="Max background tasks",
            description="Maximum simultaneously running background tasks.",
            current_value=str(config.background.max_running_tasks),
            values=tuple(_int_values(config.background.max_running_tasks, [1, 2, 4, 8, 16])),
        ),
        SettingItem(
            id="background.keep_alive_on_exit",
            label="Keep tasks on exit",
            description="Keep background tasks alive when the CLI exits.",
            current_value=_bool(config.background.keep_alive_on_exit),
            values=_BOOL_VALUES,
        ),
        SettingItem(
            id="background.agent_task_timeout_s",
            label="Agent task timeout",
            description="Maximum runtime in seconds for a background agent task.",
            current_value=str(config.background.agent_task_timeout_s),
            values=tuple(
                _int_values(
                    config.background.agent_task_timeout_s,
                    [300, 600, 900, 1800, 3600],
                )
            ),
        ),
        SettingItem(
            id="mcp.client.tool_call_timeout_ms",
            label="MCP tool timeout",
            description="Timeout for MCP tool calls in milliseconds.",
            current_value=str(config.mcp.client.tool_call_timeout_ms),
            values=tuple(
                _int_values(
                    config.mcp.client.tool_call_timeout_ms,
                    [15_000, 30_000, 60_000, 120_000, 300_000],
                )
            ),
        ),
        SettingItem(
            id="default_editor",
            label="External editor",
            description=(
                "Read-only here for now. Use /editor or config.toml to edit arbitrary commands."
            ),
            current_value=config.default_editor or "(default)",
        ),
        SettingItem(
            id="config_file",
            label="Config file",
            description="Read-only path to the loaded configuration file.",
            current_value=str(config.source_file) if config.source_file else "(none)",
        ),
    ]
    return SettingsListConfig(title="Settings", items=items, max_visible=10, enable_search=True)


def apply_settings_changes(config: Config, changes: dict[str, str]) -> list[str]:
    """Apply selector changes to ``config`` and return changed setting ids."""
    changed: list[str] = []

    def mark(setting_id: str) -> None:
        if setting_id not in changed:
            changed.append(setting_id)

    for setting_id, value in changes.items():
        match setting_id:
            case "theme":
                if config.theme != value:
                    config.theme = cast(Any, value)
                    mark(setting_id)
            case "tui.style":
                if config.tui.style != value:
                    config.tui.style = cast(Any, value)
                    mark(setting_id)
            case "tui.turn_recaps":
                new = value == "true"
                if config.tui.turn_recaps != new:
                    config.tui.turn_recaps = new
                    mark(setting_id)
            case "default_model":
                model = "" if value == _NONE_MODEL_VALUE else value
                if config.default_model != model:
                    config.default_model = model
                    mark(setting_id)
            case "default_thinking":
                if value not in EXTENDED_THINKING_LEVELS:
                    continue
                enabled = value != "off"
                if config.default_thinking != enabled or config.default_thinking_effort != value:
                    config.default_thinking = enabled
                    config.default_thinking_effort = value
                    mark(setting_id)
            case "show_thinking_stream":
                new = value == "true"
                if config.show_thinking_stream != new:
                    config.show_thinking_stream = new
                    mark(setting_id)
            case "default_yolo":
                new = value == "true"
                if config.default_yolo != new:
                    config.default_yolo = new
                    mark(setting_id)
            case "auto_deliberate_destructive_actions":
                new = value == "true"
                if config.auto_deliberate_destructive_actions != new:
                    config.auto_deliberate_destructive_actions = new
                    mark(setting_id)
            case "default_plan_mode":
                new = value == "true"
                if config.default_plan_mode != new:
                    config.default_plan_mode = new
                    mark(setting_id)
            case "skip_auto_prompt_injection":
                new = value == "true"
                if config.skip_auto_prompt_injection != new:
                    config.skip_auto_prompt_injection = new
                    mark(setting_id)
            case "telemetry":
                new = value == "true"
                if config.telemetry != new:
                    config.telemetry = new
                    mark(setting_id)
            case "auto_update":
                # Only reached for the live (non-override) row; a read-only row
                # never submits a change.
                new_mode = AutoUpdateMode(value)
                if config.auto_update != new_mode:
                    config.auto_update = new_mode
                    mark(setting_id)
            case "merge_all_available_skills":
                new = value == "true"
                if config.merge_all_available_skills != new:
                    config.merge_all_available_skills = new
                    mark(setting_id)
            case "loop_control.max_steps_per_turn":
                new = int(value)
                if config.loop_control.max_steps_per_turn != new:
                    config.loop_control.max_steps_per_turn = new
                    mark(setting_id)
            case "loop_control.max_retries_per_step":
                new = int(value)
                if config.loop_control.max_retries_per_step != new:
                    config.loop_control.max_retries_per_step = new
                    mark(setting_id)
            case "loop_control.max_ralph_iterations":
                new = int(value)
                if config.loop_control.max_ralph_iterations != new:
                    config.loop_control.max_ralph_iterations = new
                    mark(setting_id)
            case "loop_control.compaction_trigger_ratio":
                new = float(value)
                if config.loop_control.compaction_trigger_ratio != new:
                    config.loop_control.compaction_trigger_ratio = new
                    mark(setting_id)
            case "loop_control.reserved_context_size":
                new = int(value)
                if config.loop_control.reserved_context_size != new:
                    config.loop_control.reserved_context_size = new
                    mark(setting_id)
            case "background.max_running_tasks":
                new = int(value)
                if config.background.max_running_tasks != new:
                    config.background.max_running_tasks = new
                    mark(setting_id)
            case "background.keep_alive_on_exit":
                new = value == "true"
                if config.background.keep_alive_on_exit != new:
                    config.background.keep_alive_on_exit = new
                    mark(setting_id)
            case "background.agent_task_timeout_s":
                new = int(value)
                if config.background.agent_task_timeout_s != new:
                    config.background.agent_task_timeout_s = new
                    mark(setting_id)
            case "mcp.client.tool_call_timeout_ms":
                new = int(value)
                if config.mcp.client.tool_call_timeout_ms != new:
                    config.mcp.client.tool_call_timeout_ms = new
                    mark(setting_id)
            case _:
                # Read-only or unknown field. Ignore to keep older selector
                # rows compatible across config versions.
                continue
    return changed


async def run_settings_selector(config: Config) -> dict[str, str] | None:
    """Open the interactive settings selector and return changed values."""
    result = await run_settings_list(_build_settings_config(config))
    if result is None:
        return None
    return result.changes
