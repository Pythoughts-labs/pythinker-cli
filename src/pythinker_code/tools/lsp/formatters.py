"""Format LSP tool results as human-readable text."""

# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from pythinker_code.lsp.protocol import SymbolKind

MAX_RESULT_SIZE_CHARS = 100_000

_SYMBOL_KIND_LABELS: dict[int, str] = {
    SymbolKind.FILE: "File",
    SymbolKind.MODULE: "Module",
    SymbolKind.NAMESPACE: "Namespace",
    SymbolKind.PACKAGE: "Package",
    SymbolKind.CLASS: "Class",
    SymbolKind.METHOD: "Method",
    SymbolKind.PROPERTY: "Property",
    SymbolKind.FIELD: "Field",
    SymbolKind.CONSTRUCTOR: "Constructor",
    SymbolKind.ENUM: "Enum",
    SymbolKind.INTERFACE: "Interface",
    SymbolKind.FUNCTION: "Function",
    SymbolKind.VARIABLE: "Variable",
    SymbolKind.CONSTANT: "Constant",
    SymbolKind.STRING: "String",
    SymbolKind.NUMBER: "Number",
    SymbolKind.BOOLEAN: "Boolean",
    SymbolKind.ARRAY: "Array",
    SymbolKind.OBJECT: "Object",
    SymbolKind.KEY: "Key",
    SymbolKind.NULL: "Null",
    SymbolKind.ENUM_MEMBER: "EnumMember",
    SymbolKind.STRUCT: "Struct",
    SymbolKind.EVENT: "Event",
    SymbolKind.OPERATOR: "Operator",
    SymbolKind.TYPE_PARAMETER: "TypeParameter",
}


def _plural(count: int, word: str) -> str:
    return word if count == 1 else f"{word}s"


def _symbol_kind_label(kind: int | SymbolKind) -> str:
    value = int(kind)
    return _SYMBOL_KIND_LABELS.get(value, "Unknown")


def format_uri(uri: str | None, cwd: str | None = None) -> str:
    if not uri:
        return "<unknown location>"

    file_path = uri.removeprefix("file://")
    if len(file_path) >= 3 and file_path[0] == "/" and file_path[2] == ":":
        file_path = file_path[1:]

    with contextlib.suppress(Exception):
        file_path = unquote(file_path)

    file_path = file_path.replace("\\", "/")

    if cwd:
        try:
            relative = Path(file_path).relative_to(cwd).as_posix()
            if len(relative) < len(file_path) and not relative.startswith("../.."):
                return relative
        except ValueError:
            # Path is outside cwd; fall back to the absolute/normalized path below.
            relative = None

    return file_path


