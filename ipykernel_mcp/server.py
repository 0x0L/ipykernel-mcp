from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from fastmcp.tools.tool import ToolResult
from jupyter_client import AsyncKernelClient, AsyncKernelManager
from jupyter_client.kernelspec import KernelSpec, KernelSpecManager
from mcp.types import ImageContent, TextContent

# ---------------------------------------------------------------------------
# Constants & data types
# ---------------------------------------------------------------------------

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


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


# ---------------------------------------------------------------------------
# Pure helpers (no kernel state)
# ---------------------------------------------------------------------------


def _find_venv_python(project_path: Path) -> Path | None:
    """Find the Python executable in a project's .venv, cross-platform."""
    if sys.platform == "win32":
        python = project_path / ".venv" / "Scripts" / "python.exe"
    else:
        python = project_path / ".venv" / "bin" / "python"
    return python if python.exists() else None


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


# ---------------------------------------------------------------------------
# KernelSession — owns the 5 former globals and their lifecycle
# ---------------------------------------------------------------------------


class KernelSession:
    """Manages a single Jupyter kernel and its iopub reader."""

    def __init__(self) -> None:
        self.manager: AsyncKernelManager | None = None
        self.client: AsyncKernelClient | None = None
        self.cwd: str | None = None
        self.executions: dict[str, ExecutionRecord] = {}
        self.reader_task: asyncio.Task | None = None

    # -- reader lifecycle ---------------------------------------------------

    def _start_reader(self) -> None:
        self.reader_task = asyncio.create_task(self._run_iopub_reader())

    async def _cancel_reader(self) -> None:
        if self.reader_task is not None:
            self.reader_task.cancel()
            try:
                await self.reader_task
            except asyncio.CancelledError:
                pass
            self.reader_task = None

    async def _run_iopub_reader(self) -> None:
        """Background task that routes iopub messages to ExecutionRecords."""
        assert self.client is not None
        while True:
            try:
                msg = await self.client.get_iopub_msg(timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

            msg_id = msg["parent_header"].get("msg_id")
            if msg_id is None or msg_id not in self.executions:
                continue

            record = self.executions[msg_id]
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
                traceback = [
                    _ANSI_ESCAPE.sub("", line) for line in content["traceback"]
                ]
                record.error = {
                    "ename": content["ename"],
                    "evalue": content["evalue"],
                    "traceback": traceback,
                }
            elif msg_type == "status" and content["execution_state"] == "idle":
                record.done = True
                record.done_event.set()

    # -- kernel lifecycle ---------------------------------------------------

    async def start(self, kernel_name: str, cwd: str | None = None) -> str:
        if self.manager is not None:
            return "Error: a kernel is already running. Shut it down first."

        if kernel_name.startswith("venv:"):
            project_path = Path(kernel_name[5:]).resolve()
            if not project_path.is_dir():
                return f"Error: {project_path} is not a directory."

            venv_python = _find_venv_python(project_path)
            if venv_python is None:
                return f"Error: no venv found at {project_path / '.venv'}."

            spec = KernelSpec(
                argv=[
                    str(venv_python),
                    "-m",
                    "ipykernel_launcher",
                    "-f",
                    "{connection_file}",
                ],
                display_name=f"Python (.venv) — {project_path.name}",
                language="python",
            )
            km = AsyncKernelManager()
            km._kernel_spec = spec
            effective_cwd = cwd or str(project_path)
            kernel_label = f"Python: {venv_python}"
        else:
            km = AsyncKernelManager(kernel_name=kernel_name)
            effective_cwd = cwd or str(Path.home())
            kernel_label = f"Kernel: {kernel_name}"

        try:
            await km.start_kernel(cwd=effective_cwd)
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

        self.manager = km
        self.client = kc
        self.cwd = effective_cwd
        self._start_reader()

        return (
            f"Kernel started. {kernel_label}, cwd: {effective_cwd}, "
            f"connection_file: {km.connection_file}\n"
            f"Monitor output: npx github:0x0L/jupyter_watch {km.connection_file}"
        )

    async def stop(self) -> None:
        await self._cancel_reader()
        self.executions.clear()
        if self.client is not None:
            self.client.stop_channels()
            self.client = None
        if self.manager is not None:
            await self.manager.shutdown_kernel(now=True)
            self.manager = None
        self.cwd = None

    async def restart(self) -> str:
        if self.manager is None or self.client is None:
            return "Error: no kernel is running."

        await self._cancel_reader()
        self.executions.clear()

        try:
            await self.manager.restart_kernel(now=True)
            await self.client.wait_for_ready(timeout=60)
        except Exception as exc:
            await self.stop()
            return f"Error: failed to restart kernel: {exc}"

        self._start_reader()
        return "Kernel restarted."

    async def interrupt(self) -> str:
        if self.manager is None:
            return "Error: no kernel is running."
        await self.manager.interrupt_kernel()
        return "Interrupt signal sent."

    async def execute(self, code: str, timeout: float | None = None) -> ToolResult:
        if self.client is None:
            return ToolResult(
                content=[
                    TextContent(
                        type="text", text="No kernel running. Call kernel_start first."
                    )
                ],
            )

        msg_id = self.client.execute(code)
        record = ExecutionRecord()
        self.executions[msg_id] = record

        try:
            await asyncio.wait_for(record.done_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return _build_tool_result(record, msg_id=msg_id, complete=False)

        del self.executions[msg_id]
        return _build_tool_result(record, msg_id=msg_id, complete=True)

    async def get_output(self, msg_id: str, timeout: float | None = None) -> ToolResult:
        if msg_id not in self.executions:
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"Error: unknown msg_id '{msg_id}'. It may have already been retrieved.",
                    )
                ],
            )

        record = self.executions[msg_id]

        if not record.done and timeout is not None:
            try:
                await asyncio.wait_for(record.done_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

        complete = record.done
        if complete:
            del self.executions[msg_id]
        return _build_tool_result(record, msg_id=msg_id, complete=complete)

    async def status_dict(self) -> dict:
        if self.manager is None:
            return {"running": False}

        info: dict = {
            "running": True,
            "alive": await self.manager.is_alive(),
            "project_dir": self.cwd,
            "connection_file": self.manager.connection_file,
            "python": self.manager._kernel_spec.argv[0]
            if self.manager._kernel_spec
            else None,
            "pending_executions": sum(
                1 for r in self.executions.values() if not r.done
            ),
        }

        ci = self.manager.get_connection_info()
        info["transport"] = ci.get("transport")
        info["ip"] = ci.get("ip")
        info["shell_port"] = ci.get("shell_port")
        info["iopub_port"] = ci.get("iopub_port")

        return info


# ---------------------------------------------------------------------------
# Singleton + backward-compat aliases for tests
# ---------------------------------------------------------------------------

_session = KernelSession()
_cleanup = _session.stop
_executions = _session.executions

# ---------------------------------------------------------------------------
# FastMCP server + lifespan
# ---------------------------------------------------------------------------


@lifespan
async def kernel_lifespan(server):
    try:
        yield {}
    finally:
        await _session.stop()


mcp = FastMCP(
    "ipykernel-mcp",
    instructions=(
        "This server provides a persistent IPython/Jupyter kernel. Variables, "
        "imports, and definitions survive across `execute` calls, so you can build "
        "up state incrementally — just like a notebook.\n"
        "\n"
        "Call `kernel_discover` to list available kernels (registered Jupyter specs "
        "like python3, R, Julia, plus project venvs). Then call `kernel_start` with "
        "a `kernel_name` from the discovery results (e.g. `python3` or "
        "`venv:/path/to/project`). Call `kernel_execute` as many times as needed. "
        "Each call returns stdout, stderr, result (the expression value), and error "
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

# ---------------------------------------------------------------------------
# MCP tool wrappers (thin — docstrings stay here for MCP discovery)
# ---------------------------------------------------------------------------


@mcp.tool
async def kernel_discover(scan_dir: str | None = None) -> list[dict]:
    """Discover available Jupyter kernel specs and project venvs.

    Returns a list of dicts with keys: name, display_name, language, source.
    Use the "name" value as the kernel_name argument to kernel_start.

    If scan_dir is provided, also checks for a .venv with ipykernel installed
    at that path and includes it as a "venv:<path>" entry.
    """
    specs: list[dict] = []

    ksm = KernelSpecManager()
    for name, resource_dir in ksm.find_kernel_specs().items():
        ks = ksm.get_kernel_spec(name)
        specs.append(
            {
                "name": name,
                "display_name": ks.display_name,
                "language": ks.language,
                "source": "jupyter_spec",
            }
        )

    if scan_dir is not None:
        project_path = Path(scan_dir).resolve()
        venv_python = _find_venv_python(project_path)
        if venv_python is not None:
            try:
                subprocess.run(
                    [str(venv_python), "-c", "import ipykernel"],
                    check=True,
                    capture_output=True,
                    timeout=10,
                )
                specs.append(
                    {
                        "name": f"venv:{project_path}",
                        "display_name": f"Python (.venv) — {project_path.name}",
                        "language": "python",
                        "source": "project_venv",
                    }
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass

    return specs


@mcp.tool
async def kernel_start(kernel_name: str, cwd: str | None = None) -> str:
    """Start a kernel by name, as returned by kernel_discover.

    kernel_name is either a registered Jupyter spec name (e.g. "python3") or a
    venv reference ("venv:/path/to/project") from kernel_discover.

    cwd sets the working directory. Defaults to the venv project dir for venv
    kernels, or the user's home directory for registered specs.

    Only one kernel can run at a time — call kernel_stop first if one is
    already running.
    """
    return await _session.start(kernel_name, cwd)


@mcp.tool
async def kernel_status() -> dict:
    """Return the current kernel status.

    Returns a dict with: running, alive, project_dir, connection_file, python,
    pending_executions, transport, ip, shell_port, iopub_port.
    Returns {"running": False} if no kernel is running.
    """
    return await _session.status_dict()


@mcp.tool
async def kernel_stop() -> str:
    """Stop the running kernel and clean up resources."""
    if _session.manager is None:
        return "Error: no kernel is running."
    await _session.stop()
    return "Kernel stopped."


@mcp.tool
async def kernel_restart() -> str:
    """Restart the kernel, clearing all variables, imports, and state.

    Pending executions are discarded. The kernel process is replaced but the
    connection is preserved — no need to call kernel_start again.
    """
    return await _session.restart()


@mcp.tool
async def kernel_interrupt() -> str:
    """Interrupt the running kernel (send SIGINT).

    Use this to cancel a long-running execution without losing kernel state.
    """
    return await _session.interrupt()


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
    return await _session.execute(code, timeout)


@mcp.tool
async def kernel_get_output(msg_id: str, timeout: float | None = None) -> ToolResult:
    """Retrieve output for a pending execution by msg_id.

    If still running, waits up to timeout seconds (or returns immediately if
    timeout is omitted). Returns the same tagged blocks as kernel_execute:
    [stdout], [stderr], [result], [error], images, and [pending] if not yet done.

    Once complete output is returned, the record is cleaned up — calling again
    with the same msg_id will return an error.
    """
    return await _session.get_output(msg_id, timeout)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    mcp.run()
