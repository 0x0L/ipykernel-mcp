"""Public response contracts, shared by validation and MCP output schemas."""

from typing import Literal

from pydantic import BaseModel, Field

ExecutionStatus = Literal["running", "succeeded", "failed", "cancelled"]
WorkspaceState = Literal[
    "closed", "starting", "ready", "busy", "resetting", "unavailable"
]


class ErrorInfo(BaseModel):
    type: str = Field(
        description="Exception or lifecycle error type, e.g. ValueError or KernelDied."
    )
    message: str = Field(
        description="Diagnostic message explaining the failure or cancellation."
    )


class ExecutionSummary(BaseModel):
    execution_id: str = Field(
        description="Opaque execution ID. Use for read_output while pending; removed after its final outcome is consumed."
    )
    status: ExecutionStatus = Field(
        description="running: read again; succeeded: finished normally; failed: code or kernel error; cancelled: interrupted, reset, or closed. Final outcomes need no further read."
    )


class PendingExecution(ExecutionSummary):
    unread_output_count: int = Field(
        ge=0,
        description="Buffered text/image blocks before stream merging; excludes metadata. Zero can still mean a pending final outcome.",
    )


class ExecutionMetadata(ExecutionSummary):
    truncated: bool = Field(
        description="Output was dropped due to buffer limits since the previous read. Code may still be running; reading replenishes capacity but cannot recover dropped output."
    )
    error: ErrorInfo | None = Field(
        description="Execution failure/cancellation details, or null. A code error can leave partial changes; it does not by itself require reset."
    )


class DrainOutput(BaseModel):
    executions: list[ExecutionMetadata] = Field(
        description="Consumed execution outcomes in content-group order, including silent completions. Empty means no pending output/outcomes to return; running code may still exist."
    )


class WorkspaceStatus(BaseModel):
    state: WorkspaceState = Field(
        description="ready: may execute; busy: read/interrupt active work; starting/resetting: transition; unavailable: inspect error and reset; closed: kernel shut down."
    )
    kernel_name: str = Field(
        description="Configured installed Jupyter kernel name, fixed for this server's lifetime."
    )
    cwd: str = Field(
        description="Configured initial working directory, restored by reset. Code may have changed the kernel's current directory."
    )
    active_execution_id: str | None = Field(
        description="Currently running execution ID, or null. Null does not exclude unread completed outcomes."
    )
    executions: list[PendingExecution] = Field(
        description="Pending executions: running work and unconsumed completed outcomes within retention limits. Not history or a list of kernel variables."
    )
    unread_output_count: int = Field(
        ge=0,
        description="Sum of unread text/image blocks across pending executions. Excludes outcome metadata, so zero does not mean nothing to drain.",
    )
    error: ErrorInfo | None = Field(
        description="Kernel lifecycle/connection failure, or null; distinct from individual code errors. Reset can recover an unavailable kernel but clears variables."
    )


class InterruptResult(BaseModel):
    execution_id: str | None = Field(
        description="Execution targeted by the interrupt, or null if no code was active. Read this ID for its eventual outcome."
    )
    interrupt_sent: bool = Field(
        description="True if an interrupt signal was sent, not confirmation that code stopped. False means no active execution needed interruption."
    )
