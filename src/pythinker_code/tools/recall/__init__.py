"""Cross-session Recall tool (memory-1 / ctxmgmt-3).

Distilled JOURNAL recaps lose the load-bearing detail — exact commands, file paths,
why a fix was chosen — that an agent often needs to repeat or extend prior work.
Recall gives the model a designed, sanitized, workspace-scoped affordance to (1)
search prior sessions by keyword and (2) read a chosen session's transcript on
demand, instead of a brittle raw-file shell hatch.

Read-only and scoped to the current workspace's sessions. Transcript text is both
sanitized (secrets / injection-pattern blocks redacted via memory/sanitize) and
wrapped as untrusted data, since a prior transcript is untrusted historical input.
"""

import asyncio
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pythinker_core.message import Message
from pythinker_core.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue

from pythinker_code.memory.sanitize import sanitize_candidate_block
from pythinker_code.session import Session
from pythinker_code.soul.agent import Runtime
from pythinker_code.tools.utils import ToolResultBuilder, load_desc

NAME = "Recall"
_MAX_SEARCH_RESULTS = 10
_READ_BUDGET_CHARS = 16_000
# Session ids are uuid-like. Validate before Session.find so a crafted session_id
# (e.g. "../other-workspace") cannot traverse out of the workspace's sessions dir.
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class Params(BaseModel):
    mode: Literal["search", "read"] = Field(
        description="'search' to find prior sessions by keyword; 'read' to read one's transcript."
    )
    query: str | None = Field(
        default=None,
        description="Keywords to match against prior session titles (mode=search). "
        "Omit to list recent sessions.",
    )
    session_id: str | None = Field(
        default=None,
        description="The session_id to read, from a prior Recall search (mode=read).",
    )
    message_offset: int = Field(
        default=0,
        ge=0,
        description=(
            "For mode=read, skip this many rendered non-internal transcript messages before "
            "returning content."
        ),
    )
    max_messages: int | None = Field(
        default=None,
        ge=1,
        le=200,
        description="For mode=read, return at most this many rendered transcript messages.",
    )


def _session_search_blob(session: Session) -> str:
    """Lexical search text for a session (title, id, plan slug)."""
    title = (session.state.custom_title or session.title or "").strip()
    parts = [title, session.id]
    plan_slug = session.state.plan_slug
    if plan_slug:
        parts.append(plan_slug)
    return " ".join(parts).lower()


def _rank_sessions(
    sessions: list[Session], *, query: str, current_id: str, limit: int
) -> list[Session]:
    """Rank prior sessions by keyword overlap then recency (pure)."""
    terms = query.lower().split()
    scored: list[tuple[int, float, Session]] = []
    for session in sessions:
        if session.id == current_id:
            continue
        blob = _session_search_blob(session)
        score = sum(1 for term in terms if term in blob)
        if terms and score == 0:
            continue
        scored.append((score, session.updated_at, session))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [session for _score, _ts, session in scored[:limit]]


