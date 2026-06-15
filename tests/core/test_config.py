from __future__ import annotations

from pathlib import Path

import pytest
import tomlkit
from inline_snapshot import snapshot

from pythinker_code.config import (
    Config,
    _apply_env_vars,
    _check_scope_locks,
    _load_scoped,
    _lookup_provenance,
    _set_nested,
    _type_based_merge,
    find_project_root,
    get_default_config,
    load_config,
    load_config_from_string,
)
from pythinker_code.exception import ConfigError


def test_default_config():
    config = get_default_config()
    assert config == snapshot(Config())


def test_default_config_dump():
    config = get_default_config()
    assert config.model_dump() == snapshot(
        {
            "default_model": "",
            "default_thinking": False,
            "default_thinking_effort": None,
            "agent_execution_profile": "default",
            "default_yolo": False,
            "best_practices_always": False,
            "ask_user_question_policy": "ask_except_auto",
            "auto_deliberate_destructive_actions": False,
            "default_plan_mode": False,
            "default_editor": "",
            "theme": "dark",
            "show_thinking_stream": True,
            "prevent_idle_sleep": False,
            "auto_update": True,
            "models": {},
            "providers": {},
            "loop_control": {
                "max_steps_per_turn": 1000,
                "max_consecutive_failures": 8,
                "max_truncation_recoveries": 3,
                "max_session_cost_usd": None,
                "budget_nudge_ratio": 0.75,
                "max_retries_per_step": 3,
                "max_ralph_iterations": 0,
                "reserved_context_size": 50000,
                "compaction_trigger_ratio": 0.85,
                "prune_trigger_ratio": 0.7,
                "prune_protect_last": 20,
                "prune_min_chars": 2000,
                "prune_tool_result_max_chars": 0,
            },
            "background": {
                "max_running_tasks": 4,
                "task_retention_days": 7,
                "read_max_bytes": 30000,
                "notification_tail_lines": 20,
                "notification_tail_chars": 3000,
                "wait_poll_interval_ms": 500,
                "worker_heartbeat_interval_ms": 5000,
                "worker_stale_after_ms": 15000,
                "kill_grace_period_ms": 2000,
                "max_output_bytes": 52428800,
                "keep_alive_on_exit": False,
                "agent_task_timeout_s": 3600,
                "print_wait_ceiling_s": 3600,
            },
            "goal": {"auto_continue": False, "max_continuations": 3},
            "compact_prompt": None,
            "model_switch_carryover": True,
            "notifications": {
                "claim_stale_after_ms": 15000,
            },
            "services": {"pythinker_ai_search": None, "pythinker_ai_fetch": None},
            "mcp": {"client": {"tool_call_timeout_ms": 60000, "startup_timeout_ms": 30000}},
            "memory": {
                "lexical_recall": True,
                "injection_bus": True,
                "injection_ceiling_tokens": 2048,
                "harvest_on_compaction": False,
                "harvest_on_stop": False,
                "journal_recaps": False,
                "consolidation": False,
                "durable_memory": False,
            },
            "web": {"allowed_domains": None},
            "feedback": {
                "endpoint_url": "",
                "api_key": None,
                "custom_headers": None,
                "github_client_id": "",
                "github_repo": "Pythoughts-labs/pythinker-code",
            },
            "hooks": [],
            "merge_all_available_skills": True,
            "extra_skill_dirs": [],
            "telemetry": True,
            "session_retention_days": 30,
            "skip_auto_prompt_injection": False,
            "tui": {
                "style": "card",
                "prompt_history_enabled": True,
                "turn_recaps": False,
                "code_theme": "catppuccin-adaptive",
                "statusline": {
                    "enabled": True,
                    "segments": [
                        "spinner",
                        "model",
                        "cost",
                        "speed",
                        "effort",
                        "cwd",
                        "git",
                        "diff",
                        "flags",
                        "context",
                        "elapsed",
                        "clock",
                    ],
                    "command": None,
                    "command_timeout_ms": 1000,
                    "style": "fancy",
                    "bar_width": 10,
                    "cost_budget": None,
                },
                "smooth_streaming": True,
            },
        }
    )


