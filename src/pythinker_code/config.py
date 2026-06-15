from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from types import UnionType
from typing import Any, Literal, Self, Union, cast, get_args, get_origin

import tomlkit
from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    SecretStr,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)
from pythinker_core.chat_provider import ThinkingEffort
from tomlkit.exceptions import TOMLKitError

from pythinker_code.exception import ConfigError
from pythinker_code.feedback_repo import (
    DEFAULT_FEEDBACK_GITHUB_REPO,
    normalize_feedback_github_repo,
)
from pythinker_code.hooks.config import HookDef
from pythinker_code.llm import ModelCapability, ProviderType
from pythinker_code.share import get_share_dir
from pythinker_code.utils.logging import logger


def find_project_root(cwd: Path) -> Path | None:
    """Walk up from cwd to find the nearest directory containing .git/.

    Returns None when no .git marker is found before reaching the filesystem
    root, so callers can skip project/local scopes without a fallback.
    """
    current = cwd.resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


# ---------------------------------------------------------------------------
# Scope system constants
# ---------------------------------------------------------------------------

SCOPE_LOCKED_PATHS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("providers",),  # contains api_key per provider — must stay in user scope
        ("services",),  # contains api_key fields — must stay in user scope
        ("feedback", "api_key"),  # only the key, not the whole feedback section
        # Auto-executed when the shell starts — a repo-controlled project config
        # must never be able to choose the binary that runs (`command`), nor to
        # trigger or extend its execution (`enabled`/`segments` flip the command
        # segment on; `command_timeout_ms` governs how long it may run).
        ("tui", "statusline", "command"),
        ("tui", "statusline", "enabled"),
        ("tui", "statusline", "segments"),
        ("tui", "statusline", "command_timeout_ms"),
    }
)

DEDUP_LIST_FIELDS: frozenset[str] = frozenset({"allowed_domains", "extra_skill_dirs"})

ENV_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "PYTHINKER_DEFAULT_MODEL": ("default_model",),
    "PYTHINKER_DEFAULT_THINKING": ("default_thinking",),
    "PYTHINKER_DEFAULT_THINKING_EFFORT": ("default_thinking_effort",),
    "PYTHINKER_AGENT_EXECUTION_PROFILE": ("agent_execution_profile",),
    "PYTHINKER_DEFAULT_YOLO": ("default_yolo",),
    "PYTHINKER_ASK_USER_QUESTION_POLICY": ("ask_user_question_policy",),
    "PYTHINKER_AUTO_DELIBERATE_DESTRUCTIVE_ACTIONS": ("auto_deliberate_destructive_actions",),
    "PYTHINKER_SKIP_AUTO_PROMPT_INJECTION": ("skip_auto_prompt_injection",),
    "PYTHINKER_DEFAULT_PLAN_MODE": ("default_plan_mode",),
    "PYTHINKER_DEFAULT_EDITOR": ("default_editor",),
    "PYTHINKER_THEME": ("theme",),
    "PYTHINKER_SHOW_THINKING_STREAM": ("show_thinking_stream",),
    "PYTHINKER_PREVENT_IDLE_SLEEP": ("prevent_idle_sleep",),
    "PYTHINKER_AUTO_UPDATE": ("auto_update",),
    "PYTHINKER_TELEMETRY": ("telemetry",),
    "PYTHINKER_SESSION_RETENTION_DAYS": ("session_retention_days",),
    "PYTHINKER_MERGE_ALL_AVAILABLE_SKILLS": ("merge_all_available_skills",),
    "PYTHINKER_STATUSLINE": ("tui", "statusline", "enabled"),
}


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------