def _group_by_file(
    items: list[dict[str, Any]], *, cwd: str | None
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if "uri" in item:
            uri = item["uri"]
        else:
            location = item.get("location") or {}
            uri = location.get("uri")
        file_path = format_uri(uri, cwd)
        grouped.setdefault(file_path, []).append(item)
    return grouped


def _format_location(location: dict[str, Any], cwd: str | None) -> str:
    file_path = format_uri(location.get("uri"), cwd)
    start = (location.get("range") or {}).get("start") or {}
    line = int(start.get("line", 0)) + 1
    character = int(start.get("character", 0)) + 1
    return f"{file_path}:{line}:{character}"


def _is_location_link(item: dict[str, Any]) -> bool:
    return "targetUri" in item


def _to_location(item: dict[str, Any]) -> dict[str, Any]:
    if _is_location_link(item):
        return {
            "uri": item.get("targetUri"),
            "range": item.get("targetSelectionRange") or item.get("targetRange") or {},
        }
    return item


def format_go_to_definition_result(
    result: dict[str, Any] | list[dict[str, Any]] | None,
    cwd: str | None = None,
) -> str:
    if not result:
        return (
            "No definition found. This may occur if the cursor is not on a symbol, or if the "
            "definition is in an external library not indexed by the LSP server."
        )

    raw_results = result if isinstance(result, list) else [result]
    locations = [_to_location(item) for item in raw_results]
    valid = [loc for loc in locations if loc.get("uri")]

    if not valid:
        return (
            "No definition found. This may occur if the cursor is not on a symbol, or if the "
            "definition is in an external library not indexed by the LSP server."
        )
    if len(valid) == 1:
        return f"Defined in {_format_location(valid[0], cwd)}"

    lines = [f"Found {len(valid)} definitions:"]
    lines.extend(f"  {_format_location(loc, cwd)}" for loc in valid)
    return "\n".join(lines)


def format_find_references_result(
    result: list[dict[str, Any]] | None,
    cwd: str | None = None,
) -> str:
    if not result:
        return (
            "No references found. This may occur if the symbol has no usages, or if the LSP "
            "server has not fully indexed the workspace."
        )

    valid = [loc for loc in result if loc and loc.get("uri")]
    if not valid:
        return (
            "No references found. This may occur if the symbol has no usages, or if the LSP "
            "server has not fully indexed the workspace."
        )
    if len(valid) == 1:
        return f"Found 1 reference:\n  {_format_location(valid[0], cwd)}"

    by_file = _group_by_file(valid, cwd=cwd)
    lines = [f"Found {len(valid)} references across {len(by_file)} files:"]
    for file_path, locations in by_file.items():
        lines.append(f"\n{file_path}:")
        for loc in locations:
            start = (loc.get("range") or {}).get("start") or {}
            line = int(start.get("line", 0)) + 1
            character = int(start.get("character", 0)) + 1
            lines.append(f"  Line {line}:{character}")
    return "\n".join(lines)


def _extract_markup_text(contents: Any) -> str:
    if isinstance(contents, list):
        parts: list[str] = []
        for item in contents:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("value", "")))
        return "\n\n".join(parts)
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        return str(contents.get("value", ""))
    return str(contents)


def format_hover_result(result: dict[str, Any] | None, _cwd: str | None = None) -> str:
    if not result:
        return (
            "No hover information available. This may occur if the cursor is not on a symbol, "
            "or if the LSP server has not fully indexed the file."
        )

    content = _extract_markup_text(result.get("contents"))
    range_obj = result.get("range")
    if range_obj:
        start = range_obj.get("start") or {}
        line = int(start.get("line", 0)) + 1
        character = int(start.get("character", 0)) + 1
        return f"Hover info at {line}:{character}:\n\n{content}"
    return content


def _format_document_symbol_node(symbol: dict[str, Any], indent: int = 0) -> list[str]:
    lines: list[str] = []
    prefix = "  " * indent
    kind = _symbol_kind_label(symbol.get("kind", 0))
    line = f"{prefix}{symbol.get('name', '')} ({kind})"
    if symbol.get("detail"):
        line += f" {symbol['detail']}"
    symbol_line = int((symbol.get("range") or {}).get("start", {}).get("line", 0)) + 1
    line += f" - Line {symbol_line}"
    lines.append(line)
    for child in symbol.get("children") or []:
        lines.extend(_format_document_symbol_node(child, indent + 1))
    return lines


def format_document_symbol_result(
    result: list[dict[str, Any]] | None,
    cwd: str | None = None,
) -> str:
    if not result:
        return (
            "No symbols found in document. This may occur if the file is empty, not supported "
            "by the LSP server, or if the server has not fully indexed the file."
        )

    first = result[0]
    if first and "location" in first:
        return format_workspace_symbol_result(result, cwd)

    lines = ["Document symbols:"]
    for symbol in result:
        lines.extend(_format_document_symbol_node(symbol))
    return "\n".join(lines)