def _render_transcript(
    context_file: Path,
    budget: int,
    *,
    message_offset: int = 0,
    max_messages: int | None = None,
) -> str:
    """Render a session's message log into a budgeted, sanitized transcript.

    Internal (``_``-prefixed) roles are skipped. Each message's text is sanitized;
    a block that trips the secret/injection scanner becomes ``[redacted]`` rather
    than leaking or silently vanishing. Stops once the char budget is reached.
    """
    out: list[str] = []
    used = 0
    seen_messages = 0
    included_messages = 0
    try:
        # Stream line-by-line (not read_text) so a huge transcript cannot blow up
        # memory; errors="replace" tolerates a corrupt/binary line without crashing.
        with context_file.open(encoding="utf-8", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    msg = Message.model_validate_json(line)
                except Exception:
                    continue
                role = msg.role
                if role.startswith("_"):
                    continue
                text = msg.extract_text(" ").strip()
                tool_names = [call.function.name for call in (msg.tool_calls or [])]
                segment = text
                if tool_names:
                    segment = f"{segment} [tool calls: {', '.join(tool_names)}]".strip()
                if not segment:
                    continue
                if seen_messages < message_offset:
                    seen_messages += 1
                    continue
                if max_messages is not None and included_messages >= max_messages:
                    break
                clean = sanitize_candidate_block(segment)
                entry = f"[{role}] {clean if clean is not None else '[redacted]'}"
                if used + len(entry) + 1 > budget:
                    out.append("… (transcript truncated to fit the recall budget)")
                    break
                out.append(entry)
                seen_messages += 1
                included_messages += 1
                used += len(entry) + 1
    except OSError:
        return ""
    return "\n".join(out)


class Recall(CallableTool2[Params]):
    name: str = NAME
    supports_parallel: bool = True
    params: type[Params] = Params

    def __init__(self, runtime: Runtime):
        super().__init__(description=load_desc(Path(__file__).parent / "description.md"))
        self._runtime = runtime

    async def __call__(self, params: Params) -> ToolReturnValue:
        if params.mode == "search":
            return await self._search((params.query or "").strip())
        session_id = (params.session_id or "").strip()
        if not session_id:
            return ToolError(
                message='mode="read" requires session_id (from a Recall search).',
                brief="Missing session_id",
            )
        if not _SAFE_SESSION_ID.match(session_id):
            # Reject anything that isn't a plain session id so a crafted value
            # cannot traverse outside the workspace's sessions directory.
            return ToolError(
                message=f"Invalid session_id: {session_id!r}.",
                brief="Invalid session_id",
            )
        return await self._read(
            session_id,
            message_offset=params.message_offset,
            max_messages=params.max_messages,
        )

    async def _search(self, query: str) -> ToolReturnValue:
        work_dir = self._runtime.work_dir
        try:
            sessions = await Session.list(work_dir)
        except Exception as exc:
            return ToolError(message=f"Failed to list prior sessions: {exc}", brief="Recall failed")

        top = _rank_sessions(
            sessions,
            query=query,
            current_id=self._runtime.session.id,
            limit=_MAX_SEARCH_RESULTS,
        )
        if not top:
            return ToolOk(
                output="No matching prior sessions in this workspace.",
                message="No matches.",
            )
        lines = ["Prior sessions in this workspace (most relevant first):", ""]
        for session in top:
            title = session.state.custom_title or session.title or "(untitled)"
            lines.append(f"- session_id: {session.id}")
            lines.append(f"  title: {title}")
        lines.append("")
        lines.append('Read one with Recall(mode="read", session_id="...").')
        return ToolOk(output="\n".join(lines), message=f"Found {len(top)} prior session(s).")

    async def _read(
        self, session_id: str, *, message_offset: int, max_messages: int | None
    ) -> ToolReturnValue:
        if session_id == self._runtime.session.id:
            return ToolError(message="Cannot recall the current session.", brief="Current session")
        work_dir = self._runtime.work_dir
        try:
            session = await Session.find(work_dir, session_id)
        except Exception as exc:
            return ToolError(message=f"Failed to open session: {exc}", brief="Recall failed")
        if session is None:
            return ToolError(
                message=(
                    f"No session {session_id} in this workspace. "
                    'Use Recall(mode="search") to find valid session_ids.'
                ),
                brief="Unknown session",
            )

        rendered = await asyncio.to_thread(
            _render_transcript,
            session.context_file,
            _READ_BUDGET_CHARS,
            message_offset=message_offset,
            max_messages=max_messages,
        )
        if not rendered:
            return ToolOk(
                output="(this prior session has no readable transcript)",
                message="Empty transcript.",
            )
        builder = ToolResultBuilder()
        builder.write(rendered)
        builder.mark_untrusted()
        return builder.ok(f"Recalled transcript of prior session {session_id}.")
