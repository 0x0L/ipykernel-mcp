"""Six MCP tools for one configured Jupyter kernel."""

from __future__ import annotations

import argparse
from collections.abc import Awaitable
from importlib.metadata import version
from typing import Annotated

from anyio import CancelScope
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.lifespan import lifespan
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

from .interpreter import InterpreterError, KernelConfig
from .kernel import Kernel, KernelError
from .schemas import DrainOutput, ExecutionMetadata, InterruptResult, WorkspaceStatus

WaitSeconds = Annotated[
    float,
    Field(
        ge=0,
        le=60,
        allow_inf_nan=False,
        description="Seconds to wait for completion (0–60); 0 returns immediately. Expiration leaves code running.",
    ),
]
ExecutionId = Annotated[
    str,
    Field(
        min_length=1,
        description="Opaque ID from execute or status.executions; valid until its final outcome is consumed or expires.",
    ),
]
KernelCode = Annotated[
    str,
    Field(
        min_length=1,
        description="Non-empty source for the configured persistent Jupyter kernel, without Markdown fences. Variables and imports survive calls.",
    ),
]

INSTRUCTIONS = """A persistent Jupyter kernel for computation, data analysis, and images.
Call execute to run code. Variables survive calls. If status is running, continue
with read_output(execution_id); do not resubmit the code. Returned output is consumed,
including execute output. Completed IDs are removed on return; reads cannot replay.
Use status to inspect pending IDs/counts, drain_output to consume all pending results,
interrupt to request a stop, and reset for a fresh kernel. One execution at a time.

This server discovers and launches its kernel through the configured Jupyter
executable. That installation resolves the configured kernelspec, which determines
the language and available libraries. The executable, kernel name, and initial
directory are fixed at startup; no start or environment-selection call is needed.
Use this server for incremental calculations, loading data once and refining an
analysis, or generating images for inspection. Choose the server for the intended
language. Each server has separate variables and execution IDs; send follow-up
code and output reads to the same server. Files can be shared through disk.
Use the configured environment's libraries and files. Reuse data and functions
across calls. Return summaries or samples of large datasets; save large artifacts
to files. Reads free server output buffers, not variables in the kernel.

wait_seconds limits waiting, never code duration. execute defaults to 10 seconds;
read_output defaults to 0; both accept 0–60. Use a positive wait for unfinished work.
Use one output consumer at a time: read_output for one ID or drain_output for all.
A cancelled tool wait leaves code running. After a lost response, inspect status;
do not blindly rerun code, which may already have changed variables or files.

Results contain content (text/images) and structured execution metadata: status is
running, succeeded, failed, or cancelled. Code exceptions return failed outcomes
with error details and may leave partial changes; they do not require a reset.
Invalid requests, busy kernels, and consumed/expired IDs are MCP tool errors.
If the kernel is unavailable, reset recovers it but loses in-memory state.
interrupt requests a stop; only a later result confirms the outcome.

Output supports text and PNG/JPEG images. Use the kernel's printing and display
facilities. In Python, use `from IPython.display import Image, display` followed by
`display(Image(filename="/absolute/path/image.png"))` to return a local image.
Oversized images fall back to available JPEG or text representations in the same
display bundle; no conversion is performed. Omitted images report truncation.
HTML/widgets are not rendered. Display clears are ignored; updates append output.
Late output after execution completion is ignored, so await work inside the cell.
Unread buffers per execution are limited to 64 KiB text, 4 MiB payload, and 1,000
blocks. truncated reports dropped output since the previous read; reads replenish
capacity. Unread completed results expire after 10 minutes; at most 16 executions
are tracked, evicting oldest completed results first. Consumed results are removed
immediately. drain_output returns whole results within an 8 MiB JSON budget;
repeat until executions is empty. Excess results remain unread. Save needed results
in your response or files; there is no replay.
"""


