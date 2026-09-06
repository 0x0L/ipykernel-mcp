"""Public response contracts, shared by validation and MCP output schemas."""

from typing import Literal

from pydantic import BaseModel, Field

ExecutionStatus = Literal["running", "succeeded", "failed", "cancelled"]
WorkspaceState = Literal[
    "closed", "starting", "ready", "busy", "resetting", "unavailable"
]


class ErrorInfo(BaseModel):
    type: str
    message: str


class ExecutionSummary(BaseModel):
    execution_id: str
    status: ExecutionStatus


class PendingExecution(ExecutionSummary):
    unread_output_count: int = Field(ge=0, description="Unread text/image blocks.")


class ExecutionMetadata(ExecutionSummary):
    truncated: bool = Field(
        description="Unread output was dropped since the previous read; code may still be running."
    )
    error: ErrorInfo | None


class DrainOutput(BaseModel):
    executions: list[ExecutionMetadata]


class WorkspaceStatus(BaseModel):
    state: WorkspaceState
    python: str
    cwd: str
    active_execution_id: str | None
    executions: list[PendingExecution]
    unread_output_count: int = Field(
        ge=0, description="Total unread text/image blocks."
    )
    error: ErrorInfo | None


class InterruptResult(BaseModel):
    execution_id: str | None
    interrupt_sent: bool