def test_turn_recaps_default_off():
    assert get_default_config().tui.turn_recaps is False


def test_config_source_scopes_default_empty():
    config = get_default_config()
    assert config.source_scopes == {}


def test_config_source_scopes_not_in_dump():
    config = get_default_config()
    dumped = config.model_dump()
    assert "source_scopes" not in dumped


def test_theme_accepts_auto():
    config = load_config_from_string('theme = "auto"\n')
    assert config.theme == "auto"


def test_load_config_text_toml():
    config = load_config_from_string('default_model = ""\n')
    assert config == get_default_config()


def test_load_config_text_json():
    config = load_config_from_string('{"default_model": ""}')
    assert config == get_default_config()


def test_load_config_migrates_legacy_feedback_repo_default():
    old_owner = "mohamed-elkholy95"
    config = load_config_from_string(f'[feedback]\ngithub_repo = "{old_owner}/Pythinker-Code"\n')

    assert config.feedback.github_repo == "Pythoughts-labs/pythinker-code"


def test_load_config_migrates_legacy_org_feedback_repo_default():
    config = load_config_from_string('[feedback]\ngithub_repo = "TechMatrix-labs/pythinker-code"\n')

    assert config.feedback.github_repo == "Pythoughts-labs/pythinker-code"


def test_agent_execution_profile_autonomous_sets_autonomy_defaults():
    config = load_config_from_string('agent_execution_profile = "autonomous_coding"')

    assert config.default_yolo is True
    assert config.ask_user_question_policy == "never"
    assert config.auto_deliberate_destructive_actions is True


def test_agent_execution_profile_respects_explicit_values():
    config = load_config_from_string(
        "\n".join(
            [
                'agent_execution_profile = "autonomous_coding"',
                "default_yolo = false",
                'ask_user_question_policy = "always"',
                "auto_deliberate_destructive_actions = false",
            ]
        )
    )

    assert config.default_yolo is False
    assert config.ask_user_question_policy == "always"
    assert config.auto_deliberate_destructive_actions is False


def test_agent_execution_profile_plan_only_sets_plan_defaults():
    config = load_config_from_string('agent_execution_profile = "plan_only"')

    assert config.default_plan_mode is True
    assert config.ask_user_question_policy == "always"


def test_load_config_sets_source_file(tmp_path):
    config_file = tmp_path / "custom.toml"

    config = load_config(config_file)

    assert config.source_file == config_file.resolve()
    assert not config.is_from_default_location


def test_load_config_text_has_no_source_file():
    config = load_config_from_string('{"default_model": ""}')

    assert config.source_file is None


def test_load_config_text_invalid():
    with pytest.raises(ConfigError, match="Invalid configuration text"):
        load_config_from_string("not valid {")


def test_load_config_invalid_ralph_iterations():
    with pytest.raises(ConfigError, match="max_ralph_iterations"):
        load_config_from_string('{"loop_control": {"max_ralph_iterations": -2}}')


def test_load_config_reserved_context_size():
    config = load_config_from_string('{"loop_control": {"reserved_context_size": 30000}}')
    assert config.loop_control.reserved_context_size == 30000


def test_load_config_max_steps_per_turn():
    config = load_config_from_string("[loop_control]\nmax_steps_per_turn = 42\n")
    assert config.loop_control.max_steps_per_turn == 42


def test_load_config_corrupt_legacy_json_is_backed_up_and_replaced(tmp_path, monkeypatch):
    """Corrupt (non-JSON) legacy config: backup + use defaults, no silent data loss."""
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    legacy_config = tmp_path / "config.json"
    legacy_config.write_text(
        '{"providers":{"custom":{"apiKey":null}}',  # unclosed brace — invalid JSON
        encoding="utf-8",
    )

    config = load_config()

    # Config should be defaults (created fresh), JSON backed up, TOML written.
    assert config.model_dump(
        exclude={"is_from_default_location", "source_file"}
    ) == get_default_config().model_dump(exclude={"is_from_default_location", "source_file"})
    assert not legacy_config.exists()
    assert (tmp_path / "config.json.bak").exists()
    assert (tmp_path / "config.toml").exists()