def unknown_config_key_paths(
    model_cls: type[BaseModel], data: dict[str, Any], _prefix: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    """Dotted paths in *data* that no model field will consume.

    Pydantic's extra='ignore' makes a typo'd key silently vanish and change
    behavior with no signal. Conservative by design: recursion only follows
    shapes it can prove (nested models, dict-of-model maps, lists of models),
    so an unmodellable value is never reported — a false positive here would
    teach users to ignore the diagnostics.
    """
    if model_cls.model_config.get("extra") == "allow":
        return []
    known: dict[str, Any] = {}
    for name, field in model_cls.model_fields.items():
        known[name] = field
        if field.alias:
            known[field.alias] = field
        if isinstance(field.validation_alias, str):
            known[field.validation_alias] = field
        elif isinstance(field.validation_alias, AliasChoices):
            for choice in field.validation_alias.choices:
                if isinstance(choice, str):
                    known[choice] = field

    unknown: list[tuple[str, ...]] = []
    for key, value in data.items():
        path = (*_prefix, key)
        field = known.get(key)
        if field is None:
            unknown.append(path)
            continue
        unknown.extend(_nested_unknown_paths(field.annotation, value, path))
    return unknown


def _nested_unknown_paths(
    annotation: Any, value: Any, path: tuple[str, ...]
) -> list[tuple[str, ...]]:
    annotation = _sole_non_none_type(annotation)
    origin = get_origin(annotation)
    if origin is dict:
        args = get_args(annotation)
        if len(args) == 2 and isinstance(value, dict):
            item_type = _sole_non_none_type(args[1])
            if isinstance(item_type, type) and issubclass(item_type, BaseModel):
                nested: list[tuple[str, ...]] = []
                for map_key, item in cast(dict[str, Any], value).items():
                    if isinstance(item, dict):
                        nested.extend(
                            unknown_config_key_paths(
                                item_type, cast(dict[str, Any], item), (*path, str(map_key))
                            )
                        )
                return nested
        return []
    if origin in (list, tuple, set):
        args = get_args(annotation)
        if args and isinstance(value, list):
            item_type = _sole_non_none_type(args[0])
            if isinstance(item_type, type) and issubclass(item_type, BaseModel):
                nested = []
                for item in cast(list[Any], value):
                    if isinstance(item, dict):
                        # Item paths omit the index: the field name plus the
                        # offending key is what a user greps their TOML for.
                        nested.extend(
                            unknown_config_key_paths(item_type, cast(dict[str, Any], item), path)
                        )
                return nested
        return []
    if (
        isinstance(annotation, type)
        and issubclass(annotation, BaseModel)
        and isinstance(value, dict)
    ):
        return unknown_config_key_paths(annotation, cast(dict[str, Any], value), path)
    return []


def _sole_non_none_type(annotation: Any) -> Any:
    """Unwrap ``X | None`` to ``X``; ambiguous unions return unchanged."""
    if get_origin(annotation) in (Union, UnionType):
        non_none = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _report_unknown_keys_single_source(data: Any, source: str) -> None:
    """Unknown-key diagnostics for single-source loads (--config/--config-file).

    The scoped pipeline reports through its merge provenance; explicit loads
    bypass the merge, so build a one-source provenance here. Non-dict payloads
    are left for Config.model_validate to reject with its own error.
    """
    if not isinstance(data, dict):
        return
    plain = {str(key): value for key, value in cast(dict[Any, Any], data).items()}
    provenance: dict[str, Any] = {}
    merged = _type_based_merge({}, plain, provenance, source)
    _report_unknown_config_keys(merged, provenance)


def _report_unknown_config_keys(merged: dict[str, Any], provenance: dict[str, Any]) -> None:
    """Warn (or raise under PYTHINKER_STRICT_CONFIG) for unconsumed keys."""
    unknown_paths = unknown_config_key_paths(Config, merged)
    if not unknown_paths:
        return
    findings = [
        f"{'.'.join(path)} (from {_lookup_provenance(provenance, path)})"
        for path in sorted(unknown_paths)
    ]
    if os.environ.get("PYTHINKER_STRICT_CONFIG"):
        raise ConfigError("Unknown configuration keys:\n  " + "\n  ".join(findings))
    for finding in findings:
        logger.warning("Unknown config key ignored: {finding}", finding=finding)


def _set_nested(d: dict[str, Any], path: tuple[str, ...], value: object) -> None:
    """Walk *path* into *d*, creating intermediate dicts, then set the leaf."""
    node = d
    for part in path[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[path[-1]] = value


def _lookup_provenance(prov: dict[str, Any] | str, loc: tuple[str | int, ...]) -> str:
    """Recursively follow *loc* through the provenance map.

    Integer elements (Pydantic list indices) are skipped — we map them back
    to the parent collection's scope string so error messages stay useful.
    Returns "unknown scope" when the path cannot be fully resolved.
    """
    if not loc or isinstance(prov, str):
        return prov if isinstance(prov, str) else "unknown scope"
    head, *tail = loc
    if isinstance(head, int):
        return prov if isinstance(prov, str) else _lookup_provenance(prov, tuple(tail))
    if head in prov:
        return _lookup_provenance(prov[head], tuple(tail))
    return "unknown scope"


def _check_scope_locks(scope_dict: dict[str, Any], scope_name: str) -> None:
    """Raise ConfigError if *scope_dict* contains any scope-locked field paths.

    Checks every path in SCOPE_LOCKED_PATHS by walking the raw dict before
    Pydantic validation, so secrets are blocked before they can be merged.
    """
    for path in SCOPE_LOCKED_PATHS:
        node: Any = scope_dict
        for part in path:
            if not isinstance(node, dict) or part not in node:
                break
            node = cast(Any, node[part])
        else:
            field_path = ".".join(path)
            # Derive a short scope label for the error message
            if "local" in scope_name:
                scope_label = "local scope"
            elif "project" in scope_name or scope_name.startswith(".pythinker"):
                scope_label = "project scope"
            else:
                scope_label = scope_name
            # No SCOPE_LOCKED_PATHS entry has an ENV_FIELD_MAP override, so the
            # user config file is the only valid destination to point at.
            raise ConfigError(
                f"'{field_path}' cannot be set in {scope_name} ({scope_label}).\n"
                f"  Move it to ~/.pythinker/config.toml."
            )


def _type_based_merge(
    base: dict[str, Any],
    overlay: dict[str, Any],
    provenance: dict[str, Any],
    scope: str,
) -> dict[str, Any]:
    """Merge *overlay* into *base* using type-based rules, tracking provenance.

    Rules:
    - Scalar (str/bool/int/float/None): overlay wins, provenance records scope.
    - List: base + overlay concatenated; DEDUP_LIST_FIELDS deduplicated
      (order-preserving, first occurrence wins).
    - Dict: recurse so nested keys can be independently overridden.

    Mutates *base* and *provenance* in place; also returns *base* for chaining.
    """
    for key, value in overlay.items():
        if isinstance(value, dict):
            # Normalize any scalar/list at this key to an empty dict before recursing;
            # recursing into a non-dict crashes with TypeError, and leaving provenance[key]
            # as a string would crash a subsequent dict-merge on the same key.
            if key in base and not isinstance(base[key], dict):
                base[key] = {}
                provenance[key] = {}
            # For dicts, always recurse to track individual nested keys
            if key not in base:
                base[key] = {}
            if key not in provenance or not isinstance(provenance[key], dict):
                provenance[key] = {}
            _type_based_merge(
                cast(dict[str, Any], base[key]),
                cast(dict[str, Any], value),
                cast(dict[str, Any], provenance[key]),
                scope,
            )
        elif key not in base:
            base[key] = value
            provenance[key] = scope
        elif isinstance(value, list) and isinstance(base[key], list):
            combined = base[key] + value
            if key in DEDUP_LIST_FIELDS:
                combined = list(dict.fromkeys(combined))
            base[key] = combined
            existing = provenance.get(key)
            provenance[key] = f"{existing}+{scope}" if existing else scope
        else:
            base[key] = value
            provenance[key] = scope
    return base


def _apply_env_vars(merged: dict[str, Any], provenance: dict[str, Any]) -> None:
    """Overlay PYTHINKER_* env vars onto *merged*, updating *provenance*.

    Values are stored as raw strings; Pydantic coerces them during
    model_validate(). Only keys in ENV_FIELD_MAP are recognised; all others
    are silently ignored.
    """
    for env_key, path in ENV_FIELD_MAP.items():
        value = os.environ.get(env_key)
        if value is not None:
            _set_nested(merged, path, value)
            _set_nested(provenance, path, f"env {env_key}")


def _load_scoped(project_root: Path | None, *, persist: bool = True) -> Config:
    """Run the five-step scoped config resolution pipeline.

    Steps: Ingest → Guard → Merge → Env → Validate.
    Returns a fully-validated Config with source_scopes populated.

    When ``persist`` is False the pipeline is read-only: it merges the same
    user/project/local scopes but performs no disk writes (no share-dir/lock
    creation, no migration, no default seeding, no auto-gitignore) and skips the
    project-trust read (which itself creates the trust-lock file). Trust only
    gates project *hooks*, which no read-only consumer renders or executes, so
    skipping it leaves the merged config faithful for those callers.
    """
    from pythinker_code.utils.gitignore import ensure_gitignored

    # ── INGEST ────────────────────────────────────────────────────────────
    # When read-only, do not let get_config_file() create the share dir.
    default_user_file = get_config_file(create=persist).expanduser().resolve(strict=False)
    # Trigger JSON→TOML migration if needed (existing logic)
    if persist and not default_user_file.exists():
        migration_error = _migrate_json_config_to_toml()
        if migration_error is not None:
            raise ConfigError(
                f"Legacy config file has incompatible settings; please fix or "
                f"rename/delete {migration_error.config_file} to continue. "
                f"Errors: {migration_error.errors}"
            ) from None

    def _read_toml(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return dict(tomlkit.loads(path.read_text(encoding="utf-8")))
        except TOMLKitError as exc:
            raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

    user_file = default_user_file
    user_dict = _read_toml(user_file)

    # If the user config file still doesn't exist after migration (e.g. corrupt JSON
    # was backed up but no TOML was written), seed it with defaults so subsequent
    # runs have a concrete starting point — matching the legacy single-file behaviour.
    if persist and not user_file.exists():
        default_cfg = get_default_config()
        logger.debug("No config file found, creating default config: {config}", config=default_cfg)
        save_config(default_cfg, user_file)

    project_file: Path | None = None
    local_file: Path | None = None
    project_dict: dict[str, Any] = {}
    local_dict: dict[str, Any] = {}
    project_trusted = True
    stripped_hook_files: list[str] = []

    if project_root is not None:
        project_file = project_root / ".pythinker" / "config.toml"
        local_file = project_root / ".pythinker" / "config.local.toml"
        if persist:
            from pythinker_code.project_trust import is_project_trusted

            project_trusted = is_project_trusted(project_root)
        else:
            # Read-only callers skip the trust read — it creates the trust-lock
            # file under the share dir. Trust only gates project hooks, which a
            # read-only consumer never executes, so read every scope and leave any
            # hooks merged-but-inert rather than touching disk.
            project_trusted = True
        if project_trusted:
            project_dict = _read_toml(project_file)
            local_dict = _read_toml(local_file)
        else:
            # An untrusted (e.g. freshly cloned) project must not brick startup
            # with broken TOML, and must not auto-execute anything: hooks are
            # shell commands run on lifecycle events, so they load only after
            # the user records trust (/trust).
            try:
                project_dict = _read_toml(project_file)
            except ConfigError as exc:
                logger.warning(
                    "Ignoring unreadable project config in untrusted project: {error}",
                    error=exc,
                )
                project_dict = {}
            try:
                local_dict = _read_toml(local_file)
            except ConfigError as exc:
                logger.warning(
                    "Ignoring unreadable project config in untrusted project: {error}",
                    error=exc,
                )
                local_dict = {}
            for scope_dict, scope_file in ((project_dict, project_file), (local_dict, local_file)):
                if scope_dict.pop("hooks", None) is not None:
                    stripped_hook_files.append(str(scope_file))
                    logger.warning(
                        "Project hooks in {file} are disabled until the project is "
                        "trusted; run /trust to enable them",
                        file=scope_file,
                    )

    # ── GUARD ─────────────────────────────────────────────────────────────
    if project_file is not None:
        _check_scope_locks(project_dict, str(project_file))
    if local_file is not None:
        _check_scope_locks(local_dict, str(local_file))

    # ── MERGE ─────────────────────────────────────────────────────────────
    provenance: dict[str, Any] = {}
    merged = _type_based_merge({}, user_dict, provenance, str(user_file))
    if project_dict:
        merged = _type_based_merge(merged, project_dict, provenance, str(project_file))
    if local_dict:
        merged = _type_based_merge(merged, local_dict, provenance, str(local_file))

    # ── ENV OVERLAY ───────────────────────────────────────────────────────
    _apply_env_vars(merged, provenance)

    # ── DIAGNOSE ──────────────────────────────────────────────────────────
    _report_unknown_config_keys(merged, provenance)

    # ── VALIDATE ──────────────────────────────────────────────────────────
    try:
        config = Config.model_validate(merged)
    except ValidationError as exc:
        enriched: list[str] = []
        for err in exc.errors():
            scope = _lookup_provenance(provenance, tuple(err["loc"]))
            field = ".".join(str(p) for p in err["loc"])
            enriched.append(f"  {field}: {err['msg']}  [from {scope}]")
        raise ConfigError("Invalid configuration:\n" + "\n".join(enriched)) from exc

    # ── METADATA ──────────────────────────────────────────────────────────
    if project_root is not None and not project_trusted:
        config.disabled_project_hooks = stripped_hook_files
    config.is_from_default_location = True
    config.source_file = user_file
    if user_file.exists():
        config.source_scopes["user"] = user_file
    if project_file is not None and project_file.exists():
        config.source_scopes["project"] = project_file.resolve(strict=False)
    if local_file is not None and local_file.exists():
        config.source_scopes["local"] = local_file.resolve(strict=False)
        # Auto-gitignore local config so it is never accidentally committed.
        # Skip this write for read-only callers.
        if persist:
            ensure_gitignored(
                project_root,  # type: ignore[arg-type]
                ".pythinker/config.local.toml",
                comment="Added by pythinker",
            )

    return config


AgentExecutionProfile = Literal[
    "default",
    "review_safe",
    "autonomous_coding",
    "plan_only",
    "ci_fixer",
]


class OAuthRef(BaseModel):
    """Reference to OAuth credentials stored outside the config file."""

    storage: Literal["keyring", "file"] = "file"
    """Credential storage backend."""
    key: str
    """Storage key to locate OAuth credentials."""


class LLMProvider(BaseModel):
    """LLM provider configuration."""

    type: ProviderType
    """Provider type"""
    base_url: str
    """API base URL"""
    api_key: SecretStr
    """API key"""
    env: dict[str, str] | None = None
    """Environment variables to set before creating the provider instance"""
    custom_headers: dict[str, str] | None = None
    """Custom headers to include in API requests"""
    reasoning_key: str | None = None
    """Message field name carrying reasoning content for OpenAI-compatible APIs.
    Applies to provider type ``openai_legacy``. Defaults to ``reasoning_content``
    when unset. Use an empty string to disable reasoning round-tripping."""
    oauth: OAuthRef | None = None
    """OAuth credential reference (do not store tokens here)."""

    @field_serializer("api_key", when_used="json")
    def dump_secret(self, v: SecretStr):
        return v.get_secret_value()


class LLMModel(BaseModel):
    """LLM model configuration."""

    provider: str
    """Provider name"""
    model: str
    """Model name"""
    max_context_size: int
    """Maximum context size (unit: tokens)"""
    capabilities: set[ModelCapability] | None = None
    """Model capabilities"""
    display_name: str | None = None
    """Human-readable model name (sourced from the provider's models API when available)"""


class GoalConfig(BaseModel):
    """Thread-goal (/goal) behavior."""

    auto_continue: bool = Field(
        default=False,
        description=(
            "Automatically continue turns toward the active /goal after the primary "
            "turn ends, until the goal is marked complete/blocked, a continuation is "
            "rejected, or max_continuations is reached."
        ),
    )
    max_continuations: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum automatic goal continuations per user submission.",
    )


class LoopControl(BaseModel):
    """Agent loop control configuration."""

    max_steps_per_turn: int = Field(
        default=1000,
        ge=1,
        validation_alias=AliasChoices("max_steps_per_turn", "max_steps_per_run"),
    )
    """Maximum number of steps in one turn"""
    max_consecutive_failures: int = Field(default=8, ge=0)
    """Yield to the user after this many consecutive steps in which *every* tool
    call failed (a degenerate stuck loop), instead of continuing to
    ``max_steps_per_turn``. The turn ends with a ``stuck`` outcome and a handoff
    summary of what was tried. ``0`` disables the backstop. Default: 8."""
    max_truncation_recoveries: int = Field(default=3, ge=0)
    """When a model response is cut off by the output-token limit and makes no tool call,
    nudge the model to continue at most this many times per turn before surfacing the
    truncated answer. ``0`` disables truncation recovery. Default: 3."""
    max_session_cost_usd: float | None = Field(default=None, gt=0)
    """Optional per-session spend ceiling in USD. When set, the turn stops with a
    ``budget_exhausted`` outcome once the session's accumulated estimated cost reaches
    this value, instead of continuing to ``max_steps_per_turn``. Off by default
    (``None``). Best-effort: cost is estimated from token usage and is ``0`` for models
    with unknown pricing, so the ceiling never blocks when spend cannot be estimated."""
    budget_nudge_ratio: float = Field(default=0.75, ge=0.0, le=0.99)
    """Append a one-shot per-turn reminder after a turn crosses this fraction of
    ``max_session_cost_usd``. The nudge is advisory and never auto-continues."""
    max_retries_per_step: int = Field(default=3, ge=1)
    """Maximum number of retries in one step"""
    max_ralph_iterations: int = Field(default=0, ge=-1)
    """Extra iterations after the first turn in Ralph mode. Use -1 for unlimited."""
    reserved_context_size: int = Field(default=50_000, ge=1000)
    """Reserved token count for LLM response generation. Auto-compaction triggers when
    either context_tokens + reserved_context_size >= max_context_size or
    context_tokens >= max_context_size * compaction_trigger_ratio. Default is 50000."""
    compaction_trigger_ratio: float = Field(default=0.85, ge=0.5, le=0.99)
    """Context usage ratio threshold for auto-compaction. Default is 0.85 (85%).
    Auto-compaction triggers when context_tokens >= max_context_size * compaction_trigger_ratio
    or when context_tokens + reserved_context_size >= max_context_size."""
    prune_trigger_ratio: float = Field(default=0.7, ge=0.0, le=0.99)
    """Context usage ratio at which the cheap stale-tool-output prune tier runs,
    *before* full LLM summarization. Large completed tool-result bodies in deep
    history are replaced with a short placeholder, deferring or avoiding the
    lossy summary. Set at or above ``compaction_trigger_ratio`` to disable the
    tier. Default is 0.7 (70%)."""
    prune_protect_last: int = Field(default=20, ge=0)
    """Number of most-recent messages the prune tier never touches (recent tool
    output stays at full fidelity). Default: 20."""
    prune_min_chars: int = Field(default=2000, ge=0)
    """Only tool outputs whose text exceeds this many characters are pruned, so
    small results are left intact. Default: 2000."""
    prune_tool_result_max_chars: int = Field(default=0, ge=0)
    """Optional per-tool-result character budget for old tool messages before
    full pruning. ``0`` keeps current behavior. Default: 0."""


class BackgroundConfig(BaseModel):
    """Background task runtime configuration."""

    max_running_tasks: int = Field(default=4, ge=1)
    task_retention_days: int = Field(default=7, ge=0)
    """Terminal background tasks older than this many days are pruned on
    reconcile. ``0`` disables cleanup."""
    read_max_bytes: int = Field(default=30_000, ge=1024)
    notification_tail_lines: int = Field(default=20, ge=1)
    notification_tail_chars: int = Field(default=3_000, ge=256)
    wait_poll_interval_ms: int = Field(default=500, ge=50)
    worker_heartbeat_interval_ms: int = Field(default=5_000, ge=100)
    worker_stale_after_ms: int = Field(default=15_000, ge=1000)
    kill_grace_period_ms: int = Field(default=2_000, ge=100)
    max_output_bytes: int = Field(default=50 * 1024 * 1024, ge=0)
    """Maximum size of a bash task's output.log in bytes. When a task's output
    grows past this, the worker terminates it and marks it failed, preventing a
    chatty task from exhausting disk. ``0`` means unlimited. Default: 50 MiB."""
    keep_alive_on_exit: bool = Field(
        default=False,
        description="Keep background tasks alive when CLI exits. Default: kill on exit.",
    )
    agent_task_timeout_s: int = Field(default=3600, ge=60)
    """Maximum runtime in seconds for a background agent task. Default: 3600 (1 hour)."""
    print_wait_ceiling_s: int = Field(default=3600, ge=1)
    """Hard ceiling for how long ``--print`` mode waits for background tasks before
    killing them and exiting. The effective wait is
    ``min(max(active_task.timeout_s or agent_task_timeout_s), print_wait_ceiling_s)``.
    Default: 3600 (1 hour)."""


class NotificationConfig(BaseModel):
    """Notification runtime configuration."""

    claim_stale_after_ms: int = Field(default=15_000, ge=1000)


class MemoryConfig(BaseModel):
    """Context- and memory-aware agent configuration."""

    lexical_recall: bool = Field(
        default=True,
        description="Use relevance-ranked lexical recall instead of verbatim project memory.",
    )
    injection_bus: bool = Field(
        default=True,
        description="Budget dynamic prompt injections through the injection bus.",
    )
    injection_ceiling_tokens: int = Field(
        default=2048,
        ge=256,
        description="Maximum total tokens reserved for dynamic prompt injections.",
    )
    harvest_on_compaction: bool = Field(
        default=False,
        description=(
            "Persist safe decisions/blockers/next steps before compaction discards history."
        ),
    )
    harvest_on_stop: bool = Field(
        default=False,
        description="Stage safe decisions/blockers/next steps after a turn reaches Stop.",
    )
    journal_recaps: bool = Field(
        default=False,
        description="Write stable-schema JOURNAL.md session recap blocks.",
    )
    consolidation: bool = Field(
        default=False,
        description="Enable approval-gated memory inbox consolidation helpers.",
    )
    durable_memory: bool = Field(
        default=False,
        description=(
            "Opt-in 'durable memory' profile: enable cross-session persistence by turning on "
            "harvest_on_compaction + journal_recaps together, without changing their privacy-"
            "preserving defaults. Consolidation (which writes durable MEMORY.md and is approval-"
            "gated) stays separately opt-in. Set the individual flags directly for finer control."
        ),
    )

    @property
    def harvest_enabled(self) -> bool:
        """Whether compaction should harvest, honoring the durable-memory profile."""
        return self.harvest_on_compaction or self.durable_memory

    @property
    def journal_enabled(self) -> bool:
        """Whether session recaps should be journaled, honoring the durable-memory profile."""
        return self.journal_recaps or self.durable_memory


class PythinkerAISearchConfig(BaseModel):
    """Pythinker AI Search configuration."""

    base_url: str
    """Base URL for Pythinker AI Search service."""
    api_key: SecretStr
    """API key for Pythinker AI Search service."""
    custom_headers: dict[str, str] | None = None
    """Custom headers to include in API requests."""
    oauth: OAuthRef | None = None
    """OAuth credential reference (do not store tokens here)."""

    @field_serializer("api_key", when_used="json")
    def dump_secret(self, v: SecretStr):
        return v.get_secret_value()


class PythinkerAIFetchConfig(BaseModel):
    """Pythinker AI Fetch configuration."""

    base_url: str
    """Base URL for Pythinker AI Fetch service."""
    api_key: SecretStr
    """API key for Pythinker AI Fetch service."""
    custom_headers: dict[str, str] | None = None
    """Custom headers to include in API requests."""
    oauth: OAuthRef | None = None
    """OAuth credential reference (do not store tokens here)."""

    @field_serializer("api_key", when_used="json")
    def dump_secret(self, v: SecretStr):
        return v.get_secret_value()


class Services(BaseModel):
    """Services configuration."""

    pythinker_ai_search: PythinkerAISearchConfig | None = None
    """Pythinker AI Search configuration."""
    pythinker_ai_fetch: PythinkerAIFetchConfig | None = None
    """Pythinker AI Fetch configuration."""


class WebConfig(BaseModel):
    """Web fetch/search policy."""

    allowed_domains: list[str] | None = Field(
        default=None,
        description=(
            "If set, web fetch and search may only touch these domains and their "
            "subdomains. None or empty means unrestricted (default)."
        ),
    )

    @field_validator("allowed_domains")
    @classmethod
    def _validate_allowed_domains(cls, value: list[str] | None) -> list[str] | None:
        for entry in value or []:
            cleaned = entry.strip()
            if not cleaned:
                raise ValueError(
                    "Invalid allowed_domains entry: empty or whitespace-only hostname. "
                    "Remove it, or omit allowed_domains entirely to leave web access "
                    "unrestricted."
                )
            if cleaned.strip(".") == "":
                raise ValueError(
                    f"Invalid allowed_domains entry {entry!r}: hostname must contain "
                    "domain labels, not only dots."
                )
            if any(char.isspace() for char in cleaned) or any(char in cleaned for char in "/:"):
                raise ValueError(
                    f"Invalid allowed_domains entry {entry!r}: use a bare hostname "
                    "like 'example.com', not a URL, path, or host:port."
                )
        return value


class FeedbackConfig(BaseModel):
    """User-submitted feedback endpoint configuration."""

    endpoint_url: str = Field(
        default="",
        description=(
            "Full URL for the /feedback slash command. Overrides the built-in "
            "Pythinker platform endpoint when set."
        ),
    )
    api_key: SecretStr | None = Field(
        default=None,
        description="Optional bearer token for the feedback endpoint.",
    )
    custom_headers: dict[str, str] | None = Field(
        default=None,
        description="Optional extra headers for feedback endpoint requests.",
    )
    github_client_id: str = Field(
        default="",
        description=(
            "Optional GitHub OAuth App client ID. When set, /feedback uses GitHub "
            "device login and creates issues as the signed-in GitHub user."
        ),
    )
    github_repo: str = Field(
        default=DEFAULT_FEEDBACK_GITHUB_REPO,
        description="GitHub owner/repo used by /feedback GitHub OAuth submissions.",
    )

    @field_validator("github_repo")
    @classmethod
    def migrate_legacy_github_repo(cls, value: str) -> str:
        return normalize_feedback_github_repo(value)

    @field_serializer("api_key", when_used="json")
    def dump_secret(self, v: SecretStr | None):
        return v.get_secret_value() if v is not None else None


class MCPClientConfig(BaseModel):
    """MCP client configuration."""

    tool_call_timeout_ms: int = 60000
    """Timeout for tool calls in milliseconds."""

    startup_timeout_ms: int = 30000
    """Per-server connect/inventory timeout. A hung connect would otherwise
    block every agent turn, since the loop awaits MCP loading."""


STATUSLINE_SEGMENT_IDS: tuple[str, ...] = (
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
    "tokens",
    "elapsed",
    "limits",
    "clock",
    "command",
)

_DEFAULT_STATUSLINE_SEGMENTS: tuple[str, ...] = (
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
)


class StatusLineConfig(BaseModel):
    """Customizable shell status line (footer) configuration."""

    enabled: bool = Field(
        default=True,
        description=(
            "Master switch for status line customization. When false the shell "
            "renders the stock footer regardless of the other fields."
        ),
    )
    segments: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_STATUSLINE_SEGMENTS),
        description=(
            "Footer segments to display, in order. Known ids: cwd, git, flags, "
            "context, tokens, model, spinner, cost, speed, effort, diff, elapsed, "
            "limits, clock, command. Unknown ids are ignored so configs "
            "stay forward-compatible."
        ),
    )
    command: str | None = Field(
        default=None,
        description=(
            "Optional external command whose first stdout line is shown in the "
            "footer (requires the 'command' segment). Run without a shell; "
            "killed after command_timeout_ms."
        ),
    )
    command_timeout_ms: int = Field(
        default=1000,
        gt=0,
        le=60_000,
        description="Timeout in milliseconds for the external status command (max 60s).",
    )
    style: Literal["fancy", "plain"] = Field(
        default="fancy",
        description=(
            "Footer visual style. 'fancy' renders colors, separators, and the "
            "context bar; 'plain' keeps the monochrome text-only footer."
        ),
    )
    bar_width: int = Field(
        default=10,
        ge=4,
        le=20,
        description="Width in cells of the context progress bar.",
    )
    cost_budget: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional session budget in USD; when set the cost segment renders '$spent/$budget'."
        ),
    )

    @field_validator("segments")
    @classmethod
    def _drop_unknown_and_duplicate_segments(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for segment in value:
            if segment in STATUSLINE_SEGMENT_IDS and segment not in seen:
                seen.add(segment)
                cleaned.append(segment)
        return cleaned


class TUIConfig(BaseModel):
    """TUI rendering style configuration."""

    style: Literal["pythinker", "card"] = Field(
        default="card",
        description=(
            "TUI rendering style. 'card' (default) uses highlighted user "
            "messages and bordered tool cards. 'pythinker' keeps the legacy "
            "worklog-based rendering. Override at runtime with the env var "
            "PYTHINKER_TUI_STYLE=pythinker."
        ),
    )
    prompt_history_enabled: bool = Field(
        default=True,
        description=(
            "Persist shell prompt history under the Pythinker share directory. "
            "Set false or export PYTHINKER_DISABLE_PROMPT_HISTORY=1 for sensitive sessions."
        ),
    )
    turn_recaps: bool = Field(
        default=False,
        description=(
            "Show a compact recap line after completed interactive shell turns. "
            "Off by default; enable with `/config recaps on`."
        ),
    )
    code_theme: str = Field(
        default="catppuccin-adaptive",
        description=(
            "Syntax-highlighting theme for assistant code blocks. Default "
            "'catppuccin-adaptive' uses Catppuccin Mocha (dark UI) / Latte "
            "(light UI) on the calm transparent block. Use "
            "'catppuccin-mocha'/'catppuccin-latte' to pin one, 'pythinker-ansi' "
            "for the terminal-native ANSI look, or any Pygments style name (e.g. "
            "'monokai', 'dracula') to render on that style's solid background."
        ),
    )
    statusline: StatusLineConfig = Field(
        default_factory=StatusLineConfig,
        description="Customizable status line (footer) settings; see /statusline.",
    )
    smooth_streaming: bool = Field(
        default=True,
        description=(
            "Pace streamed assistant text so it reveals smoothly instead of "
            "landing in bursty delta-sized clumps. Keeps up with the model "
            "(bounded catch-up). Set false to reveal each delta immediately."
        ),
    )

    @field_validator("code_theme")
    @classmethod
    def _validate_code_theme(cls, value: str) -> str:
        from pythinker_code.utils.rich.syntax import available_code_themes

        allowed = available_code_themes()
        if value in allowed:
            return value
        if value.lower() in allowed:
            return value.lower()
        raise ValueError(
            f"Unknown code_theme {value!r}. Choose 'pythinker-ansi' or a Pygments "
            f"style name. Available: {', '.join(allowed)}"
        )


class MCPConfig(BaseModel):
    """MCP configuration."""

    client: MCPClientConfig = Field(
        default_factory=MCPClientConfig, description="MCP client configuration"
    )


class Config(BaseModel):
    """Main configuration structure."""

    is_from_default_location: bool = Field(
        default=False,
        description="Whether the config was loaded from the default location",
        exclude=True,
    )
    source_file: Path | None = Field(
        default=None,
        description="Path to the loaded config file. None when loaded from --config text.",
        exclude=True,
    )
    source_scopes: dict[str, Path] = Field(
        default_factory=dict,
        description=(
            "Paths of config files that contributed to this resolved config, keyed by scope name. "
            "e.g. {'user': Path('~/.pythinker/config.toml'), "
            "'project': Path('.pythinker/config.toml')}."
        ),
        exclude=True,
    )
    default_model: str = Field(default="", description="Default model to use")
    default_thinking: bool = Field(default=False, description="Default thinking mode")
    default_thinking_effort: ThinkingEffort | None = Field(
        default=None,
        description=(
            "Default thinking effort level. When unset, default_thinking=true maps to high "
            "and default_thinking=false maps to off for backward compatibility."
        ),
    )
    agent_execution_profile: AgentExecutionProfile = Field(
        default="default",
        description=(
            "Preset defaults for agent autonomy. Profiles only fill unset related options: "
            "review_safe, autonomous_coding, plan_only, or ci_fixer."
        ),
    )
    default_yolo: bool = Field(default=False, description="Default yolo (auto-approve) mode")
    best_practices_always: bool = Field(
        default=False,
        description=(
            "When true, fold the full /best-practices engineering guidance into the root "
            "session's system prompt at startup, so the guardrails apply without running "
            "/best-practices manually. Applies to new sessions; costs context tokens each session."
        ),
    )
    ask_user_question_policy: Literal["always", "ask_except_auto", "never", "auto_deliberate"] = (
        Field(
            default="ask_except_auto",
            description=(
                "Controls AskUserQuestion behavior: always ask, ask except in auto mode, "
                "never pause (best judgment), or auto_deliberate (in auto mode, run an "
                "advisor-assisted self-decision instead of dismissing)."
            ),
        )
    )
    auto_deliberate_destructive_actions: bool = Field(
        default=False,
        description=(
            "When true, destructive auto-approved actions are bounced once for agent "
            "deliberation before running. This is independent of AskUserQuestion policy."
        ),
    )
    skip_auto_prompt_injection: bool = Field(
        default=False,
        description=(
            "If true, suppress the auto-mode system reminder. "
            "Yolo mode does not inject a system reminder."
        ),
    )
    default_plan_mode: bool = Field(default=False, description="Default plan mode for new sessions")
    default_editor: str = Field(
        default="",
        description="Default external editor command (e.g. 'vim', 'code --wait')",
    )
    theme: Literal["dark", "light", "auto"] = Field(
        default="dark",
        description=(
            "Terminal color theme. Use 'light' for light terminal backgrounds, "
            "or 'auto' to detect the terminal background at startup (falls back "
            "to dark when detection is unavailable)."
        ),
    )
    show_thinking_stream: bool = Field(
        default=True,
        description=(
            "If true, stream the raw reasoning text in the live area as a "
            "6-line scrolling preview and commit the full reasoning markdown "
            "to history when the block ends. Default true. Set to false to "
            "show only the compact 'Thinking ...' indicator and a one-line "
            "trace summary."
        ),
    )
    prevent_idle_sleep: bool = Field(
        default=False,
        description=(
            "Prevent the computer from idle-sleeping while an agent turn is running. "
            "Supported on macOS, Linux, and Windows. Default: false."
        ),
    )
    auto_update: bool = Field(
        default=True,
        description="Automatically install new releases in the background at startup.",
    )
    models: dict[str, LLMModel] = Field(default_factory=dict, description="List of LLM models")
    providers: dict[str, LLMProvider] = Field(
        default_factory=dict, description="List of LLM providers"
    )
    loop_control: LoopControl = Field(default_factory=LoopControl, description="Agent loop control")
    goal: GoalConfig = Field(
        default_factory=GoalConfig, description="Thread-goal (/goal) configuration"
    )
    compact_prompt: str | None = Field(
        default=None,
        description=(
            "Override the built-in compaction summarization prompt. None keeps the "
            "default handoff-structured prompt; a per-invocation /compact focus is "
            "still appended on top."
        ),
    )
    model_switch_carryover: bool = Field(
        default=True,
        description=(
            "On /model switches, seed the new session with a plain-text summary "
            "of the conversation produced by the outgoing model. False keeps the "
            "previous start-fresh behavior."
        ),
    )
    background: BackgroundConfig = Field(
        default_factory=BackgroundConfig, description="Background task configuration"
    )
    notifications: NotificationConfig = Field(
        default_factory=NotificationConfig, description="Notification configuration"
    )
    services: Services = Field(default_factory=Services, description="Services configuration")
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="Memory configuration")
    web: WebConfig = Field(default_factory=WebConfig, description="Web fetch/search policy")
    feedback: FeedbackConfig = Field(
        default_factory=FeedbackConfig,
        description="User-submitted feedback endpoint configuration",
    )
    mcp: MCPConfig = Field(default_factory=MCPConfig, description="MCP configuration")
    tui: TUIConfig = Field(default_factory=TUIConfig, description="TUI rendering configuration")
    hooks: list[HookDef] = Field(default_factory=list, description="Hook definitions")  # pyright: ignore[reportUnknownVariableType]
    disabled_project_hooks: list[str] = Field(
        default_factory=list,
        exclude=True,
        description=(
            "Config files whose project-scope hooks were stripped because the "
            "project is untrusted. Populated at load; surfaced as a "
            "notification so non-shell frontends see it too."
        ),
    )
    merge_all_available_skills: bool = Field(
        default=True,
        description=(
            "Merge skills from all existing brand directories (pythinker/claude/codex) "
            "instead of using only the first one found. Defaults to true so users "
            "who keep skills in multiple brand directories see everything out of "
            "the box; set to false to restore the first-match-only behaviour."
        ),
    )
    extra_skill_dirs: list[str] = Field(
        default_factory=list,
        description=(
            "Extra directories to discover skills from, added on top of the "
            "built-in / user / project locations. Each entry may be an absolute "
            "path, ``~``-prefixed (expanded against $HOME), or relative to the "
            "project root (the nearest ``.git`` directory above the work dir). "
            "Missing paths are silently skipped."
        ),
    )
    telemetry: bool = Field(
        default=True,
        description=(
            "Enable anonymous telemetry to help improve pythinker-code. Set to false to opt out."
        ),
    )
    session_retention_days: int = Field(
        default=30,
        ge=0,
        description=(
            "Archived session directories in ~/.pythinker/sessions/ older than this many days "
            "are removed on startup. Plan files older than this many days are also pruned. "
            "0 disables cleanup."
        ),
    )

    @model_validator(mode="after")
    def validate_model(self) -> Self:
        self._apply_agent_execution_profile()
        if self.default_model and self.default_model not in self.models:
            raise ValueError(f"Default model {self.default_model} not found in models")
        for model in self.models.values():
            if model.provider not in self.providers:
                raise ValueError(f"Provider {model.provider} not found in providers")
        return self

    def _apply_agent_execution_profile(self) -> None:
        profile = self.agent_execution_profile
        if profile == "default":
            return
        fields_set = self.model_fields_set
        if profile == "review_safe":
            if "ask_user_question_policy" not in fields_set:
                self.ask_user_question_policy = "always"
        elif profile == "autonomous_coding":
            if "default_yolo" not in fields_set:
                self.default_yolo = True
            if "ask_user_question_policy" not in fields_set:
                # Preserve no-human AskUserQuestion behavior for headless autonomy.
                self.ask_user_question_policy = "never"
            if "auto_deliberate_destructive_actions" not in fields_set:
                self.auto_deliberate_destructive_actions = True
        elif profile == "plan_only":
            if "default_plan_mode" not in fields_set:
                self.default_plan_mode = True
            if "ask_user_question_policy" not in fields_set:
                self.ask_user_question_policy = "always"
        elif profile == "ci_fixer" and "ask_user_question_policy" not in fields_set:
            self.ask_user_question_policy = "ask_except_auto"


