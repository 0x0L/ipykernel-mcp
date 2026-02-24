from __future__ import annotations

import re
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from jupyter_client.blocking import BlockingKernelClient
from jupyter_client.kernelspec import KernelSpec
from jupyter_client.manager import KernelManager


@lifespan
async def kernel_lifespan(server):
    try:
        yield {}
    finally:
        _cleanup()


mcp = FastMCP("ipykernel-mcp", lifespan=kernel_lifespan)

_kernel_manager: KernelManager | None = None
_kernel_client: BlockingKernelClient | None = None
_project_dir: str | None = None


def _cleanup() -> None:
    global _kernel_manager, _kernel_client, _project_dir
    if _kernel_client is not None:
        _kernel_client.stop_channels()
        _kernel_client = None
    if _kernel_manager is not None:
        _kernel_manager.shutdown_kernel(now=True)
        _kernel_manager = None
    _project_dir = None


@mcp.tool
def start_kernel(project_dir: str) -> str:
    """Start an IPython kernel using the venv from the given project directory.

    The project directory must contain a .venv with ipykernel installed.
    """
    global _kernel_manager, _kernel_client, _project_dir

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

    km = KernelManager()
    km._kernel_spec = spec

    try:
        km.start_kernel(cwd=str(project_path))
    except Exception as exc:
        return f"Error: failed to start kernel: {exc}"

    kc = km.client()
    kc.start_channels()

    try:
        kc.wait_for_ready(timeout=60)
    except RuntimeError as exc:
        kc.stop_channels()
        km.shutdown_kernel(now=True)
        return f"Error: kernel did not become ready: {exc}"

    _kernel_manager = km
    _kernel_client = kc
    _project_dir = str(project_path)

    return f"Kernel started. Python: {venv_python}, cwd: {project_path}"


@mcp.tool
def status() -> dict:
    """Return the current kernel status."""
    if _kernel_manager is None:
        return {"running": False}

    info: dict = {
        "running": True,
        "alive": _kernel_manager.is_alive(),
        "project_dir": _project_dir,
        "python": _kernel_manager._kernel_spec.argv[0]
        if _kernel_manager._kernel_spec
        else None,
    }

    ci = _kernel_manager.get_connection_info()
    info["transport"] = ci.get("transport")
    info["ip"] = ci.get("ip")
    info["shell_port"] = ci.get("shell_port")
    info["iopub_port"] = ci.get("iopub_port")

    return info


@mcp.tool
def stop_kernel() -> str:
    """Stop the running kernel and clean up resources."""
    if _kernel_manager is None:
        return "Error: no kernel is running."
    _cleanup()
    return "Kernel stopped."


@mcp.tool
def restart_kernel() -> str:
    """Restart the running kernel, preserving the connection."""
    if _kernel_manager is None or _kernel_client is None:
        return "Error: no kernel is running."
    try:
        _kernel_manager.restart_kernel(now=True)
        _kernel_client.wait_for_ready(timeout=60)
    except Exception as exc:
        _cleanup()
        return f"Error: failed to restart kernel: {exc}"
    return "Kernel restarted."


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


@mcp.tool
def execute(code: str, timeout: float = 30.0) -> dict:
    """Execute code on the running IPython kernel and return the output.

    Returns a dict with keys: stdout, stderr, result, error.
    """
    if _kernel_client is None:
        return {"error": "No kernel running. Call start_kernel first."}

    msg_id = _kernel_client.execute(code)

    stdout = ""
    stderr = ""
    result = None
    error = None

    while True:
        msg = _kernel_client.get_iopub_msg(timeout=timeout)

        # Only process messages from our execution request.
        if msg["parent_header"].get("msg_id") != msg_id:
            continue

        msg_type = msg["msg_type"]
        content = msg["content"]

        if msg_type == "stream":
            if content["name"] == "stdout":
                stdout += content["text"]
            elif content["name"] == "stderr":
                stderr += content["text"]
        elif msg_type == "execute_result":
            result = content["data"].get("text/plain")
        elif msg_type == "error":
            traceback = [_ANSI_ESCAPE.sub("", line) for line in content["traceback"]]
            error = {
                "ename": content["ename"],
                "evalue": content["evalue"],
                "traceback": traceback,
            }
        elif msg_type == "status" and content["execution_state"] == "idle":
            break

    output: dict = {"stdout": stdout, "stderr": stderr}
    if result is not None:
        output["result"] = result
    if error is not None:
        output["error"] = error
    return output


def main() -> None:
    mcp.run()