def test_load_config_incompatible_legacy_json_is_preserved_and_rejected(tmp_path, monkeypatch):
    """Incompatible (valid JSON but schema mismatch) legacy config: preserved, error raised."""
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    legacy_config = tmp_path / "config.json"
    legacy_config.write_text(
        '{"loop_control": {"max_ralph_iterations": -2}}',  # valid JSON, invalid schema
        encoding="utf-8",
    )

    # Should raise ConfigError with details instead of silently using defaults.
    from pythinker_code.exception import ConfigError

    with pytest.raises(ConfigError, match="max_ralph_iterations"):
        load_config()

    # Legacy file and TOML must both remain intact — no silent backup/replace.
    assert legacy_config.exists()
    assert not (tmp_path / "config.toml").exists()


def test_load_config_max_steps_per_run():
    config = load_config_from_string('{"loop_control": {"max_steps_per_run": 7}}')
    assert config.loop_control.max_steps_per_turn == 7


def test_load_config_reserved_context_size_too_low():
    with pytest.raises(ConfigError, match="reserved_context_size"):
        load_config_from_string('{"loop_control": {"reserved_context_size": 500}}')


def test_load_config_compaction_trigger_ratio():
    config = load_config_from_string('{"loop_control": {"compaction_trigger_ratio": 0.8}}')
    assert config.loop_control.compaction_trigger_ratio == 0.8


def test_load_config_compaction_trigger_ratio_default():
    config = load_config_from_string("{}")
    assert config.loop_control.compaction_trigger_ratio == 0.85


def test_load_config_compaction_trigger_ratio_too_low():
    with pytest.raises(ConfigError, match="compaction_trigger_ratio"):
        load_config_from_string('{"loop_control": {"compaction_trigger_ratio": 0.3}}')


def test_load_config_compaction_trigger_ratio_too_high():
    with pytest.raises(ConfigError, match="compaction_trigger_ratio"):
        load_config_from_string('{"loop_control": {"compaction_trigger_ratio": 1.0}}')


def test_auto_deliberate_is_a_valid_policy() -> None:
    c = Config(ask_user_question_policy="auto_deliberate")
    assert c.ask_user_question_policy == "auto_deliberate"