def format_workspace_symbol_result(
    result: list[dict[str, Any]] | None,
    cwd: str | None = None,
) -> str:
    if not result:
        return (
            "No symbols found in workspace. This may occur if the workspace is empty, or if the "
            "LSP server has not finished indexing the project."
        )

    valid = [sym for sym in result if sym and (sym.get("location") or {}).get("uri")]
    if not valid:
        return (
            "No symbols found in workspace. This may occur if the workspace is empty, or if the "
            "LSP server has not finished indexing the project."
        )

    lines = [f"Found {len(valid)} {_plural(len(valid), 'symbol')} in workspace:"]
    by_file = _group_by_file(valid, cwd=cwd)
    for file_path, symbols in by_file.items():
        lines.append(f"\n{file_path}:")
        for symbol in symbols:
            kind = _symbol_kind_label(symbol.get("kind", 0))
            location = symbol.get("location") or {}
            sym_line = int((location.get("range") or {}).get("start", {}).get("line", 0)) + 1
            symbol_line = f"  {symbol.get('name', '')} ({kind}) - Line {sym_line}"
            if symbol.get("containerName"):
                symbol_line += f" in {symbol['containerName']}"
            lines.append(symbol_line)
    return "\n".join(lines)


def _format_call_hierarchy_item(item: dict[str, Any], cwd: str | None) -> str:
    if not item.get("uri"):
        kind = _symbol_kind_label(item.get("kind", 0))
        return f"{item.get('name', '')} ({kind}) - <unknown location>"

    file_path = format_uri(item.get("uri"), cwd)
    line = int((item.get("range") or {}).get("start", {}).get("line", 0)) + 1
    kind = _symbol_kind_label(item.get("kind", 0))
    text = f"{item.get('name', '')} ({kind}) - {file_path}:{line}"
    if item.get("detail"):
        text += f" [{item['detail']}]"
    return text


def format_prepare_call_hierarchy_result(
    result: list[dict[str, Any]] | None,
    cwd: str | None = None,
) -> str:
    if not result:
        return "No call hierarchy item found at this position"

    if len(result) == 1:
        return f"Call hierarchy item: {_format_call_hierarchy_item(result[0], cwd)}"

    lines = [f"Found {len(result)} call hierarchy items:"]
    for item in result:
        lines.append(f"  {_format_call_hierarchy_item(item, cwd)}")
    return "\n".join(lines)


def format_incoming_calls_result(
    result: list[dict[str, Any]] | None,
    cwd: str | None = None,
) -> str:
    if not result:
        return "No incoming calls found (nothing calls this function)"

    lines = [f"Found {len(result)} incoming {_plural(len(result), 'call')}:"]
    by_file: dict[str, list[dict[str, Any]]] = {}
    for call in result:
        source = call.get("from")
        if not source:
            continue
        file_path = format_uri(source.get("uri"), cwd)
        by_file.setdefault(file_path, []).append(call)

    for file_path, calls in by_file.items():
        lines.append(f"\n{file_path}:")
        for call in calls:
            source = call.get("from") or {}
            kind = _symbol_kind_label(source.get("kind", 0))
            line = int((source.get("range") or {}).get("start", {}).get("line", 0)) + 1
            call_line = f"  {source.get('name', '')} ({kind}) - Line {line}"
            from_ranges = call.get("fromRanges") or []
            if from_ranges:
                call_sites = ", ".join(
                    f"{int(r.get('start', {}).get('line', 0)) + 1}:"
                    f"{int(r.get('start', {}).get('character', 0)) + 1}"
                    for r in from_ranges
                )
                call_line += f" [calls at: {call_sites}]"
            lines.append(call_line)
    return "\n".join(lines)