def get_config_file(*, create: bool = True) -> Path:
    """Get the configuration file path.

    Pass ``create=False`` to resolve the path without creating the share
    directory, for read-only callers that must avoid filesystem side effects.
    """
    return get_share_dir(create=create) / "config.toml"


def get_default_config() -> Config:
    """Get the default configuration."""
    return Config(
        default_model="",
        models={},
        providers={},
        services=Services(),
    )


def load_config(config_file: Path | None = None, *, persist: bool = True) -> Config:
    """Load configuration, resolving up to three scopes when no explicit file is given.

    When *config_file* is None (the default), the scoped pipeline runs:
    User (~/.pythinker/config.toml) → Project (.pythinker/config.toml) →
    Local (.pythinker/config.local.toml), merged with type-based rules.

    When *config_file* is given explicitly (e.g. via --config), that single
    file is loaded directly with no scope resolution — preserving the legacy
    behaviour used by tests and the CLI --config flag.

    Pass ``persist=False`` for read-only callers (e.g. ``pythinker system-prompt``)
    that must resolve the merged scoped config without any disk side effects:
    no share-dir creation, no default-config seeding, no JSON→TOML migration,
    no project-trust lock, and no auto-gitignore. Only the scoped (``config_file
    is None``) branch honours the flag; an explicit path is already read-or-seed.
    """
    if config_file is None:
        project_root = find_project_root(Path.cwd())
        return _load_scoped(project_root, persist=persist)

    # ── Explicit path: legacy single-file load (unchanged) ────────────────
    default_config_file = get_config_file().expanduser().resolve(strict=False)
    config_file = config_file.expanduser().resolve(strict=False)
    is_default_config_file = config_file == default_config_file
    logger.debug("Loading config from file: {file}", file=config_file)

    if is_default_config_file and not config_file.exists():
        migration_error = _migrate_json_config_to_toml()
        if migration_error is not None:
            raise ConfigError(
                f"Legacy config file has incompatible settings; please fix or "
                f"rename/delete {migration_error.config_file} to continue. "
                f"Errors: {migration_error.errors}"
            ) from None

    if not config_file.exists():
        config = get_default_config()
        logger.debug("No config file found, creating default config: {config}", config=config)
        save_config(config, config_file)
        config.is_from_default_location = is_default_config_file
        config.source_file = config_file
        return config

    try:
        config_text = config_file.read_text(encoding="utf-8")
        if config_file.suffix.lower() == ".json":
            data = json.loads(config_text)
        else:
            data = tomlkit.loads(config_text)
        _report_unknown_keys_single_source(data, str(config_file))
        config = Config.model_validate(data)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in configuration file {config_file}: {e}") from e
    except TOMLKitError as e:
        raise ConfigError(f"Invalid TOML in configuration file {config_file}: {e}") from e
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration file {config_file}: {e}") from e
    config.is_from_default_location = is_default_config_file
    config.source_file = config_file
    return config


