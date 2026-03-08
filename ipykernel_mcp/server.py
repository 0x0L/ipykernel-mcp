from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from fastmcp.tools.tool import ToolResult
from jupyter_client import AsyncKernelClient, AsyncKernelManager
from jupyter_client.kernelspec import KernelSpec
from mcp.types import ImageContent, TextContent


@dataclass
class ExecutionRecord:
    stdout: str = ""
    stderr: str = ""
    result: str | None = None
    error: dict | None = None
    image_blocks: list[ImageContent] = field(default_factory=list)
    pending_clear: bool = False
    done: bool = False
    done_event: asyncio.Event = field(default_factory=asyncio.Event)


@lifespan
async def kernel_lifespan(server):
    try:
        yield {}
    finally:
        await _cleanup()


mcp = FastMCP(
    "ipykernel-mcp",
    instructions=(
        "This server provides a persistent Python (IPython) kernel. Variables, "
        "imports, and definitions survive across `execute` calls, so you can build "
        "up state incrementally — just like a notebook.\n"
        "\n"
        "Call `kernel_start` once with a project directory (must have a .venv with "
        "ipykernel installed), then call `kernel_execute` as many times as needed. Each "
        "call returns stdout, stderr, result (the expression value), and error "
        "(traceback details) as structured output.\n"
        "\n"
        "If `kernel_execute` times out, it returns partial output plus a `[pending]` "
        "block with a `msg_id`. Use `kernel_get_output(msg_id)` to retrieve the "
        "remaining output once execution completes.\n"
        "\n"
        "Use `kernel_restart` to clear all state. Use `kernel_interrupt` to cancel a "
        "long-running execution without losing state. Use `kernel_stop` to shut "
        "down. Only one kernel runs at a time."
    ),
    lifespan=kernel_lifespan,
)

_kernel_manager: AsyncKernelManager | None = None
_kernel_client: AsyncKernelClient | None = None
_project_dir: str | None = None
_executions: dict[str, ExecutionRecord] = {}
_reader_task: asyncio.Task | None = None