def testfind_project_root_finds_git_root(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    subdir = tmp_path / "src" / "pkg"
    subdir.mkdir(parents=True)
    assert find_project_root(subdir) == tmp_path


def testfind_project_root_returns_none_outside_git(tmp_path):
    # tmp_path itself has no .git ancestor in practice
    assert find_project_root(tmp_path) is None


def testfind_project_root_finds_root_in_cwd(tmp_path):
    (tmp_path / ".git").mkdir()
    assert find_project_root(tmp_path) == tmp_path


def test_set_nested_flat():
    d: dict = {}
    _set_nested(d, ("theme",), "light")
    assert d == {"theme": "light"}


def test_set_nested_deep():
    d: dict = {}
    _set_nested(d, ("tui", "style"), "card")
    assert d == {"tui": {"style": "card"}}


def test_set_nested_overwrites_existing():
    d = {"tui": {"style": "pythinker", "smooth_streaming": True}}
    _set_nested(d, ("tui", "style"), "card")
    assert d["tui"]["style"] == "card"
    assert d["tui"]["smooth_streaming"] is True  # sibling preserved


def test_lookup_provenance_scalar():
    prov = {"theme": ".pythinker/config.local.toml"}
    assert _lookup_provenance(prov, ("theme",)) == ".pythinker/config.local.toml"


def test_lookup_provenance_nested():
    prov = {"tui": {"style": ".pythinker/config.toml"}}
    assert _lookup_provenance(prov, ("tui", "style")) == ".pythinker/config.toml"


def test_lookup_provenance_list_index():
    # Pydantic gives loc=("hooks", 0, "command") for a bad list element.
    # Should return the collection scope, not crash.
    prov = {"hooks": "~/.pythinker/config.toml+.pythinker/config.toml"}
    assert (
        _lookup_provenance(prov, ("hooks", 0, "command"))
        == "~/.pythinker/config.toml+.pythinker/config.toml"
    )


def test_lookup_provenance_partial_path():
    prov = {"tui": {"style": ".pythinker/config.toml"}}
    assert _lookup_provenance(prov, ("tui", "nonexistent")) == "unknown scope"


def test_lookup_provenance_empty_loc():
    prov = "~/.pythinker/config.toml"
    assert _lookup_provenance(prov, ()) == "~/.pythinker/config.toml"


def test_lookup_provenance_unknown():
    prov: dict = {}
    assert _lookup_provenance(prov, ("missing_key",)) == "unknown scope"


def test_scope_lock_providers_in_project():
    with pytest.raises(ConfigError, match="'providers'.*project scope"):
        _check_scope_locks({"providers": {"openai": {}}}, ".pythinker/config.toml")


def test_scope_lock_services_in_local():
    with pytest.raises(ConfigError, match="'services'.*local scope"):
        _check_scope_locks(
            {"services": {"pythinker_ai_search": {}}}, ".pythinker/config.local.toml"
        )


def test_scope_lock_feedback_api_key():
    with pytest.raises(ConfigError, match="'feedback.api_key'"):
        _check_scope_locks({"feedback": {"api_key": "secret"}}, ".pythinker/config.toml")


def test_scope_lock_feedback_url_allowed():
    # feedback.endpoint_url is NOT locked — should not raise
    _check_scope_locks(
        {"feedback": {"endpoint_url": "https://internal.example.com"}},
        ".pythinker/config.toml",
    )


def test_scope_lock_statusline_command_in_project():
    with pytest.raises(ConfigError, match="'tui.statusline.command'.*project scope"):
        _check_scope_locks(
            {"tui": {"statusline": {"command": "/tmp/evil-binary"}}},
            ".pythinker/config.toml",
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", True),
        ("segments", ["cwd", "git", "command"]),
        ("command_timeout_ms", 60000),
    ],
)
def test_scope_lock_statusline_run_knobs_in_project(field, value):
    """A repo-controlled config must not be able to CAUSE the user's status
    command to run, only the user may: `command` chooses the binary, and
    `enabled`/`segments`/`command_timeout_ms` are the knobs that trigger or
    extend its execution."""
    with pytest.raises(ConfigError, match=f"'tui.statusline.{field}'.*project scope"):
        _check_scope_locks(
            {"tui": {"statusline": {field: value}}},
            ".pythinker/config.toml",
        )


def test_scope_lock_statusline_cosmetic_fields_allowed():
    # Purely cosmetic statusline fields stay project-configurable; only the
    # execution-relevant knobs are user-scope-only.
    _check_scope_locks(
        {"tui": {"statusline": {"style": "plain", "bar_width": 8}}},
        ".pythinker/config.toml",
    )


def test_scope_lock_clean_dict():
    _check_scope_locks({"theme": "light", "default_model": "gpt-4"}, ".pythinker/config.toml")


def test_scope_lock_error_points_to_user_config():
    # No locked path has an env override, so the error must point at the user
    # config file — not at a "corresponding PYTHINKER_*" var that doesn't exist.
    with pytest.raises(ConfigError, match=r"Move it to ~/\.pythinker/config\.toml\."):
        _check_scope_locks({"providers": {}}, ".pythinker/config.toml")


def test_merge_scalar_override():
    prov: dict = {}
    result = _type_based_merge(
        {"theme": "dark"}, {"theme": "light"}, prov, ".pythinker/config.local.toml"
    )
    assert result["theme"] == "light"
    assert prov["theme"] == ".pythinker/config.local.toml"