def load_config_from_string(config_string: str) -> Config:
    """
    Load configuration from a TOML or JSON string.

    Args:
        config_string (str): TOML or JSON configuration text.

    Returns:
        Validated Config object.

    Raises:
        ConfigError: If the configuration text is invalid.
    """
    if not config_string.strip():
        raise ConfigError("Configuration text cannot be empty")

    json_error: json.JSONDecodeError | None = None
    try:
        data = json.loads(config_string)
    except json.JSONDecodeError as exc:
        json_error = exc
        data = None

    if data is None:
        try:
            data = tomlkit.loads(config_string)
        except TOMLKitError as toml_error:
            raise ConfigError(
                f"Invalid configuration text: {json_error}; {toml_error}"
            ) from toml_error

    _report_unknown_keys_single_source(data, "--config text")
    try:
        config = Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration text: {e}") from e
    config.is_from_default_location = False
    config.source_file = None
    return config


def save_config(config: Config, config_file: Path | None = None):
    """
    Save configuration to config file.

    Args:
        config (Config): Config object to save.
        config_file (Path | None): Path to the configuration file. If None, use default path.
    """
    config_file = config_file or get_config_file()
    logger.debug("Saving config to file: {file}", file=config_file)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_data = config.model_dump(mode="json", exclude_none=True)
    with open(config_file, "w", encoding="utf-8") as f:
        if config_file.suffix.lower() == ".json":
            f.write(json.dumps(config_data, ensure_ascii=False, indent=2))
        else:
            f.write(tomlkit.dumps(config_data))  # type: ignore[reportUnknownMemberType]
    with contextlib.suppress(OSError):
        os.chmod(config_file, 0o600)


