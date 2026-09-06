"""Public response contracts, shared by validation and MCP output schemas."""

from typing import Literal

from pydantic import BaseModel, Field

ExecutionStatus = Literal["running", "succeeded", "failed", "cancelled"]
KernelState = Literal[
    "closed", "starting", "ready", "busy", "restarting", "unavailable"
]


class ErrorInfo(BaseModel):
    type: str = Field(
        description="Exception or lifecycle error type, e.g. ValueError or KernelDied. Kernel exceptions use Jupyter's ename; server lifecycle errors use server-defined types."
    )
    message: str = Field(
        description="Diagnostic message explaining the failure or cancellation. Kernel exceptions use Jupyter's evalue; server lifecycle errors use server-defined messages."
    )


class ExecutionSummary(BaseModel):
    execution_id: str = Field(
        description="Jupyter execute_request header msg_id, used as an opaque execution ID. Use for read_output while pending; removed after its final outcome is consumed."
    )
    status: ExecutionStatus = Field(
        description="Server execution lifecycle outcome, distinct from Jupyter execute_reply status (ok/error/aborted). running: read again; succeeded: finished normally; failed: code or kernel error; cancelled: interrupted, restarted, or closed. Final outcomes need no further read."
    )


class PendingExecution(ExecutionSummary):
    unread_output_count: int = Field(
        ge=0,
        description="Buffered text/image blocks after adjacent stream merging; excludes metadata. Zero can still mean a pending final outcome.",
    )


class ExecutionMetadata(ExecutionSummary):
    truncated: bool = Field(
        description="Output was dropped due to buffer limits since the previous read. Code may still be running; reading replenishes capacity but cannot recover dropped output."
    )
    error: ErrorInfo | None = Field(
        description="Execution failure/cancellation details, or null. A code error can leave partial changes; it does not by itself require restart."
    )


class DrainOutput(BaseModel):
    executions: list[ExecutionMetadata] = Field(
        description="Consumed execution outcomes in content-group order, including silent completions. Each drain has an 8 MiB JSON budget; excess whole results remain unread. Repeat until empty to drain available results. Empty means no pending output/outcomes to return; running code may still exist."
    )


class KernelStatus(BaseModel):
    state: KernelState = Field(
        description="Server lifecycle and availability state, not Jupyter's published execution_state. ready: may submit code through this server; busy: read/interrupt this server's active work; starting/restarting: transition; unavailable: inspect error and restart; closed: kernel shut down. External clients' executions are not tracked."
    )
    jupyter: str = Field(
        description="Configured Jupyter executable path used for discovery and launch, fixed for this server lifetime."
    )
    kernel_name: str = Field(
        description="Configured installed Jupyter kernel name, fixed for this server's lifetime."
    )
    cwd: str = Field(
        description="Configured initial working directory, restored by restart. Code may have changed the kernel's current directory."
    )
    connection_file: str | None = Field(
        description="Absolute Jupyter connection JSON path for attaching another local client to this kernel. Available when ready/busy; otherwise null. Changes on restart and is removed on shutdown. External executions are not tracked by this server."
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
        description="Kernel lifecycle/connection failure, or null; distinct from individual code errors. Restart can recover an unavailable kernel but clears variables."
    )


class InterruptResult(BaseModel):
    execution_id: str | None = Field(
        description="Execution targeted by the interrupt, or null if no code was active. Read this ID for its eventual outcome."
    )
    interrupt_sent: bool = Field(
        description="True if Jupyter delivered an interrupt request, not confirmation that code stopped. False means no active execution needed interruption."
    )