async def _call_kernel[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except KernelError as exc:
        raise ToolError(str(exc)) from exc


def create_server(kernel: Kernel) -> FastMCP:
    """The server owns the configured kernel's entire lifetime."""

    @lifespan
    async def workspace_lifespan(server):
        try:
            await kernel.open()
            assert kernel.manager is not None
            spec = kernel.manager.kernel_spec
            assert spec is not None
            server.instructions = INSTRUCTIONS + (
                f"\nConfigured Jupyter executable: {str(kernel.config.jupyter)!r}. "
                f"Configured kernel: {kernel.config.kernel_name!r}. "
                f"Language: {spec.language!r}. "
                f"Initial working directory: {str(kernel.config.cwd)!r}.\n"
                "Write code in this language; Python examples apply only to Python kernels."
            )
            yield {}
        finally:
            with CancelScope(shield=True):
                await kernel.close()

    server = FastMCP(
        "ipykernel-mcp",
        version=version("ipykernel-mcp"),
        instructions=INSTRUCTIONS,
        lifespan=workspace_lifespan,
        strict_input_validation=True,
        mask_error_details=True,
    )

    @server.tool(
        title="Execute in Jupyter kernel",
        output_schema=ExecutionMetadata.model_json_schema(),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def execute(code: KernelCode, wait_seconds: WaitSeconds = 10) -> ToolResult:
        """Run code in the persistent Jupyter kernel for calculations, analysis, or images.

        Use source and libraries supported by the configured kernel. Variables,
        imports, and functions survive calls. Jupyter display data containing PNG
        or JPEG images is returned as image content.

        Returns text/images plus execution_id, status, truncated, and error.
        Returned output is consumed. If running, call read_output with that ID
        and a positive wait_seconds; do not resubmit code. A final outcome removes
        the ID, so no follow-up read is needed. Code errors may leave partial state.
        If busy, read or interrupt the active execution before submitting more code.
        Interactive stdin is disabled. wait_seconds defaults to 10 and never stops code.

        Python-only examples (adapt syntax for other kernel languages):
        Example: {"code": "values = [10, 20, 30]; sum(values)"}
        Follow-up: {"code": "sum(values) / len(values)"}
        """
        return await _call_kernel(kernel.execute(code, wait_seconds))

    @server.tool(
        title="Read execution output",
        output_schema=ExecutionMetadata.model_json_schema(),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def read_output(
        execution_id: ExecutionId,
        wait_seconds: WaitSeconds = 0,
    ) -> ToolResult:
        """Retrieve and consume one pending execution's unread output and current outcome.

        Use the ID returned by execute or listed in status.executions. Defaults
        to an immediate read; use wait_seconds=10 to wait for unfinished work.
        Returns text/images plus execution_id, status, truncated, and error.
        If running, keep reading the same ID. A final outcome removes the record,
        including silent completions; do not read that ID again. No code is run.

        Output is returned once, without a cursor or replay. Do not race this call
        with another read or drain_output: a waiter whose final outcome was consumed
        elsewhere gets a tool error. Unknown/consumed/expired IDs also error; inspect
        status for remaining work. Reads preserve kernel variables. To retrieve
        pending results in batches, use drain_output.
        """
        return await _call_kernel(kernel.read_output(execution_id, wait_seconds))

    @server.tool(
        title="Drain pending output batch",
        output_schema=DrainOutput.model_json_schema(),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def drain_output() -> ToolResult:
        """Retrieve and consume a bounded batch of pending output and outcomes without waiting.

        Repeat until executions is empty to collect pending results; use read_output
        to target one ID. Each response has an 8 MiB JSON budget; whole execution
        results that do not fit remain unread for the next call.
        Each content group starts with an [execution] header identifying its ID
        and outcome, followed by text/images. Structured executions lists the same
        metadata in group order. Silent completed outcomes and truncation notices
        are included even when status.unread_output_count is zero.

        Returned output is released and completed records are removed. Running
        records remain; output arriving afterward stays unread. Running records
        with no output or truncation notice are omitted. An empty executions list
        means nothing was pending to return, not necessarily that the kernel is idle.
        Check status for active work. Do not overlap with other output consumers.
        Does not run code, stop execution, or clear kernel variables. No replay.
        """
        return await _call_kernel(kernel.drain_output())

    @server.tool(
        title="Interrupt Jupyter kernel",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def interrupt() -> InterruptResult:
        """Request that running code stop while preserving the existing Jupyter kernel.

        Use to stop unwanted work without resetting variables. Returns the targeted
        execution_id and interrupt_sent. A true flag only confirms Jupyter delivered
        an interrupt request; code can catch or defer it. Read that ID with
        read_output to learn the eventual outcome. If no execution is active,
        returns null and false.
        Partial changes made by code remain. If the kernel is unavailable, use
        reset to recover; reset clears in-memory state.
        """
        return InterruptResult.model_validate(await _call_kernel(kernel.interrupt()))

    @server.tool(
        title="Reset Jupyter kernel",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def reset() -> WorkspaceStatus:
        """Replace the Jupyter kernel with a fresh one using the same Jupyter executable, kernelspec, and initial cwd.

        Use for an intentional fresh start or to recover an unavailable kernel.
        Cancels active execution and clears all variables, imports, and functions.
        Files already written remain. For ordinary code errors, fix the code and
        execute again; for a stop that preserves state, use interrupt instead.

        Returns kernel status with pending IDs/counts. Unread output and cancelled
        outcomes from the old kernel remain available until consumed or expired;
        reset does not drain them. Use drain_output to retrieve them. This cannot
        change the configured Jupyter executable, kernelspec, or initial directory.
        """
        return WorkspaceStatus.model_validate(await _call_kernel(kernel.reset()))

    @server.tool(
        title="Inspect Jupyter kernel",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    async def status() -> WorkspaceStatus:
        """Inspect kernel availability, configured paths, pending executions, and unread counts.

        Does not execute code or consume output. ready means code can be submitted;
        busy means read or interrupt active_execution_id first. starting/resetting
        are transitional; unavailable means inspect error and use reset to recover.
        closed means the server's kernel is shut down.

        executions includes active work and completed outcomes not yet consumed or
        expired; it is not execution history or a variable inventory. Counts measure
        buffered text/image blocks after adjacent stream merging, excluding metadata and
        internal Jupyter messages. Zero unread_output_count does not imply completion
        or no pending outcomes. Use read_output for one ID or drain_output for all.
        jupyter, kernel_name, and cwd are fixed startup settings; code may have
        changed its current directory since startup. This tool does not inspect
        current kernel variables.
        """
        return WorkspaceStatus.model_validate(kernel.status())

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="A persistent Jupyter kernel for MCP.")
    parser.add_argument(
        "--jupyter",
        required=True,
        help="Path to the Jupyter executable used for discovery and launch",
    )
    parser.add_argument("--kernel", required=True, help="Installed Jupyter kernel name")
    parser.add_argument(
        "--cwd", help="Initial working directory (default: server working directory)"
    )
    args = parser.parse_args()
    try:
        config = KernelConfig.from_paths(args.jupyter, args.kernel, args.cwd)
    except InterpreterError as exc:
        parser.error(str(exc))
    try:
        create_server(Kernel(config)).run(transport="stdio", show_banner=False)
    except KernelError as exc:
        parser.exit(1, f"ipykernel-mcp: {exc}\n")


if __name__ == "__main__":
    main()
