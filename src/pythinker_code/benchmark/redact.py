from __future__ import annotations

import json
import re
from typing import Any, cast

_MAX_STRING_CHARS = 8_000
_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(cookie\s*:\s*)[^\n\r]+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s\"']+"),
    re.compile(r"(?i)(access[_-]?token\s*[=:]\s*)[^\s\"']+"),
    re.compile(r"(?i)(refresh[_-]?token\s*[=:]\s*)[^\s\"']+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}\b"),
]


def redact_text(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda m: f"{m.group(1)}<redacted>" if m.groups() else "<redacted>",
            redacted,
        )
    if len(redacted) > _MAX_STRING_CHARS:
        return redacted[:_MAX_STRING_CHARS] + "\n<truncated>"
    return redacted


def redact_json(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(k): redact_json(v) for k, v in cast(dict[object, object], value).items()}
    if isinstance(value, list):
        return [redact_json(item) for item in cast(list[object], value)]
    return value


def dumps_redacted(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(redact_json(value), indent=indent, ensure_ascii=False)
