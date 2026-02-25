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
        assert "connection_file:" in result.data
        assert "npx github:0x0L/jupyter_watch" in result.data


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
        error_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[error]")
        ]
        assert len(error_blocks) == 1
        assert "NameError" in error_blocks[0].text


# -- Execute tool -------------------------------------------------------------


async def test_execute_no_kernel():
    async with Client(mcp) as client:
        result = await client.call_tool("execute", {"code": "1+1"})
        assert len(result.content) == 1
        assert "No kernel running" in result.content[0].text


async def test_execute_print():
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        result = await client.call_tool("execute", {"code": 'print("hello")'})
        stdout_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[stdout]")
        ]
        assert len(stdout_blocks) == 1
        assert "hello\n" in stdout_blocks[0].text


async def test_execute_expression():
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        result = await client.call_tool("execute", {"code": "2 + 2"})
        result_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[result]")
        ]
        assert len(result_blocks) == 1
        assert "4" in result_blocks[0].text


async def test_execute_error():
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        result = await client.call_tool("execute", {"code": "1/0"})
        error_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[error]")
        ]
        assert len(error_blocks) == 1
        text = error_blocks[0].text
        assert "ZeroDivisionError" in text
        assert "division by zero" in text
        # Verify ANSI codes are stripped from traceback
        assert "\x1b[" not in text


async def test_execute_stderr():
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        result = await client.call_tool(
            "execute", {"code": 'import sys; print("err", file=sys.stderr)'}
        )
        stderr_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[stderr]")
        ]
        assert len(stderr_blocks) == 1
        assert "err\n" in stderr_blocks[0].text


# -- Instructions & prompts ---------------------------------------------------


def test_server_instructions():
    assert mcp.instructions
    assert "start_kernel" in mcp.instructions


async def test_list_prompts():
    async with Client(mcp) as client:
        prompts = await client.list_prompts()
        names = {p.name for p in prompts}
        assert "run_code" in names
        assert "debug_error" in names
        assert "explore_project" in names


async def test_run_code_prompt():
    async with Client(mcp) as client:
        result = await client.get_prompt("run_code", {"code": "1+1"})
        assert result.messages
        assert any("1+1" in m.content.text for m in result.messages)


async def test_debug_error_prompt():
    async with Client(mcp) as client:
        result = await client.get_prompt(
            "debug_error", {"code": "1/0", "error": "ZeroDivisionError"}
        )
        assert result.messages
        assert any("1/0" in m.content.text for m in result.messages)


# -- Content blocks (images, mixed output) ------------------------------------


async def test_execute_display_data_image():
    """display(Image(...)) should produce an ImageContent block."""
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        code = (
            "from IPython.display import display, Image\n"
            "import base64\n"
            "# 1x1 red PNG pixel\n"
            "png = base64.b64decode("
            "'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg==')\n"
            "display(Image(data=png, format='png'))"
        )
        result = await client.call_tool("execute", {"code": code})
        # No stdout block expected
        stdout_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[stdout]")
        ]
        assert len(stdout_blocks) == 0
        # Check content blocks contain an image
        image_blocks = [b for b in result.content if b.type == "image"]
        assert len(image_blocks) >= 1
        assert image_blocks[0].mimeType == "image/png"
        assert len(image_blocks[0].data) > 0


async def test_execute_mixed_output():
    """Printing text AND displaying an image should produce both block types."""
    async with Client(mcp) as client:
        await client.call_tool("start_kernel", {"project_dir": PROJECT_DIR})
        code = (
            "from IPython.display import display, Image\n"
            "import base64\n"
            "print('before image')\n"
            "png = base64.b64decode("
            "'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg==')\n"
            "display(Image(data=png, format='png'))\n"
            "print('after image')"
        )
        result = await client.call_tool("execute", {"code": code})
        # stdout block has the combined stdout
        stdout_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[stdout]")
        ]
        assert len(stdout_blocks) == 1
        assert "before image" in stdout_blocks[0].text
        assert "after image" in stdout_blocks[0].text
        # Content blocks should have text and image
        types = [b.type for b in result.content]
        assert "text" in types
        assert "image" in types