def test_merge_scalar_three_scopes():
    prov: dict = {}
    base = _type_based_merge({}, {"theme": "dark"}, prov, "~/.pythinker/config.toml")
    base = _type_based_merge(base, {"theme": "solarized"}, prov, ".pythinker/config.toml")
    base = _type_based_merge(base, {"theme": "light"}, prov, ".pythinker/config.local.toml")
    assert base["theme"] == "light"
    assert prov["theme"] == ".pythinker/config.local.toml"


def test_merge_list_concat():
    prov: dict = {}
    base = _type_based_merge(
        {}, {"hooks": [{"event": "Stop", "command": "a"}]}, prov, "~/.pythinker/config.toml"
    )
    base = _type_based_merge(
        base, {"hooks": [{"event": "Stop", "command": "b"}]}, prov, ".pythinker/config.toml"
    )
    assert len(base["hooks"]) == 2
    assert base["hooks"][0]["command"] == "a"
    assert base["hooks"][1]["command"] == "b"


def test_merge_list_concat_provenance():
    prov: dict = {}
    base = _type_based_merge({}, {"hooks": []}, prov, "~/.pythinker/config.toml")
    base = _type_based_merge(base, {"hooks": []}, prov, ".pythinker/config.toml")
    assert prov["hooks"] == "~/.pythinker/config.toml+.pythinker/config.toml"


def test_merge_list_base_case_provenance():
    prov: dict = {}
    _type_based_merge({}, {"hooks": []}, prov, "~/.pythinker/config.toml")
    assert prov["hooks"] == "~/.pythinker/config.toml"


def test_merge_list_dedup_extra_skill_dirs():
    prov: dict = {}
    base = _type_based_merge(
        {}, {"extra_skill_dirs": ["/a", "/b"]}, prov, "~/.pythinker/config.toml"
    )
    base = _type_based_merge(
        base, {"extra_skill_dirs": ["/b", "/c"]}, prov, ".pythinker/config.toml"
    )
    # /b appears in both — should appear only once (first occurrence kept)
    assert base["extra_skill_dirs"] == ["/a", "/b", "/c"]


def test_merge_dict_deep():
    prov: dict = {}
    base = _type_based_merge(
        {},
        {"tui": {"style": "pythinker", "smooth_streaming": True}},
        prov,
        "~/.pythinker/config.toml",
    )
    base = _type_based_merge(base, {"tui": {"style": "card"}}, prov, ".pythinker/config.toml")
    assert base["tui"]["style"] == "card"
    assert base["tui"]["smooth_streaming"] is True  # sibling preserved
    assert prov["tui"]["style"] == ".pythinker/config.toml"
    assert prov["tui"]["smooth_streaming"] == "~/.pythinker/config.toml"


def test_merge_key_only_in_overlay():
    prov: dict = {}
    result = _type_based_merge({}, {"theme": "dark"}, prov, "~/.pythinker/config.toml")
    assert result["theme"] == "dark"
    assert prov["theme"] == "~/.pythinker/config.toml"


def test_apply_env_vars_known_key(monkeypatch):
    monkeypatch.setenv("PYTHINKER_THEME", "light")
    merged: dict = {}
    prov: dict = {}
    _apply_env_vars(merged, prov)
    assert merged["theme"] == "light"
    assert prov["theme"] == "env PYTHINKER_THEME"


def test_apply_env_vars_unknown_key_ignored(monkeypatch):
    monkeypatch.setenv("PYTHINKER_XYZZY_UNKNOWN", "whatever")
    merged: dict = {}
    prov: dict = {}
    _apply_env_vars(merged, prov)
    assert "xyzzy_unknown" not in merged


def test_apply_env_vars_bool_coercion(monkeypatch):
    monkeypatch.setenv("PYTHINKER_DEFAULT_YOLO", "true")
    merged: dict = {}
    prov: dict = {}
    _apply_env_vars(merged, prov)
    # Stored as string; Pydantic coerces during model_validate
    assert merged["default_yolo"] == "true"
    assert prov["default_yolo"] == "env PYTHINKER_DEFAULT_YOLO"