class MigrationError(Exception):
    """Raised when legacy config migration fails due to incompatible schema."""

    def __init__(self, config_file: str, errors: list[str]) -> None:
        super().__init__(config_file, errors)
        self.config_file = config_file
        self.errors = errors


def _migrate_json_config_to_toml() -> MigrationError | None:
    """
    Attempt to migrate legacy JSON config to TOML.

    Returns:
        None on success.
        MigrationError if the JSON is valid but has incompatible schema — caller
        should raise ConfigError so the user can see their actual settings.

    Corrupt JSON is handled here: backed up so defaults take over silently.
    """
    old_json_config_file = get_share_dir() / "config.json"
    new_toml_config_file = get_share_dir() / "config.toml"

    if not old_json_config_file.exists():
        return None
    if new_toml_config_file.exists():
        return None

    logger.info(
        "Migrating legacy config file from {old} to {new}",
        old=old_json_config_file,
        new=new_toml_config_file,
    )

    backup_path = _next_legacy_config_backup_path(old_json_config_file)

    data: object | None = None
    json_decode_error: json.JSONDecodeError | None = None
    validation_errors: list[str] = []
    config: Config | None = None

    try:
        data = json.loads(old_json_config_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        json_decode_error = e

    if json_decode_error is not None:
        # Corrupt JSON: back up the file so load_config() creates a fresh config.
        logger.error(
            "Legacy config file is corrupt JSON ({err}); backing it up and skipping migration. "
            "Please fix or remove {file} to restore default behavior.",
            err=json_decode_error,
            file=old_json_config_file,
        )
        old_json_config_file.replace(backup_path)
        return None

    try:
        config = Config.model_validate(data)
    except ValidationError as e:
        validation_errors = [
            f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in e.errors()
        ]

    if config is not None:
        save_config(config, new_toml_config_file)
        old_json_config_file.replace(backup_path)
        logger.info("Legacy config migrated and backed up to {file}", file=backup_path)
        return None

    # Valid JSON but incompatible schema: keep the original in place so load_config()
    # raises ConfigError with the user's actual settings. This preserves user data.
    logger.warning(
        "Legacy config file has incompatible settings; please review and fix {file} or "
        "rename/delete it to use defaults. Errors: {errors}",
        file=old_json_config_file,
        errors=validation_errors,
    )
    return MigrationError(
        config_file=str(old_json_config_file),
        errors=validation_errors,
    )


def _next_legacy_config_backup_path(config_file: Path) -> Path:
    backup_path = config_file.with_name("config.json.bak")
    if not backup_path.exists():
        return backup_path
    index = 1
    while True:
        candidate = config_file.with_name(f"config.json.bak.{index}")
        if not candidate.exists():
            return candidate
        index += 1
