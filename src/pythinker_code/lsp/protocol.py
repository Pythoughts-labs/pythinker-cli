"""Hand-defined LSP types used by the Pythinker CLI LSP subsystem."""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _LspModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class DiagnosticSeverity(IntEnum):
    ERROR = 1
    WARNING = 2
    INFORMATION = 3
    HINT = 4


class SymbolKind(IntEnum):
    FILE = 1
    MODULE = 2
    NAMESPACE = 3
    PACKAGE = 4
    CLASS = 5
    METHOD = 6
    PROPERTY = 7
    FIELD = 8
    CONSTRUCTOR = 9
    ENUM = 10
    INTERFACE = 11
    FUNCTION = 12
    VARIABLE = 13
    CONSTANT = 14
    STRING = 15
    NUMBER = 16
    BOOLEAN = 17
    ARRAY = 18
    OBJECT = 19
    KEY = 20
    NULL = 21
    ENUM_MEMBER = 22
    STRUCT = 23
    EVENT = 24
    OPERATOR = 25
    TYPE_PARAMETER = 26


class Position(_LspModel):
    line: int
    character: int


class Range(_LspModel):
    start: Position
    end: Position


class Location(_LspModel):
    uri: str
    range: Range


class LocationLink(_LspModel):
    targetUri: str
    targetRange: Range
    targetSelectionRange: Range | None = None
    originSelectionRange: Range | None = None


class Diagnostic(_LspModel):
    range: Range
    message: str
    severity: DiagnosticSeverity | None = None
    code: str | int | None = None
    source: str | None = None


class DocumentSymbol(_LspModel):
    name: str
    kind: SymbolKind
    range: Range
    selectionRange: Range
    children: list[DocumentSymbol] | None = None
    detail: str | None = None


class SymbolInformation(_LspModel):
    name: str
    kind: SymbolKind
    location: Location
    containerName: str | None = None


class MarkupContent(_LspModel):
    kind: str
    value: str


class Hover(_LspModel):
    contents: MarkupContent | str | list[str | MarkupContent]
    range: Range | None = None


class CallHierarchyItem(_LspModel):
    name: str
    kind: SymbolKind
    uri: str
    range: Range
    selectionRange: Range
    detail: str | None = None
    data: Any | None = None


class CallHierarchyIncomingCall(_LspModel):
    from_: CallHierarchyItem = Field(alias="from")
    fromRanges: list[Range]


class CallHierarchyOutgoingCall(_LspModel):
    to: CallHierarchyItem
    fromRanges: list[Range]


class InitializeParams(_LspModel):
    processId: int | None = None
    rootUri: str | None = None
    rootPath: str | None = None
    workspaceFolders: list[dict[str, Any]] | None = None
    capabilities: dict[str, Any] | None = None
    initializationOptions: Any | None = None
    trace: str | None = None
    locale: str | None = None


class ServerCapabilities(_LspModel):
    hoverProvider: bool | dict[str, Any] | None = None
    definitionProvider: bool | dict[str, Any] | None = None
    referencesProvider: bool | dict[str, Any] | None = None
    documentSymbolProvider: bool | dict[str, Any] | None = None
    workspaceSymbolProvider: bool | dict[str, Any] | None = None
    implementationProvider: bool | dict[str, Any] | None = None
    callHierarchyProvider: bool | dict[str, Any] | None = None


class InitializeResult(_LspModel):
    capabilities: ServerCapabilities
    serverInfo: dict[str, Any] | None = None


class PublishDiagnosticsParams(_LspModel):
    uri: str
    diagnostics: list[Diagnostic]
    version: int | None = None
