"""LSP tool input schemas."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Operation(StrEnum):
    GO_TO_DEFINITION = "goToDefinition"
    FIND_REFERENCES = "findReferences"
    HOVER = "hover"
    DOCUMENT_SYMBOL = "documentSymbol"
    WORKSPACE_SYMBOL = "workspaceSymbol"
    GO_TO_IMPLEMENTATION = "goToImplementation"
    PREPARE_CALL_HIERARCHY = "prepareCallHierarchy"
    INCOMING_CALLS = "incomingCalls"
    OUTGOING_CALLS = "outgoingCalls"


class Params(BaseModel):
    operation: Operation = Field(description="The LSP operation to perform")
    file_path: str = Field(description="The absolute or relative path to the file")
    line: int = Field(ge=1, description="The line number (1-based, as shown in editors)")
    character: int = Field(ge=1, description="The character offset (1-based, as shown in editors)")
