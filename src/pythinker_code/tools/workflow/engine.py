"""Runtime-agnostic dynamic-workflow engine.

Parses a model-written Python workflow script, AST-validates it (literal `meta`
first, no imports / `time` / `random` / `datetime`), and runs the remaining
statements inside an `async def` wrapper in a restricted namespace whose
`agent()`/`parallel()`/`pipeline()` primitives orchestrate subagents through an
injected `agent_runner` callback.

This is NOT a security sandbox: the host agent already has shell and file tools,
so the script can run nothing the model could not already run. The AST checks
exist for reproducibility and a parseable `meta`, mirroring the reference's
determinism rules — not for isolation.
"""

from __future__ import annotations

import ast
import asyncio
import builtins as _builtins
import inspect
import json
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

_FORBIDDEN_NAME_LOADS = {
    "__import__",
    "eval",
    "exec",
    "compile",
    "open",
    "input",
    "globals",
    "locals",
    "vars",
    "time",
    "random",
    "datetime",
    "os",
    "sys",
}


class WorkflowScriptError(Exception):
    """Raised when a workflow script is structurally invalid."""


class WorkflowMetaPhase:
    __slots__ = ("title", "detail", "model")

    def __init__(self, title: str, detail: str | None = None, model: str | None = None) -> None:
        self.title = title
        self.detail = detail
        self.model = model


class WorkflowMeta:
    __slots__ = ("name", "description", "when_to_use", "phases")

    def __init__(
        self,
        name: str,
        description: str,
        when_to_use: str | None = None,
        phases: tuple[WorkflowMetaPhase, ...] = (),
    ) -> None:
        self.name = name
        self.description = description
        self.when_to_use = when_to_use
        self.phases = phases


def parse_workflow_script(script: str) -> tuple[WorkflowMeta, list[ast.stmt]]:
    """Parse + validate a workflow script. Returns (meta, post-meta statements)."""
    try:
        tree = ast.parse(script, filename="<workflow>", mode="exec")
    except SyntaxError as exc:
        raise WorkflowScriptError(f"workflow script is not valid Python: {exc}") from exc
    if not tree.body:
        raise WorkflowScriptError("workflow script is empty")
    meta = _extract_meta(tree.body[0])
    _assert_deterministic(tree)
    return meta, tree.body[1:]


def _extract_meta(node: ast.stmt) -> WorkflowMeta:
    if not (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "meta"
    ):
        raise WorkflowScriptError("the first statement must be `meta = { ... }`")
    try:
        raw = ast.literal_eval(node.value)
    except (ValueError, SyntaxError, TypeError) as exc:
        raise WorkflowScriptError(
            "meta must be a literal dict (no function calls, names, or interpolation)"
        ) from exc
    return _validate_meta(raw)


def _validate_meta(raw: Any) -> WorkflowMeta:
    if not isinstance(raw, dict):
        raise WorkflowScriptError("meta must be a dict")
    # isinstance narrows Any to dict[Unknown, Unknown]; cast to the correct type.
    data = cast(dict[str, Any], raw)
    name: Any = data.get("name")
    description: Any = data.get("description")
    if not isinstance(name, str) or not name.strip():
        raise WorkflowScriptError("meta.name must be a non-empty string")
    if not isinstance(description, str) or not description.strip():
        raise WorkflowScriptError("meta.description must be a non-empty string")
    when_to_use: Any = data.get("when_to_use")
    if when_to_use is not None and not isinstance(when_to_use, str):
        raise WorkflowScriptError("meta.when_to_use must be a string")
    phases_raw: Any = data.get("phases", [])
    if not isinstance(phases_raw, list):
        raise WorkflowScriptError("meta.phases must be a list")
    phases: list[WorkflowMetaPhase] = []
    for entry in cast(list[Any], phases_raw):
        if not isinstance(entry, dict):
            raise WorkflowScriptError("each meta phase must have a title string")
        entry_d = cast(dict[str, Any], entry)
        if not isinstance(entry_d.get("title"), str):
            raise WorkflowScriptError("each meta phase must have a title string")
        phases.append(
            WorkflowMetaPhase(entry_d["title"], entry_d.get("detail"), entry_d.get("model"))
        )
    return WorkflowMeta(name.strip(), description.strip(), when_to_use, tuple(phases))


def _assert_deterministic(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise WorkflowScriptError("import statements are not allowed in a workflow script")
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in _FORBIDDEN_NAME_LOADS
        ):
            raise WorkflowScriptError(
                f"`{node.id}` is not allowed: workflow scripts must be deterministic and "
                "may not import modules or read the clock / RNG"
            )


# ---------------------------------------------------------------------------
# Runtime half — execution primitives and run_workflow entry point
# ---------------------------------------------------------------------------

AgentRunner = Callable[[str, "AgentOptions"], Awaitable[Any]]

