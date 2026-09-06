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
        description="Seconds to wait for completion; expiration does not stop Python.",
    ),
]
ExecutionId = Annotated[
    str,
    Field(
        min_length=1,
        description="Execution ID returned by execute or listed by status.",
    ),
]
PythonCode = Annotated[
    str,
    Field(
        min_length=1, description="Python code to run in the persistent Jupyter kernel."
    ),
]

INSTRUCTIONS = """A persistent Jupyter kernel, already configured and started.
Use execute for code, read_output for one execution, drain_output for all pending
output, and status for unread counts and pending IDs. Returned output is consumed,
including output returned by execute. Completed records are removed on delivery.
Reads cannot be replayed. One execution runs at a time. wait_seconds limits waiting,
not execution duration. If running, call read_output with execution_id; no cursor.
Each execution response has execution_id, status, truncated, and error. Status is
running, succeeded, failed, or cancelled. drain_output groups output by execution
and includes completed outcomes even when there is no output. New output arriving
after a drain remains unread. Counts measure text/image blocks, not Jupyter messages.
Unread completed results expire after 10 minutes, at most 16 executions are tracked.
Display clears are ignored; display updates append output to their own execution.
Exceptions are execution outcomes; invalid requests are MCP tool errors. input() fails.
Cancelling a wait leaves code running. interrupt asks code to stop; reset starts a
fresh workspace and recovers failures. Variables survive reads, not resets or shutdown.
Code is never silently rerun."""


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
        title="Execute Python",
        output_schema=ExecutionMetadata.model_json_schema(),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def execute(code: PythonCode, wait_seconds: WaitSeconds = 10) -> ToolResult:
        """Run Python with persistent variables; wait up to 10 seconds by default.

        wait_seconds (0–60) limits waiting, not code duration. If status is running,
        pass execution_id to read_output. Returned output is consumed. One execution at a time.
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
        """Return and consume unread output for one execution; immediate by default.

        Completed records are removed when returned. Reads cannot be replayed.
        If another reader consumes the final outcome while this call waits, this
        call returns a tool error. Use one consumer per execution.
        """
        return await _call_kernel(kernel.read_output(execution_id, wait_seconds))

    @server.tool(
        title="Drain all pending output",
        output_schema=DrainOutput.model_json_schema(),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def drain_output() -> ToolResult:
        """Immediately return and consume all pending output, grouped by execution.

        Includes silent completed outcomes and truncation notices. Each content
        group starts with execution metadata; structured executions lists outcomes.
        Removes completed records, keeps running ones, and leaves future output
        for the next read. An empty drain returns executions=[]. No code is run.
        """
        return await _call_kernel(kernel.drain_output())

    @server.tool(
        title="Interrupt Python",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    async def interrupt() -> InterruptResult:
        """Ask running code to stop, preserving variables. Code may defer or catch it.

        Read the active execution to see its eventual outcome.
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
        """Start a fresh Jupyter kernel with the same interpreter and initial cwd.

        Clears variables and cancels active execution. Also recovers from crashes
        and output connection failures. Unread results remain available until consumed or expired.
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
        """Inspect workspace state, fixed configuration, and pending execution IDs, and unread output counts."""
        return WorkspaceStatus.model_validate(kernel.status())

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="A persistent Jupyter kernel for MCP.")
    parser.add_argument(
        "--python", required=True, help="Python executable with ipykernel installed"
    )
    parser.add_argument(
        "--cwd", help="Initial working directory (default: server working directory)"
    )
    args = parser.parse_args()
    try:
        config = KernelConfig.from_paths(args.python, args.cwd)
    except InterpreterError as exc:
        parser.error(str(exc))
    try:
        create_server(Kernel(config)).run(transport="stdio", show_banner=False)
    except KernelError as exc:
        parser.exit(1, f"ipykernel-mcp: {exc}\n")


if __name__ == "__main__":
    main()
