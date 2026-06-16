"""Content-Length JSON-RPC framing for LSP over stdio."""

from __future__ import annotations

import json
from asyncio import IncompleteReadError
from typing import Any, cast

from pythinker_host import AsyncReadable, AsyncWritable

# Upper bound on a single LSP frame body. A misbehaving or hostile server could
# otherwise send an enormous Content-Length and force an unbounded allocation in
# readexactly(). 64 MiB is far above any legitimate LSP payload.
MAX_CONTENT_LENGTH = 64 * 1024 * 1024


class LspProtocolError(Exception):
    """Malformed or invalid LSP frame or JSON-RPC error response."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class LspStartError(Exception):
    """Failed to start the language server process."""


class LspServerDown(Exception):
    """Language server process exited or the read loop failed."""


async def read_message(stdout: AsyncReadable) -> dict[str, Any]:
    """Read one LSP message framed with Content-Length headers."""
    content_length: int | None = None
    while True:
        line = await stdout.readline()
        if not line:
            raise LspServerDown("unexpected EOF while reading header")
        line_str = line.decode(encoding="utf-8", errors="strict").rstrip("\r\n")
        if line_str == "":
            break
        key, _, value = line_str.partition(":")
        if key.strip().lower() == "content-length":
            try:
                content_length = int(value.strip())
            except ValueError as exc:
                raise LspProtocolError(f"invalid Content-Length: {value.strip()!r}") from exc

    if content_length is None:
        raise LspProtocolError("missing Content-Length header")
    if content_length < 0:
        raise LspProtocolError(f"invalid Content-Length: {content_length}")
    if content_length > MAX_CONTENT_LENGTH:
        raise LspProtocolError(
            f"Content-Length {content_length} exceeds maximum {MAX_CONTENT_LENGTH}"
        )

    try:
        body = await stdout.readexactly(content_length)
    except IncompleteReadError as exc:
        raise LspServerDown("unexpected EOF while reading message body") from exc

    try:
        payload = json.loads(body.decode(encoding="utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LspProtocolError("invalid JSON body") from exc

    if not isinstance(payload, dict):
        raise LspProtocolError("LSP message must be a JSON object")
    return cast(dict[str, Any], payload)


async def write_message(stdin: AsyncWritable, message: dict[str, Any]) -> None:
    """Write one LSP message with Content-Length framing."""
    body = json.dumps(message, separators=(",", ":")).encode()
    header = f"Content-Length: {len(body)}\r\n\r\n".encode()
    stdin.write(header + body)
    await stdin.drain()