async def _iopub_reader() -> None:
    """Background task that routes iopub messages to ExecutionRecords."""
    assert _kernel_client is not None
    while True:
        try:
            msg = await _kernel_client.get_iopub_msg(timeout=1.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            # queue.Empty on timeout, or other transient errors — just loop
            continue

        msg_id = msg["parent_header"].get("msg_id")
        if msg_id is None or msg_id not in _executions:
            continue

        record = _executions[msg_id]
        msg_type = msg["msg_type"]
        content = msg["content"]

        if msg_type == "clear_output":
            if content.get("wait", False):
                record.pending_clear = True
            else:
                record.stdout = ""
                record.stderr = ""
                record.image_blocks.clear()
            continue

        # Apply deferred clear before any new output
        if record.pending_clear and msg_type in (
            "stream",
            "display_data",
            "execute_result",
        ):
            record.stdout = ""
            record.stderr = ""
            record.image_blocks.clear()
            record.pending_clear = False

        if msg_type == "stream":
            if content["name"] == "stdout":
                record.stdout += content["text"]
            elif content["name"] == "stderr":
                record.stderr += content["text"]
        elif msg_type in ("execute_result", "display_data"):
            data = content["data"]
            record.image_blocks.extend(_extract_images(data))
            if msg_type == "execute_result":
                record.result = data.get("text/plain")
        elif msg_type == "error":
            traceback = [_ANSI_ESCAPE.sub("", line) for line in content["traceback"]]
            record.error = {
                "ename": content["ename"],
                "evalue": content["evalue"],
                "traceback": traceback,
            }
        elif msg_type == "status" and content["execution_state"] == "idle":
            record.done = True
            record.done_event.set()


async def _cleanup() -> None:
    global _kernel_manager, _kernel_client, _project_dir, _reader_task
    if _reader_task is not None:
        _reader_task.cancel()
        try:
            await _reader_task
        except asyncio.CancelledError:
            pass
        _reader_task = None
    _executions.clear()
    if _kernel_client is not None:
        _kernel_client.stop_channels()
        _kernel_client = None
    if _kernel_manager is not None:
        await _kernel_manager.shutdown_kernel(now=True)
        _kernel_manager = None
    _project_dir = None


@mcp.tool
async def kernel_start(project_dir: str) -> str:
    """Start an IPython kernel using the .venv from the given project directory.

    The directory must contain a .venv with ipykernel installed. Only one kernel
    can run at a time — call kernel_stop first if one is already running.
    """
    global _kernel_manager, _kernel_client, _project_dir, _reader_task

    if _kernel_manager is not None:
        return "Error: a kernel is already running. Shut it down first."

    project_path = Path(project_dir).resolve()
    if not project_path.is_dir():
        return f"Error: {project_path} is not a directory."

    venv_python = project_path / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return f"Error: no venv found at {project_path / '.venv'}."

    spec = KernelSpec(
        argv=[str(venv_python), "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        display_name="MCP IPython Kernel",
        language="python",
    )

    km = AsyncKernelManager()
    km._kernel_spec = spec

    try:
        await km.start_kernel(cwd=str(project_path))
    except Exception as exc:
        return f"Error: failed to start kernel: {exc}"

    kc = km.client()
    kc.start_channels()

    try:
        await kc.wait_for_ready(timeout=60)
    except RuntimeError as exc:
        kc.stop_channels()
        await km.shutdown_kernel(now=True)
        return f"Error: kernel did not become ready: {exc}"

    _kernel_manager = km
    _kernel_client = kc
    _project_dir = str(project_path)
    _reader_task = asyncio.create_task(_iopub_reader())

    return (
        f"Kernel started. Python: {venv_python}, cwd: {project_path}, connection_file: {km.connection_file}\n"
        f"Monitor output: npx github:0x0L/jupyter_watch {km.connection_file}"
    )


@mcp.tool
async def kernel_status() -> dict:
    """Return the current kernel status.

    Returns a dict with: running, alive, project_dir, connection_file, python,
    pending_executions, transport, ip, shell_port, iopub_port.
    Returns {"running": False} if no kernel is running.
    """
    if _kernel_manager is None:
        return {"running": False}

    info: dict = {
        "running": True,
        "alive": await _kernel_manager.is_alive(),
        "project_dir": _project_dir,
        "connection_file": _kernel_manager.connection_file,
        "python": _kernel_manager._kernel_spec.argv[0]
        if _kernel_manager._kernel_spec
        else None,
        "pending_executions": sum(1 for r in _executions.values() if not r.done),
    }

    ci = _kernel_manager.get_connection_info()
    info["transport"] = ci.get("transport")
    info["ip"] = ci.get("ip")
    info["shell_port"] = ci.get("shell_port")
    info["iopub_port"] = ci.get("iopub_port")

    return info


@mcp.tool
async def kernel_stop() -> str:
    """Stop the running kernel and clean up resources."""
    if _kernel_manager is None:
        return "Error: no kernel is running."
    await _cleanup()
    return "Kernel stopped."


@mcp.tool
async def kernel_restart() -> str:
    """Restart the kernel, clearing all variables, imports, and state.

    Pending executions are discarded. The kernel process is replaced but the
    connection is preserved — no need to call kernel_start again.
    """
    global _reader_task
    if _kernel_manager is None or _kernel_client is None:
        return "Error: no kernel is running."

    # Cancel reader before restart to avoid message contention
    if _reader_task is not None:
        _reader_task.cancel()
        try:
            await _reader_task
        except asyncio.CancelledError:
            pass
        _reader_task = None
    _executions.clear()

    try:
        await _kernel_manager.restart_kernel(now=True)
        await _kernel_client.wait_for_ready(timeout=60)
    except Exception as exc:
        await _cleanup()
        return f"Error: failed to restart kernel: {exc}"

    _reader_task = asyncio.create_task(_iopub_reader())
    return "Kernel restarted."


@mcp.tool
async def kernel_interrupt() -> str:
    """Interrupt the running kernel (send SIGINT).

    Use this to cancel a long-running execution without losing kernel state.
    """
    if _kernel_manager is None:
        return "Error: no kernel is running."
    await _kernel_manager.interrupt_kernel()
    return "Interrupt signal sent."


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _extract_images(mime_bundle: dict) -> list[ImageContent]:
    """Extract ImageContent blocks from a kernel MIME bundle."""
    images: list[ImageContent] = []
    for mime_type in ("image/png", "image/jpeg"):
        if mime_type in mime_bundle:
            images.append(
                ImageContent(
                    type="image", data=mime_bundle[mime_type], mimeType=mime_type
                )
            )
    return images


def _build_tool_result(
    record: ExecutionRecord, *, msg_id: str, complete: bool
) -> ToolResult:
    """Build MCP content blocks from an ExecutionRecord."""
    blocks: list[TextContent | ImageContent] = []
    if record.stdout:
        blocks.append(TextContent(type="text", text=f"[stdout]\n{record.stdout}"))
    if record.stderr:
        blocks.append(TextContent(type="text", text=f"[stderr]\n{record.stderr}"))
    blocks.extend(record.image_blocks)
    if record.result is not None:
        blocks.append(TextContent(type="text", text=f"[result]\n{record.result}"))
    if record.error is not None:
        tb = "\n".join(record.error["traceback"])
        blocks.append(
            TextContent(
                type="text",
                text=f"[error]\n{record.error['ename']}: {record.error['evalue']}\n{tb}",
            )
        )
    if not complete:
        blocks.append(TextContent(type="text", text=f"[pending]\nmsg_id: {msg_id}"))
    return ToolResult(content=blocks)


@mcp.tool
async def kernel_execute(code: str, timeout: float | None = None) -> ToolResult:
    """Execute code on the running kernel and return structured output.

    Output is returned as tagged text blocks: [stdout], [stderr], [result]
    (last expression value), [error] (with traceback), plus image blocks for
    plots and display calls.

    If timeout is set and execution exceeds it, returns any output collected so
    far plus a [pending] block with a msg_id. Pass that msg_id to
    kernel_get_output to retrieve the rest. Without timeout, blocks until done.
    """
    if _kernel_client is None:
        return ToolResult(
            content=[
                TextContent(
                    type="text", text="No kernel running. Call kernel_start first."
                )
            ],
        )

    msg_id = _kernel_client.execute(code)
    record = ExecutionRecord()
    _executions[msg_id] = record

    try:
        await asyncio.wait_for(record.done_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        return _build_tool_result(record, msg_id=msg_id, complete=False)

    # Completed — clean up and return full result
    del _executions[msg_id]
    return _build_tool_result(record, msg_id=msg_id, complete=True)


@mcp.tool
async def kernel_get_output(msg_id: str, timeout: float | None = None) -> ToolResult:
    """Retrieve output for a pending execution by msg_id.

    If still running, waits up to timeout seconds (or returns immediately if
    timeout is omitted). Returns the same tagged blocks as kernel_execute:
    [stdout], [stderr], [result], [error], images, and [pending] if not yet done.

    Once complete output is returned, the record is cleaned up — calling again
    with the same msg_id will return an error.
    """
    if msg_id not in _executions:
        return ToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"Error: unknown msg_id '{msg_id}'. It may have already been retrieved.",
                )
            ],
        )

    record = _executions[msg_id]

    if not record.done and timeout is not None:
        try:
            await asyncio.wait_for(record.done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    complete = record.done
    if complete:
        del _executions[msg_id]
    return _build_tool_result(record, msg_id=msg_id, complete=complete)


def main() -> None:
    mcp.run()