_SAFE_BUILTIN_NAMES = (
    "len",
    "range",
    "enumerate",
    "list",
    "dict",
    "set",
    "tuple",
    "frozenset",
    "str",
    "int",
    "float",
    "bool",
    "bytes",
    "sorted",
    "reversed",
    "min",
    "max",
    "sum",
    "any",
    "all",
    "map",
    "filter",
    "zip",
    "abs",
    "round",
    "isinstance",
    "repr",
    "Exception",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
)


class WorkflowRuntimeError(Exception):
    """Raised when a workflow script misuses a primitive at runtime."""


class AgentOptions:
    __slots__ = ("label", "phase", "schema", "model", "agent_type")

    def __init__(
        self,
        label: str | None = None,
        phase: str | None = None,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        agent_type: str | None = None,
    ) -> None:
        self.label = label
        self.phase = phase
        self.schema = schema
        self.model = model
        self.agent_type = agent_type


class AgentStartEvent:
    __slots__ = ("label", "phase", "prompt")

    def __init__(self, label: str, phase: str | None, prompt: str) -> None:
        self.label = label
        self.phase = phase
        self.prompt = prompt


class AgentEndEvent:
    __slots__ = ("label", "phase", "result", "error")

    def __init__(
        self, label: str, phase: str | None, result: Any, error: str | None = None
    ) -> None:
        self.label = label
        self.phase = phase
        self.result = result
        self.error = error


class RunWorkflowHooks:
    __slots__ = ("on_log", "on_phase", "on_agent_start", "on_agent_end")

    def __init__(
        self,
        on_log: Callable[[str], None] | None = None,
        on_phase: Callable[[str], None] | None = None,
        on_agent_start: Callable[[AgentStartEvent], None] | None = None,
        on_agent_end: Callable[[AgentEndEvent], None] | None = None,
    ) -> None:
        self.on_log = on_log
        self.on_phase = on_phase
        self.on_agent_start = on_agent_start
        self.on_agent_end = on_agent_end


class WorkflowRunResult:
    __slots__ = ("meta", "result", "logs", "phases", "agent_count")

    def __init__(
        self,
        meta: WorkflowMeta,
        result: Any,
        logs: list[str],
        phases: list[str],
        agent_count: int,
    ) -> None:
        self.meta = meta
        self.result = result
        self.logs = logs
        self.phases = phases
        self.agent_count = agent_count


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise WorkflowRuntimeError(f"{name} must be a string")
    return value


def _normalize_options(value: Any) -> AgentOptions:
    if value is None:
        return AgentOptions()
    if not isinstance(value, dict):
        raise WorkflowRuntimeError("agent options must be a dict")
    d = cast(dict[str, Any], value)
    return AgentOptions(
        label=d.get("label"),
        phase=d.get("phase"),
        schema=d.get("schema"),
        model=d.get("model"),
        agent_type=d.get("agent_type") or d.get("agentType"),
    )


def _default_label(phase: str | None, index: int) -> str:
    return f"{phase} agent {index}" if phase else f"agent {index}"


def _estimate_tokens(value: Any) -> int:
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return math.ceil(len(text) / 4)


def _close_coroutines(value: Any) -> None:
    if inspect.iscoroutine(value):
        value.close()
    elif isinstance(value, dict):
        for item in cast(dict[str, Any], value).values():
            _close_coroutines(item)
    elif isinstance(value, (list, tuple, set)):
        for item in cast(list[Any], value):
            _close_coroutines(item)


def _assert_no_coroutines(value: Any) -> None:
    found = _contains_coroutine(value)
    if found:
        _close_coroutines(value)
        raise WorkflowRuntimeError(
            "workflow result contains a coroutine; did you forget to await "
            "agent(), parallel(), or pipeline()?"
        )


def _contains_coroutine(value: Any) -> bool:
    if inspect.iscoroutine(value):
        return True
    if isinstance(value, dict):
        return any(_contains_coroutine(v) for v in cast(dict[str, Any], value).values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_coroutine(v) for v in cast(list[Any], value))
    return False


