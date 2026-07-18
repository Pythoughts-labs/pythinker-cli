from __future__ import annotations

from pydantic import SecretStr

from pythinker_code.config import Config, LLMModel, LLMProvider
from pythinker_code.ui.shell.components.settings_list import (
    SettingItem,
    SettingsListConfig,
    _SettingsListState,  # type: ignore[reportPrivateUsage]
)
from pythinker_code.ui.shell.selectors.settings import (
    _build_settings_config,  # type: ignore[reportPrivateUsage]
    apply_settings_changes,
)


def test_settings_list_cycles_value_and_tracks_changes():
    state = _SettingsListState(
        SettingsListConfig(
            title="Settings",
            items=[
                SettingItem(
                    id="telemetry",
                    label="Telemetry",
                    current_value="false",
                    values=("true", "false"),
                )
            ],
        )
    )

    assert state.activate() is True
    assert state.values["telemetry"] == "true"
    assert state.changes() == {"telemetry": "true"}


def test_settings_list_read_only_item_does_not_change():
    state = _SettingsListState(
        SettingsListConfig(
            title="Settings",
            items=[SettingItem(id="config_file", label="Config file", current_value="/tmp/x")],
        )
    )

    assert state.activate() is False
    assert state.changes() == {}


def test_settings_list_fuzzy_search_filters_items():
    state = _SettingsListState(
        SettingsListConfig(
            title="Settings",
            items=[
                SettingItem(id="theme", label="Theme", current_value="dark"),
                SettingItem(id="default_model", label="Default model", current_value="gpt"),
            ],
        )
    )

    state.append_search("dm")
    assert [state.config.items[i].id for i in state.visible] == ["default_model"]


def test_build_settings_config_exposes_backed_settings_only():
    config = Config()
    settings = _build_settings_config(config)
    ids = {item.id for item in settings.items}

    assert "theme" in ids
    assert "tui.style" in ids
    assert "default_thinking" in ids
    assert "auto_deliberate_destructive_actions" in ids
    assert "telemetry" in ids
    # No-op image controls are intentionally not exposed yet.
    assert "show-images" not in ids
    assert "image-width-cells" not in ids


def test_build_settings_config_model_values_include_config_models():
    config = Config(
        providers={
            "p": LLMProvider(
                type="openai_legacy",
                base_url="https://example.test",
                api_key=SecretStr("k"),
            )
        },
        models={
            "alpha": LLMModel(provider="p", model="a", max_context_size=1000),
            "beta": LLMModel(provider="p", model="b", max_context_size=1000),
        },
        default_model="beta",
    )

    settings = _build_settings_config(config)
    item = next(item for item in settings.items if item.id == "default_model")
    assert item.current_value == "beta"
    assert item.values is not None
    assert "alpha" in item.values
    assert "beta" in item.values


def test_build_settings_config_labels_native_reasoning_as_read_only():
    config = Config(
        providers={
            "p": LLMProvider(
                type="anthropic",
                base_url="https://example.test",
                api_key=SecretStr("k"),
            )
        },
        models={
            "minimax/m2.7": LLMModel(
                provider="p",
                model="MiniMax-M2.7",
                max_context_size=192_000,
                capabilities={"always_thinking"},
            ),
        },
        default_model="minimax/m2.7",
        default_thinking=False,
        default_thinking_effort="off",
    )

    settings = _build_settings_config(config)
    item = next(item for item in settings.items if item.id == "default_thinking")

    assert item.current_value == "native reasoning"
    assert item.values is None


def test_apply_settings_changes_mutates_config():
    config = Config()

    changed = apply_settings_changes(
        config,
        {
            "theme": "light",
            "default_thinking": "high",
            "telemetry": "true",
            "auto_deliberate_destructive_actions": "true",
            "loop_control.max_retries_per_step": "5",
            "config_file": "/ignored/read-only",
        },
    )

    # telemetry default is True, so passing "true" is a no-op and is correctly
    # omitted from the changed list.
    assert changed == [
        "theme",
        "default_thinking",
        "auto_deliberate_destructive_actions",
        "loop_control.max_retries_per_step",
    ]
    assert config.theme == "light"
    assert config.default_thinking is True
    assert config.telemetry is True
    assert config.auto_deliberate_destructive_actions is True
    assert config.loop_control.max_retries_per_step == 5


def test_settings_list_visible_window_centers_selected_item():
    state = _SettingsListState(
        SettingsListConfig(
            title="Settings",
            max_visible=3,
            items=[SettingItem(id=str(i), label=f"Item {i}", current_value="x") for i in range(6)],
        )
    )

    state.move(3)
    # An overflowing list reserves one row of the budget for the scroll
    # indicator, so the slice spans budget-1 = 2 rows: (start, start+2).
    assert state.visible_window() == (2, 4)


def test_settings_list_overflow_keeps_selected_row_within_window():
    """Regression: navigating an overflowing settings list must not scroll the
    highlighted row under the scroll indicator and off the bottom of the
    (max_visible-tall) window. Mirrors the selector.py viewport guarantee.
    """
    budget = 10
    state = _SettingsListState(
        SettingsListConfig(
            title="Settings",
            max_visible=budget,
            items=[
                SettingItem(id=str(i), label=f"Item {i:02}", current_value="x") for i in range(25)
            ],
        )
    )

    for target in range(len(state.visible)):
        state.selected_idx = target
        start, end = state.visible_window()
        has_scroll_row = start > 0 or end < len(state.visible)
        content_rows = (end - start) + (1 if has_scroll_row else 0)
        assert start <= target < end, f"selected {target} fell outside window {(start, end)}"
        assert content_rows <= budget, f"content {content_rows} exceeds budget {budget}"


def _item(settings: SettingsListConfig, item_id: str) -> SettingItem | None:
    return next((item for item in settings.items if item.id == item_id), None)


def test_settings_exposes_auto_update_toggle_when_live(monkeypatch):
    from pythinker_code import update_policy
    from pythinker_code.config import AutoUpdateMode

    monkeypatch.setattr(update_policy, "auto_update_override_reason", lambda: None)
    config = Config()
    config.auto_update = AutoUpdateMode.DOWNLOAD

    item = _item(_build_settings_config(config), "auto_update")

    assert item is not None
    assert item.values == ("off", "notify", "download", "apply_on_exit")  # selectable
    assert item.current_value == "download"


def test_settings_auto_update_readonly_under_override(monkeypatch):
    from pythinker_code import update_policy

    monkeypatch.setattr(
        update_policy,
        "auto_update_override_reason",
        lambda: "disabled by PYTHINKER_CLI_NO_AUTO_UPDATE",
    )
    from pythinker_code.config import AutoUpdateMode

    config = Config()
    # Stored download, but the override forces the effective policy off.
    config.auto_update = AutoUpdateMode.DOWNLOAD

    item = _item(_build_settings_config(config), "auto_update")

    assert item is not None
    assert item.values is None  # read-only: no no-op toggle
    assert item.current_value == "off"  # shows the effective state
    assert "PYTHINKER_CLI_NO_AUTO_UPDATE" in item.description


def test_apply_settings_changes_sets_auto_update():
    from pythinker_code.config import AutoUpdateMode

    config = Config()  # auto_update defaults to download

    changed = apply_settings_changes(config, {"auto_update": "notify"})

    assert changed == ["auto_update"]
    assert config.auto_update is AutoUpdateMode.NOTIFY