def format_outgoing_calls_result(
    result: list[dict[str, Any]] | None,
    cwd: str | None = None,
) -> str:
    if not result:
        return "No outgoing calls found (this function calls nothing)"

    lines = [f"Found {len(result)} outgoing {_plural(len(result), 'call')}:"]
    by_file: dict[str, list[dict[str, Any]]] = {}
    for call in result:
        target = call.get("to")
        if not target:
            continue
        file_path = format_uri(target.get("uri"), cwd)
        by_file.setdefault(file_path, []).append(call)

    for file_path, calls in by_file.items():
        lines.append(f"\n{file_path}:")
        for call in calls:
            target = call.get("to") or {}
            kind = _symbol_kind_label(target.get("kind", 0))
            line = int((target.get("range") or {}).get("start", {}).get("line", 0)) + 1
            call_line = f"  {target.get('name', '')} ({kind}) - Line {line}"
            from_ranges = call.get("fromRanges") or []
            if from_ranges:
                call_sites = ", ".join(
                    f"{int(r.get('start', {}).get('line', 0)) + 1}:"
                    f"{int(r.get('start', {}).get('character', 0)) + 1}"
                    for r in from_ranges
                )
                call_line += f" [called from: {call_sites}]"
            lines.append(call_line)
    return "\n".join(lines)


def count_symbols(symbols: list[dict[str, Any]]) -> int:
    total = len(symbols)
    for symbol in symbols:
        children = symbol.get("children") or []
        if children:
            total += count_symbols(children)
    return total


def count_unique_files_from_locations(locations: list[dict[str, Any]]) -> int:
    return len({loc.get("uri") for loc in locations if loc.get("uri")})


def format_result(
    operation: str,
    result: Any,
    cwd: str | None,
) -> tuple[str, int, int]:
    match operation:
        case "goToDefinition" | "goToImplementation":
            raw_results = result if isinstance(result, list) else ([result] if result else [])
            locations = [_to_location(item) for item in raw_results]
            valid = [loc for loc in locations if loc.get("uri")]
            formatted = format_go_to_definition_result(result, cwd)
            return formatted, len(valid), count_unique_files_from_locations(valid)
        case "findReferences":
            locations = result or []
            valid = [loc for loc in locations if loc and loc.get("uri")]
            formatted = format_find_references_result(result, cwd)
            return formatted, len(valid), count_unique_files_from_locations(valid)
        case "hover":
            formatted = format_hover_result(result, cwd)
            count = 1 if result else 0
            return formatted, count, count
        case "documentSymbol":
            symbols = result or []
            is_document_symbol = bool(symbols and "range" in symbols[0])
            formatted = format_document_symbol_result(result, cwd)
            if is_document_symbol:
                # Hierarchical DocumentSymbol[] always describes the one open file.
                count = count_symbols(symbols)
                file_count = 1 if symbols else 0
            else:
                # SymbolInformation[] fallback carries per-symbol locations that may
                # span files; count unique URIs like workspaceSymbol does.
                count = len(symbols)
                locations = [sym.get("location") for sym in symbols]
                file_count = count_unique_files_from_locations([loc for loc in locations if loc])
            return formatted, count, file_count
        case "workspaceSymbol":
            symbols = result or []
            valid = [sym for sym in symbols if sym and (sym.get("location") or {}).get("uri")]
            locations = [sym.get("location") for sym in valid]
            formatted = format_workspace_symbol_result(result, cwd)
            return formatted, len(valid), count_unique_files_from_locations(locations)
        case "prepareCallHierarchy":
            items = result or []
            formatted = format_prepare_call_hierarchy_result(result, cwd)
            uris = [item.get("uri") for item in items if item.get("uri")]
            file_count = len(set(uris)) if uris else 0
            return formatted, len(items), file_count
        case "incomingCalls":
            calls = result or []
            formatted = format_incoming_calls_result(result, cwd)
            uris = [(call.get("from") or {}).get("uri") for call in calls]
            uris = [uri for uri in uris if uri]
            file_count = len(set(uris)) if uris else 0
            return formatted, len(calls), file_count
        case "outgoingCalls":
            calls = result or []
            formatted = format_outgoing_calls_result(result, cwd)
            uris = [(call.get("to") or {}).get("uri") for call in calls]
            uris = [uri for uri in uris if uri]
            file_count = len(set(uris)) if uris else 0
            return formatted, len(calls), file_count
        case _:
            return str(result), 0, 0