async def run_workflow(
    script: str,
    *,
    agent_runner: AgentRunner,
    args: Any = None,
    cwd: str = ".",
    concurrency: int = 8,
    token_budget: int | None = None,
    hooks: RunWorkflowHooks | None = None,
) -> WorkflowRunResult:
    meta, body = parse_workflow_script(script)
    hooks = hooks or RunWorkflowHooks()
    logs: list[str] = []
    phases: list[str] = []
    state: dict[str, Any] = {"current_phase": None, "agent_count": 0, "spent": 0}
    semaphore = asyncio.Semaphore(max(1, concurrency))

    def log(message: Any) -> None:
        text = str(message)
        logs.append(text)
        if hooks.on_log:
            hooks.on_log(text)

    def phase(title: Any) -> None:
        text = _require_str(title, "phase title")
        state["current_phase"] = text
        if text not in phases:
            phases.append(text)
        if hooks.on_phase:
            hooks.on_phase(text)

    class _Budget:
        total = token_budget

        @staticmethod
        def spent() -> int:
            return state["spent"]

        @staticmethod
        def remaining() -> float:
            if token_budget is None:
                return math.inf
            return max(0, token_budget - state["spent"])

    budget = _Budget()

    async def agent(prompt: Any, options: Any = None) -> Any:
        task_prompt = _require_str(prompt, "agent prompt")
        opts = _normalize_options(options)
        assigned_phase = opts.phase or state["current_phase"]
        if token_budget is not None and budget.remaining() <= 0:
            raise WorkflowRuntimeError("workflow token budget exhausted")
        async with semaphore:
            state["agent_count"] += 1
            label = (opts.label or "").strip() or _default_label(
                assigned_phase, state["agent_count"]
            )
            opts.label = label
            opts.phase = assigned_phase
            if hooks.on_agent_start:
                hooks.on_agent_start(AgentStartEvent(label, assigned_phase, task_prompt))
            try:
                result = await agent_runner(task_prompt, opts)
            except asyncio.CancelledError:
                if hooks.on_agent_end:
                    hooks.on_agent_end(
                        AgentEndEvent(label, assigned_phase, None, error="cancelled")
                    )
                raise
            except Exception as exc:  # noqa: BLE001 - reference parity: branch fails to None
                log(f"agent {label} failed: {exc}")
                if hooks.on_agent_end:
                    hooks.on_agent_end(AgentEndEvent(label, assigned_phase, None, error=str(exc)))
                return None
            state["spent"] += _estimate_tokens(result)
            if hooks.on_agent_end:
                hooks.on_agent_end(AgentEndEvent(label, assigned_phase, result))
            return result

    async def parallel(items: Sequence[Any]) -> list[Any]:
        if not isinstance(items, (list, tuple)):
            raise WorkflowRuntimeError("parallel() expects a list of awaitables")
        for item in items:
            if callable(item) and not inspect.isawaitable(item):
                raise WorkflowRuntimeError(
                    "parallel() expects awaitables, not functions: "
                    "use parallel([agent('...'), agent('...')])"
                )

        async def guarded(index: int, item: Any) -> Any:
            try:
                return await item
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reference parity
                log(f"parallel[{index}] failed: {exc}")
                return None

        return list(await asyncio.gather(*(guarded(i, it) for i, it in enumerate(items))))

    async def pipeline(items: Any, *stages: Any) -> list[Any]:
        if not isinstance(items, (list, tuple)):
            raise WorkflowRuntimeError("pipeline() expects a list as the first argument")
        for stage in stages:
            if not callable(stage):
                raise WorkflowRuntimeError("pipeline() stages must be callables")
        typed_items: list[Any] = list(cast(Any, items))

        async def run_item(index: int, item: Any) -> Any:
            value: Any = item
            for stage in stages:
                try:
                    produced = stage(value, item, index)
                    value = await produced if inspect.isawaitable(produced) else produced
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - reference parity
                    log(f"pipeline[{index}] failed: {exc}")
                    return None
            return value

        return list(await asyncio.gather(*(run_item(i, it) for i, it in enumerate(typed_items))))

    def _print(*a: Any, **_kw: Any) -> None:
        log(" ".join(str(x) for x in a))

    namespace: dict[str, Any] = {
        "__builtins__": {name: getattr(_builtins, name) for name in _SAFE_BUILTIN_NAMES},
        "agent": agent,
        "parallel": parallel,
        "pipeline": pipeline,
        "phase": phase,
        "log": log,
        "print": _print,
        "budget": budget,
        "args": args,
        "cwd": cwd,
        "json": json,
        "math": math,
    }

    result = await _execute(body, namespace)
    _assert_no_coroutines(result)
    return WorkflowRunResult(
        meta=meta,
        result=result,
        logs=logs,
        phases=phases,
        agent_count=int(state["agent_count"]),
    )


async def _execute(body: list[ast.stmt], namespace: dict[str, Any]) -> Any:
    func = ast.AsyncFunctionDef(
        name="__workflow_main__",
        args=ast.arguments(
            posonlyargs=[],
            args=[],
            vararg=None,
            kwonlyargs=[],
            kw_defaults=[],
            kwarg=None,
            defaults=[],
        ),
        body=body or [ast.Pass()],
        decorator_list=[],
        returns=None,
        type_comment=None,
        type_params=[],
    )
    module = ast.Module(body=[func], type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, filename="<workflow>", mode="exec")
    exec(code, namespace)  # noqa: S102 - not a security boundary; see module docstring
    return await namespace["__workflow_main__"]()
