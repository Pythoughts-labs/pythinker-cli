"""Diagnostic aggregation for passive LSP feedback."""

from __future__ import annotations

import json
from collections import OrderedDict, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

from pythinker_code.lsp.protocol import (
    Diagnostic,
    DiagnosticSeverity,
    PublishDiagnosticsParams,
    Range,
)
from pythinker_code.utils.logging import logger

MAX_DIAGNOSTICS_PER_FILE = 10
MAX_DIAGNOSTICS_TOTAL = 30
SENT_FILE_LRU_CAP = 500
_HANDLER_FAILURE_WARN_THRESHOLD = 3

_SEVERITY_LABELS: Mapping[int, str] = {
    DiagnosticSeverity.ERROR: "Error",
    DiagnosticSeverity.WARNING: "Warning",
    DiagnosticSeverity.INFORMATION: "Information",
    DiagnosticSeverity.HINT: "Hint",
}


@dataclass(frozen=True, slots=True)
class DiagnosticEntry:
    message: str
    severity: int
    range: Range
    source: str | None = None
    code: str | int | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticFile:
    uri: str
    path: str
    diagnostics: list[DiagnosticEntry]


@dataclass(frozen=True, slots=True)
class ServerDiagnostics:
    server_name: str
    files: list[DiagnosticFile]


def uri_to_path(uri: str) -> str | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return unquote(parsed.path)


def diagnostic_entry_from_lsp(diagnostic: Diagnostic) -> DiagnosticEntry:
    severity = int(diagnostic.severity or DiagnosticSeverity.HINT)
    return DiagnosticEntry(
        message=diagnostic.message,
        severity=severity,
        range=diagnostic.range,
        source=diagnostic.source,
        code=diagnostic.code,
    )


def diagnostic_key(entry: DiagnosticEntry) -> str:
    return json.dumps(
        {
            "message": entry.message,
            "severity": entry.severity,
            "range": {
                "start": {
                    "line": entry.range.start.line,
                    "character": entry.range.start.character,
                },
                "end": {
                    "line": entry.range.end.line,
                    "character": entry.range.end.character,
                },
            },
            "source": entry.source or None,
            "code": entry.code if entry.code is not None else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def render_diagnostics_block(groups: list[ServerDiagnostics]) -> str:
    lines = [
        "LSP diagnostics from connected language servers "
        "(point-in-time; may be stale by the time you act):\n"
    ]
    for group in groups:
        lines.append(f"[{group.server_name}]")
        for file in group.files:
            lines.append(f"{file.path}:")
            for diag in file.diagnostics:
                label = _SEVERITY_LABELS.get(diag.severity, "Diagnostic")
                start = diag.range.start
                location = f"{start.line + 1}:{start.character + 1}"
                code_part = f" [{diag.code}]" if diag.code is not None else ""
                source_part = f" ({diag.source})" if diag.source else ""
                lines.append(f"  {label} ({location}){code_part}{source_part}: {diag.message}")
        lines.append("")
    return "\n".join(lines).rstrip()


class DiagnosticRegistry:
    """Stores, deduplicates, and volume-limits LSP diagnostics for passive injection."""

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, list[DiagnosticEntry]]] = {}
        self._pending_paths: dict[str, dict[str, str]] = {}
        self._sent_keys: OrderedDict[str, set[str]] = OrderedDict()

    def register_pending(self, server_name: str, files: list[DiagnosticFile]) -> None:
        if not files:
            return
        server_pending = self._pending.setdefault(server_name, {})
        server_paths = self._pending_paths.setdefault(server_name, {})
        for file in files:
            if not file.diagnostics:
                # Empty payload is an LSP "clear all diagnostics for this URI" signal.
                server_pending.pop(file.uri, None)
                server_paths.pop(file.uri, None)
                continue
            server_paths[file.uri] = file.path
            entries = server_pending.setdefault(file.uri, [])
            seen_in_batch: set[str] = set()
            for diagnostic in file.diagnostics:
                key = diagnostic_key(diagnostic)
                if key in seen_in_batch:
                    continue
                seen_in_batch.add(key)
                entries.append(diagnostic)

    def check_for_diagnostics(self) -> list[ServerDiagnostics]:
        candidates: list[tuple[str, str, str, DiagnosticEntry, str]] = []
        for server_name, file_map in self._pending.items():
            paths = self._pending_paths.get(server_name, {})
            for file_uri, diagnostics in file_map.items():
                path = paths.get(file_uri) or uri_to_path(file_uri) or file_uri
                deduped: list[tuple[DiagnosticEntry, str]] = []
                for diagnostic in diagnostics:
                    key = diagnostic_key(diagnostic)
                    if self._is_sent(file_uri, key):
                        continue
                    deduped.append((diagnostic, key))
                if not deduped:
                    continue
                deduped.sort(
                    key=lambda item: (
                        item[0].severity,
                        item[0].range.start.line,
                        item[0].range.start.character,
                    )
                )
                for diagnostic, key in deduped[:MAX_DIAGNOSTICS_PER_FILE]:
                    candidates.append((server_name, file_uri, path, diagnostic, key))

        candidates.sort(
            key=lambda item: (
                item[3].severity,
                item[2],
                item[3].range.start.line,
                item[3].range.start.character,
            )
        )
        selected = candidates[:MAX_DIAGNOSTICS_TOTAL]
        if not selected:
            return []

        selected_keys: dict[str, set[str]] = defaultdict(set)
        for _, file_uri, _, _, key in selected:
            self._mark_sent(file_uri, key)
            selected_keys[file_uri].add(key)

        self._remove_selected_from_pending(selected_keys)
        return _group_selected(selected)

    def clear_all(self) -> None:
        self._pending.clear()
        self._pending_paths.clear()
        self._sent_keys.clear()

    def clear_for_file(self, file_uri: str) -> None:
        for server_name in list(self._pending.keys()):
            file_map = self._pending.get(server_name)
            if file_map is not None:
                file_map.pop(file_uri, None)
                if not file_map:
                    self._pending.pop(server_name, None)
            paths = self._pending_paths.get(server_name)
            if paths is not None:
                paths.pop(file_uri, None)
                if not paths:
                    self._pending_paths.pop(server_name, None)
        self._sent_keys.pop(file_uri, None)

    @property
    def pending_count(self) -> int:
        return sum(
            len(entries) for file_map in self._pending.values() for entries in file_map.values()
        )

    def _is_sent(self, file_uri: str, key: str) -> bool:
        sent = self._sent_keys.get(file_uri)
        return sent is not None and key in sent

    def _mark_sent(self, file_uri: str, key: str) -> None:
        if file_uri in self._sent_keys:
            self._sent_keys.move_to_end(file_uri)
            self._sent_keys[file_uri].add(key)
        else:
            self._sent_keys[file_uri] = {key}
        while len(self._sent_keys) > SENT_FILE_LRU_CAP:
            self._sent_keys.popitem(last=False)

    def _remove_selected_from_pending(self, selected_keys: dict[str, set[str]]) -> None:
        for server_name, file_map in list(self._pending.items()):
            for file_uri, keys in selected_keys.items():
                diagnostics = file_map.get(file_uri)
                if diagnostics is None:
                    continue
                remaining = [
                    diagnostic
                    for diagnostic in diagnostics
                    if diagnostic_key(diagnostic) not in keys
                ]
                if remaining:
                    file_map[file_uri] = remaining
                else:
                    file_map.pop(file_uri, None)
                    paths = self._pending_paths.get(server_name)
                    if paths is not None:
                        paths.pop(file_uri, None)
            if not file_map:
                self._pending.pop(server_name, None)
                self._pending_paths.pop(server_name, None)


