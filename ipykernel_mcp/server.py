from __future__ import annotations

import re
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan
from fastmcp.tools.tool import ToolResult
from jupyter_client.blocking import BlockingKernelClient
from jupyter_client.kernelspec import KernelSpec
from jupyter_client.manager import KernelManager
from mcp.types import ImageContent, TextContent


@lifespan
async def kernel_lifespan(server):
    try:
        yield {}
    finally:
        _cleanup()


mcp = FastMCP(
    "ipykernel-mcp",
    instructions=(
        "This server provides a persistent Python (IPython) kernel. Variables, "
        "imports, and definitions survive across `execute` calls, so you can build "
        "up state incrementally — just like a notebook.\n"
        "\n"
        "Call `start_kernel` once with a project directory (must have a .venv with "
        "ipykernel installed), then call `execute` as many times as needed. Each "
        "call returns stdout, stderr, result (the expression value), and error "
        "(traceback details) as structured output.\n"
        "\n"
        "Use `restart_kernel` to clear all state. Use `stop_kernel` to shut down. "
        "Only one kernel runs at a time."
    ),
    lifespan=kernel_lifespan,
)

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

    return f"Kernel started. Python: {venv_python}, cwd: {project_path}, connection_file: {km.connection_file}"


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


@mcp.tool
def execute(code: str, timeout: float = 30.0) -> ToolResult:
    """Execute code on the running IPython kernel and return the output.

    Returns structured output (stdout, stderr, result, error) and MCP content
    blocks including images from plots and display calls.
    """
    if _kernel_client is None:
        return ToolResult(
            content=[
                TextContent(
                    type="text", text="No kernel running. Call start_kernel first."
                )
            ],
        )

    msg_id = _kernel_client.execute(code)

    stdout = ""
    stderr = ""
    result = None
    error = None
    image_blocks: list[ImageContent] = []

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
        elif msg_type in ("execute_result", "display_data"):
            data = content["data"]
            image_blocks.extend(_extract_images(data))
            if msg_type == "execute_result":
                result = data.get("text/plain")
        elif msg_type == "error":
            traceback = [_ANSI_ESCAPE.sub("", line) for line in content["traceback"]]
            error = {
                "ename": content["ename"],
                "evalue": content["evalue"],
                "traceback": traceback,
            }
        elif msg_type == "status" and content["execution_state"] == "idle":
            break

    # Build MCP content blocks in order: stdout, stderr, images, result, error
    blocks: list[TextContent | ImageContent] = []
    if stdout:
        blocks.append(TextContent(type="text", text=f"[stdout]\n{stdout}"))
    if stderr:
        blocks.append(TextContent(type="text", text=f"[stderr]\n{stderr}"))
    blocks.extend(image_blocks)
    if result is not None:
        blocks.append(TextContent(type="text", text=f"[result]\n{result}"))
    if error is not None:
        tb = "\n".join(error["traceback"])
        blocks.append(
            TextContent(
                type="text",
                text=f"[error]\n{error['ename']}: {error['evalue']}\n{tb}",
            )
        )

    return ToolResult(content=blocks)


# -- Prompts ------------------------------------------------------------------


@mcp.prompt
def run_code(code: str) -> str:
    """Execute Python code and explain the output."""
    return (
        f"Run the following code using the `execute` tool, then explain the output "
        f"(stdout, result, and any errors):\n\n```python\n{code}\n```"
    )


@mcp.prompt
def debug_error(code: str, error: str) -> str:
    """Debug a piece of code that produces an error."""
    return (
        f"The following code produces an error:\n\n```python\n{code}\n```\n\n"
        f"Error:\n```\n{error}\n```\n\n"
        f"Diagnose the root cause, fix the code, and re-run it with the `execute` "
        f"tool to verify the fix works."
    )


@mcp.prompt
def explore_project(project_dir: str) -> str:
    """Start a kernel and explore a Python project."""
    return (
        f"Start a kernel for the project at `{project_dir}` using `start_kernel`, "
        f"then explore it:\n"
        f"1. List the files (`import os; os.listdir('.')`).\n"
        f"2. Check installed packages (`import pkg_resources; "
        f"[d.project_name for d in pkg_resources.working_set]`).\n"
        f"3. Summarize what the project does based on its structure and dependencies."
    )


def main() -> None:
    mcp.run()
