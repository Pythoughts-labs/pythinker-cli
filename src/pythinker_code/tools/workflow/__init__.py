import hashlib
import json
import re
from pathlib import Path
from typing import Any, override

import jsonschema
from pydantic import BaseModel, Field
from pythinker_core.message import ContentPart, TextPart
from pythinker_core.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue

from pythinker_code.soul import wire_send
from pythinker_code.soul.agent import Runtime
from pythinker_code.tools.agent import AgentTool
from pythinker_code.tools.agent import Params as AgentParams
from pythinker_code.tools.utils import load_desc
from pythinker_code.tools.workflow.display import WorkflowSnapshot, render_progress
from pythinker_code.tools.workflow.engine import (
    AgentEndEvent,
    AgentOptions,
    AgentStartEvent,
    RunWorkflowHooks,
    WorkflowScriptError,
    parse_workflow_script,
    run_workflow,
)
from pythinker_code.utils.logging import logger
from pythinker_code.wire.types import ProgressNote

_FENCE_RE = re.compile(r"^```(?:py|python)?\s*\n([\s\S]*?)\n```$", re.IGNORECASE)
_JSON_BLOCK_RE = re.compile(r"```json\s*\n([\s\S]*?)\n```", re.IGNORECASE)
_SUMMARY_SEP = "\n[summary]\n"


class Params(BaseModel):
    script: str = Field(
        description=(
            "Raw Python workflow script (no Markdown fences). First statement must be "
            '`meta = {"name": ..., "description": ...}`. Must call agent() at least once '
            "and return a JSON-serializable value."
        )
    )
    args: Any | None = Field(
        default=None,
        description="Optional JSON value exposed to the script as the global `args`.",
    )
    token_budget: int | None = Field(
        default=None,
        description=(
            "Optional cap on estimated total tokens spent by spawned subagents. When set, "
            "the script's `budget.remaining()` reaches 0 once the cap is hit and further "
            "agent() calls raise inside the engine: within parallel()/pipeline(), that one "
            "call is caught and returns None (logged), but a bare `await agent(...)` propagates "
            "and fails the whole workflow run. Leave unset for no cap (budget.remaining() "
            "stays unbounded)."
        ),
        ge=1,
    )