def test_apply_env_vars_overrides_existing(monkeypatch):
    monkeypatch.setenv("PYTHINKER_THEME", "light")
    merged = {"theme": "dark"}
    prov = {"theme": "~/.pythinker/config.toml"}
    _apply_env_vars(merged, prov)
    assert merged["theme"] == "light"
    assert prov["theme"] == "env PYTHINKER_THEME"


# ---------------------------------------------------------------------------
# _load_scoped integration tests
# ---------------------------------------------------------------------------


def _write_toml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(data), encoding="utf-8")  # type: ignore[arg-type]


def test_load_scoped_user_only(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    _write_toml(tmp_path / "config.toml", {"theme": "light"})
    config = _load_scoped(project_root=None)
    assert config.theme == "light"
    assert config.source_scopes["user"] == (tmp_path / "config.toml").resolve()


def test_load_scoped_project_overrides_user(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    _write_toml(tmp_path / "config.toml", {"theme": "dark"})
    project_root = tmp_path / "myproject"
    _write_toml(project_root / ".pythinker" / "config.toml", {"theme": "light"})
    config = _load_scoped(project_root=project_root)
    assert config.theme == "light"


def test_load_scoped_local_overrides_project(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    _write_toml(tmp_path / "config.toml", {"theme": "dark"})
    project_root = tmp_path / "myproject"
    _write_toml(project_root / ".pythinker" / "config.toml", {"theme": "dark"})
    _write_toml(project_root / ".pythinker" / "config.local.toml", {"theme": "light"})
    config = _load_scoped(project_root=project_root)
    assert config.theme == "light"


def test_load_scoped_hooks_concatenate(tmp_path, monkeypatch):
    from pythinker_code.project_trust import set_project_trusted

    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    _write_toml(tmp_path / "config.toml", {"hooks": [{"event": "Stop", "command": "user-hook"}]})
    project_root = tmp_path / "myproject"
    _write_toml(
        project_root / ".pythinker" / "config.toml",
        {"hooks": [{"event": "Stop", "command": "project-hook"}]},
    )
    # Project hooks auto-execute, so they only merge once the project is
    # trusted (see test_project_trust.py for the untrusted paths).
    set_project_trusted(project_root, True)
    config = _load_scoped(project_root=project_root)
    commands = [h.command for h in config.hooks]
    assert "user-hook" in commands
    assert "project-hook" in commands


def test_load_scoped_scope_lock_violation(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    _write_toml(tmp_path / "config.toml", {})
    project_root = tmp_path / "myproject"
    _write_toml(
        project_root / ".pythinker" / "config.toml",
        {"providers": {"bad": {"type": "openai", "base_url": "x", "api_key": "sk-x"}}},
    )
    with pytest.raises(ConfigError, match="'providers'"):
        _load_scoped(project_root=project_root)


def test_load_scoped_validation_error_attributes_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    _write_toml(tmp_path / "config.toml", {})
    project_root = tmp_path / "myproject"
    _write_toml(
        project_root / ".pythinker" / "config.local.toml",
        {"theme": "neon"},  # invalid value
    )
    with pytest.raises(ConfigError, match="config.local.toml"):
        _load_scoped(project_root=project_root)


def test_load_scoped_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    monkeypatch.setenv("PYTHINKER_THEME", "light")
    _write_toml(tmp_path / "config.toml", {"theme": "dark"})
    project_root = tmp_path / "myproject"
    _write_toml(project_root / ".pythinker" / "config.toml", {"theme": "dark"})
    config = _load_scoped(project_root=project_root)
    assert config.theme == "light"  # env beats all file scopes


def test_load_scoped_source_scopes_populated(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    _write_toml(tmp_path / "config.toml", {})
    project_root = tmp_path / "myproject"
    _write_toml(project_root / ".pythinker" / "config.toml", {})
    config = _load_scoped(project_root=project_root)
    assert "user" in config.source_scopes
    assert "project" in config.source_scopes
    assert "local" not in config.source_scopes  # local file absent


def test_load_scoped_gitignores_local_config(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    _write_toml(tmp_path / "config.toml", {})
    project_root = tmp_path / "myproject"
    _write_toml(project_root / ".pythinker" / "config.local.toml", {})

    _load_scoped(project_root=project_root)

    gitignore = project_root / ".gitignore"
    assert gitignore.exists()
    assert ".pythinker/config.local.toml" in gitignore.read_text(encoding="utf-8")


def test_load_config_explicit_path_bypasses_scoping(tmp_path):
    """--config flag must bypass scope resolution entirely."""
    config_file = tmp_path / "explicit.toml"
    config_file.write_text('theme = "light"\n', encoding="utf-8")
    config = load_config(config_file)
    assert config.theme == "light"
    assert config.source_file == config_file.resolve()
    # source_scopes is empty because no scope pipeline was run
    assert config.source_scopes == {}


def test_load_config_no_args_uses_scope_resolution(tmp_path, monkeypatch):
    """load_config() with no args routes through scoped pipeline."""
    monkeypatch.setenv("PYTHINKER_SHARE_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # no .git in tmp_path → user-only scope
    (tmp_path / "config.toml").write_text('theme = "light"\n', encoding="utf-8")
    config = load_config()
    assert config.theme == "light"
    assert "user" in config.source_scopes


def test_goal_config_bounds():
    """goal.max_continuations is clamped to 1-10 by validation."""
    import pytest
    from pydantic import ValidationError

    from pythinker_code.config import GoalConfig

    assert GoalConfig().max_continuations == 3
    assert GoalConfig(max_continuations=1).max_continuations == 1
    assert GoalConfig(max_continuations=10).max_continuations == 10
    with pytest.raises(ValidationError):
        GoalConfig(max_continuations=0)
    with pytest.raises(ValidationError):
        GoalConfig(max_continuations=11)


def test_statusline_v2_segment_ids_and_defaults():
    from pythinker_code.config import STATUSLINE_SEGMENT_IDS, StatusLineConfig

    for seg in ("spinner", "speed", "effort", "cost", "diff", "elapsed", "limits", "clock"):
        assert seg in STATUSLINE_SEGMENT_IDS
    cfg = StatusLineConfig()
    assert cfg.segments == [
        "spinner",
        "model",
        "cost",
        "speed",
        "effort",
        "cwd",
        "git",
        "diff",
        "flags",
        "context",
        "elapsed",
        "clock",
    ]
    assert cfg.style == "fancy"
    assert cfg.bar_width == 10
    assert cfg.cost_budget is None


def test_statusline_v2_field_validation():
    import pytest
    from pydantic import ValidationError

    from pythinker_code.config import StatusLineConfig

    with pytest.raises(ValidationError):
        StatusLineConfig(bar_width=3)
    with pytest.raises(ValidationError):
        StatusLineConfig(bar_width=21)
    with pytest.raises(ValidationError):
        StatusLineConfig(cost_budget=-1.0)
    with pytest.raises(ValidationError):
        StatusLineConfig.model_validate({"style": "neon"})
    # command_timeout_ms is bounded: a runaway value must not let the external
    # status command hang for days before the kill fires.
    assert StatusLineConfig(command_timeout_ms=60_000).command_timeout_ms == 60_000
    with pytest.raises(ValidationError):
        StatusLineConfig(command_timeout_ms=60_001)
    with pytest.raises(ValidationError):
        StatusLineConfig(command_timeout_ms=0)


def test_apply_env_vars_auto_update(monkeypatch):
    monkeypatch.setenv("PYTHINKER_AUTO_UPDATE", "false")
    merged: dict = {}
    prov: dict = {}
    _apply_env_vars(merged, prov)
    assert merged["auto_update"] == "false"
    assert prov["auto_update"] == "env PYTHINKER_AUTO_UPDATE"


def test_auto_update_defaults_true():
    from pythinker_code.config import Config

    assert Config().auto_update is True


def test_auto_update_round_trips_false():
    from pythinker_code.config import Config

    cfg = Config.model_validate({"auto_update": False})
    assert cfg.auto_update is False
