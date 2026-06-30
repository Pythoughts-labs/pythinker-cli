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