class Workflow(CallableTool2[Params]):
    name: str = "Workflow"
    params: type[Params] = Params
    emits_tool_execution_started_after_approval = True

    def __init__(self, runtime: Runtime) -> None:
        super().__init__(description=load_desc(Path(__file__).parent / "description.md"))
        self._runtime = runtime
        self._agent_tool = AgentTool(runtime)

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        if self._runtime.role != "root":
            return ToolError(
                message="Workflow can only run from the root agent; subagents may not "
                "launch nested workflows.",
                brief="Not allowed in subagent",
            )

        script = _strip_fences(params.script)
        try:
            meta, _ = parse_workflow_script(script)
        except WorkflowScriptError as exc:
            return ToolError(message=str(exc), brief="Invalid workflow script")

        fingerprint = _fingerprint(script, params.args)
        if self._runtime.approval.is_orchestration_approved(fingerprint):
            from pythinker_code.soul.toolset import emit_current_tool_execution_started

            emit_current_tool_execution_started()
        else:
            approval = await self._runtime.approval.request(
                self.name,
                "run workflow orchestration",
                f"Run workflow `{meta.name}`: {meta.description}",
            )
            if not approval:
                return approval.rejection_error()
            self._runtime.approval.approve_orchestration(fingerprint)

        snapshot = WorkflowSnapshot(name=meta.name, description=meta.description)

        def emit() -> None:
            wire_send(ProgressNote(title=f"Workflow {meta.name}", body=render_progress(snapshot)))

        def on_phase(title: str) -> None:
            snapshot.add_phase(title)
            emit()

        def on_agent_start(event: AgentStartEvent) -> None:
            snapshot.start_agent(event.label, event.phase)
            emit()

        def on_agent_end(event: AgentEndEvent) -> None:
            snapshot.end_agent(event.label, error=event.error)
            emit()

        hooks = RunWorkflowHooks(
            on_log=lambda _m: emit(),
            on_phase=on_phase,
            on_agent_start=on_agent_start,
            on_agent_end=on_agent_end,
        )

        try:
            result = await run_workflow(
                script,
                agent_runner=self._agent_runner,
                args=params.args,
                cwd=str(self._runtime.work_dir),
                concurrency=max(1, self._runtime.config.background.max_running_tasks),
                token_budget=params.token_budget,
                hooks=hooks,
            )
        except WorkflowScriptError as exc:
            return ToolError(message=str(exc), brief="Invalid workflow script")
        except Exception as exc:  # noqa: BLE001 - converted to a typed ToolError below
            # asyncio.CancelledError is a BaseException in 3.12+ and is NOT caught here;
            # it propagates so the host can abort the tool. The engine already fires
            # on_agent_end(..., error="cancelled") for in-flight agents before
            # re-raising, so the incrementally-emitted snapshot already shows them.
            logger.exception("Workflow run failed")
            return ToolError(message=f"workflow failed: {exc}", brief="Workflow failed")

        if result.agent_count == 0:
            return ToolError(
                message="workflow scripts must call agent() at least once; this workflow "
                "declared phases but ran no subagents.",
                brief="No agents run",
            )

        output = json.dumps(result.result, indent=2, default=str)
        return ToolOk(
            output=output,
            message=f"Workflow {meta.name} completed with {result.agent_count} agent(s).",
            brief=f"{result.agent_count} agent(s), {len(result.phases)} phase(s)",
        )

    async def _agent_runner(self, prompt: str, opts: AgentOptions) -> Any:
        if opts.schema is None:
            text, is_error, message = await self._run_child(prompt, opts)
            if is_error:
                raise RuntimeError(message or "subagent failed")
            return text

        # Schema mode: instruct, parse, validate; one retry with the error fed back.
        instructed = prompt + "\n\n" + _schema_instructions(opts.schema)
        last_error = ""
        for _attempt in range(2):
            text, is_error, message = await self._run_child(instructed, opts)
            if is_error:
                raise RuntimeError(message or "subagent failed")
            value, err = _parse_and_validate(text, opts.schema)
            if err is None:
                return value
            last_error = err
            instructed = (
                prompt
                + "\n\n"
                + _schema_instructions(opts.schema)
                + f"\n\nYour previous output was invalid: {err}\nReturn a valid ```json block."
            )
        raise RuntimeError(f"subagent never produced schema-valid output: {last_error}")

    async def _run_child(self, prompt: str, opts: AgentOptions) -> tuple[str, bool, str]:
        child_params = AgentParams(
            description=opts.label or "workflow agent",
            prompt=prompt,
            subagent_type=opts.agent_type or "coder",
            model=opts.model,
            run_in_background=False,
        )
        ret = await self._agent_tool(child_params)
        output = ret.output if isinstance(ret.output, str) else _content_text(ret.output)
        return _extract_summary(output), ret.is_error, ret.message


def _strip_fences(script: str) -> str:
    text = script.strip()
    match = _FENCE_RE.match(text)
    return match.group(1).strip() if match else text


def _fingerprint(script: str, args: Any) -> str:
    payload = json.dumps({"script": script, "args": args}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _extract_summary(output: str) -> str:
    if _SUMMARY_SEP in output:
        return output.split(_SUMMARY_SEP, 1)[1].strip()
    return output.strip()


def _content_text(parts: list[ContentPart]) -> str:
    # AgentTool returns a string; this is a defensive fallback for ContentPart lists.
    return "".join(part.text for part in parts if isinstance(part, TextPart))


def _schema_instructions(schema: dict[str, Any]) -> str:
    return (
        "Final output contract: your final message MUST end with a single fenced "
        "```json block whose contents validate against this JSON Schema:\n"
        + json.dumps(schema, indent=2)
        + "\nDo not write prose after the json block."
    )


def _parse_and_validate(text: str, schema: dict[str, Any]) -> tuple[Any, str | None]:
    blocks = _JSON_BLOCK_RE.findall(text)
    raw = blocks[-1] if blocks else text
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"output was not valid JSON ({exc})"
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError as exc:
        return None, f"output did not match schema ({exc.message})"
    return value, None
