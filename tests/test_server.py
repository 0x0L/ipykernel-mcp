from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client

from ipykernel_mcp.server import _cleanup, mcp

# Use our own project's .venv as the test target — it has ipykernel as a dev dep.
PROJECT_DIR = str(Path(__file__).resolve().parent.parent)


@pytest.fixture(autouse=True)
def clean_kernel_state():
    """Ensure no kernel is running before/after each test."""
    _cleanup()
    yield
    _cleanup()


# -- Error paths (stateless, fast) ------------------------------------------


async def test_status_no_kernel():
    async with Client(mcp) as client:
        result = await client.call_tool("status", {})
        assert result.data == {"running": False}


async def test_start_kernel_nonexistent_dir():
    async with Client(mcp) as client:
        result = await client.call_tool("start_kernel", {"project_dir": "/nonexistent"})
        assert "Error" in result.data
        assert "not a directory" in result.data


async def test_start_kernel_no_venv(tmp_path):
    async with Client(mcp) as client:
        result = await client.call_tool("start_kernel", {"project_dir": str(tmp_path)})
        assert "Error" in result.data
        assert "no venv found" in result.data


# -- Happy path (starts a real kernel, slower) ------------------------------


async def test_start_kernel():
    async with Client(mcp) as client:
        result = await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        assert "Kernel started" in result.data
        assert ".venv/bin/python" in result.data


async def test_status_after_start():
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        result = await client.call_tool("status", {})
        data = result.data
        assert data["running"] is True
        assert data["alive"] is True
        assert data["project_dir"] == PROJECT_DIR
        assert "shell_port" in data


async def test_double_start_rejected():
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        result = await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        assert "Error" in result.data
        assert "already running" in result.data


async def test_cleanup_resets_state():
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        _cleanup()
        result = await client.call_tool("status", {})
        assert result.data == {"running": False}


async def test_start_after_cleanup():
    """Can start a new kernel after cleaning up the previous one."""
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        _cleanup()
        result = await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        assert "Kernel started" in result.data


# -- Stop / restart kernel ----------------------------------------------------


async def test_stop_kernel_no_kernel():
    async with Client(mcp) as client:
        result = await client.call_tool("stop_kernel", {})
        assert "Error" in result.data
        assert "no kernel is running" in result.data


async def test_stop_kernel():
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        result = await client.call_tool("stop_kernel", {})
        assert "Kernel stopped" in result.data
        status = await client.call_tool("status", {})
        assert status.data["running"] is False


async def test_restart_kernel_no_kernel():
    async with Client(mcp) as client:
        result = await client.call_tool("restart_kernel", {})
        assert "Error" in result.data
        assert "no kernel is running" in result.data


async def test_restart_kernel():
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        # Set a variable, restart, verify it's gone
        await client.call_tool("execute", {"code": "x = 42"})
        result = await client.call_tool("restart_kernel", {})
        assert "Kernel restarted" in result.data
        result = await client.call_tool("execute", {"code": "x"})
        assert result.data.get("error") is not None
        assert result.data["error"]["ename"] == "NameError"


# -- Execute tool -------------------------------------------------------------


async def test_execute_no_kernel():
    async with Client(mcp) as client:
        result = await client.call_tool("execute", {"code": "1+1"})
        assert "error" in result.data
        assert "No kernel running" in result.data["error"]


async def test_execute_print():
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        result = await client.call_tool("execute", {"code": 'print("hello")'})
        assert result.data["stdout"] == "hello\n"


async def test_execute_expression():
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        result = await client.call_tool("execute", {"code": "2 + 2"})
        assert result.data["result"] == "4"


async def test_execute_error():
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        result = await client.call_tool("execute", {"code": "1/0"})
        err = result.data["error"]
        assert err["ename"] == "ZeroDivisionError"
        assert "division by zero" in err["evalue"]
        # Verify ANSI codes are stripped from traceback
        for line in err["traceback"]:
            assert "\x1b[" not in line


async def test_execute_stderr():
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        result = await client.call_tool(
            "execute", {"code": 'import sys; print("err", file=sys.stderr)'}
        )
        assert result.data["stderr"] == "err\n"