def _group_selected(
    selected: list[tuple[str, str, str, DiagnosticEntry, str]],
) -> list[ServerDiagnostics]:
    grouped: dict[str, dict[str, tuple[str, list[DiagnosticEntry]]]] = defaultdict(dict)
    for server_name, file_uri, path, diagnostic, _ in selected:
        file_bucket = grouped[server_name].get(file_uri)
        if file_bucket is None:
            grouped[server_name][file_uri] = (path, [diagnostic])
        else:
            _, diagnostics = file_bucket
            diagnostics.append(diagnostic)

    result: list[ServerDiagnostics] = []
    for server_name in sorted(grouped.keys()):
        files = [
            DiagnosticFile(uri=file_uri, path=path, diagnostics=diagnostics)
            for file_uri, (path, diagnostics) in sorted(grouped[server_name].items())
        ]
        result.append(ServerDiagnostics(server_name=server_name, files=files))
    return result


def register_publish_diagnostics_handler(
    registry: DiagnosticRegistry,
    server_name: str,
    instance: Any,
) -> None:
    """Wire ``textDocument/publishDiagnostics`` for one server instance."""
    failure_count = 0
    warned = False

    async def handler(params: Any) -> None:
        nonlocal failure_count, warned
        try:
            parsed = PublishDiagnosticsParams.model_validate(params)
            path = uri_to_path(parsed.uri) or parsed.uri
            entries = [diagnostic_entry_from_lsp(item) for item in parsed.diagnostics]
            if not entries:
                # An empty payload means "no problems now" for this file, so drop
                # any previously stored diagnostics for it. clear_for_file clears
                # across all servers and the sent-key LRU; that is safe here
                # because routing is one server per extension.
                registry.clear_for_file(parsed.uri)
            else:
                registry.register_pending(
                    server_name,
                    [DiagnosticFile(uri=parsed.uri, path=path, diagnostics=entries)],
                )
            failure_count = 0
        except Exception as exc:
            failure_count += 1
            if failure_count >= _HANDLER_FAILURE_WARN_THRESHOLD and not warned:
                logger.warning(
                    "LSP publishDiagnostics handler for {server} failed {count} times: {err}",
                    server=server_name,
                    count=failure_count,
                    err=exc,
                )
                warned = True

    instance.on_notification("textDocument/publishDiagnostics", handler)


def wire_publish_diagnostics_handlers(
    registry: DiagnosticRegistry,
    servers: Mapping[str, Any],
) -> None:
    """Register publishDiagnostics handlers for all running server instances."""
    for server_name, instance in servers.items():
        register_publish_diagnostics_handler(registry, server_name, instance)
