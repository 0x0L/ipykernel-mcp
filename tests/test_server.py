from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client

from ipykernel_mcp.server import _cleanup, _executions, mcp

# Use our own project's .venv as the test target — it has ipykernel as a dev dep.
PROJECT_DIR = str(Path(__file__).resolve().parent.parent)
VENV_KERNEL = f"venv:{PROJECT_DIR}"


@pytest.fixture(autouse=True)
async def clean_kernel_state():
    """Ensure no kernel is running before/after each test."""
    await _cleanup()
    yield
    await _cleanup()


# -- Discovery (no kernel needed, fast) ---------------------------------------


def _parse_specs(result) -> list[dict]:
    """Parse kernel_discover result — JSON list serialised in a TextContent block."""
    return json.loads(result.content[0].text)


async def test_discover_returns_specs():
    async with Client(mcp) as client:
        result = await client.call_tool("kernel_discover", {})
        specs = _parse_specs(result)
        assert isinstance(specs, list)
        assert len(specs) > 0
        # Should include a python3 spec (installed as dev dep)
        names = [s["name"] for s in specs]
        assert "python3" in names
        # Verify dict keys
        for spec in specs:
            assert set(spec.keys()) == {"name", "display_name", "language", "source"}
            assert spec["source"] == "jupyter_spec"


async def test_discover_with_scan_dir():
    async with Client(mcp) as client:
        result = await client.call_tool("kernel_discover", {"scan_dir": PROJECT_DIR})
        specs = _parse_specs(result)
        venv_specs = [s for s in specs if s["source"] == "project_venv"]
        assert len(venv_specs) == 1
        assert venv_specs[0]["name"] == VENV_KERNEL
        assert venv_specs[0]["language"] == "python"


async def test_discover_nonexistent_dir():
    async with Client(mcp) as client:
        result = await client.call_tool("kernel_discover", {"scan_dir": "/nonexistent"})
        specs = _parse_specs(result)
        # No crash — just no venv entry (still has jupyter specs)
        venv_specs = [s for s in specs if s["source"] == "project_venv"]
        assert len(venv_specs) == 0


async def test_discover_dir_without_venv(tmp_path):
    async with Client(mcp) as client:
        result = await client.call_tool("kernel_discover", {"scan_dir": str(tmp_path)})
        specs = _parse_specs(result)
        venv_specs = [s for s in specs if s["source"] == "project_venv"]
        assert len(venv_specs) == 0


# -- Error paths (stateless, fast) ------------------------------------------


async def test_status_no_kernel():
    async with Client(mcp) as client:
        result = await client.call_tool("kernel_status", {})
        assert result.data == {"running": False}


async def test_start_kernel_nonexistent_dir():
    async with Client(mcp) as client:
        result = await client.call_tool(
            "kernel_start", {"kernel_name": "venv:/nonexistent"}
        )
        assert "Error" in result.data
        assert "not a directory" in result.data


async def test_start_kernel_no_venv(tmp_path):
    async with Client(mcp) as client:
        result = await client.call_tool(
            "kernel_start", {"kernel_name": f"venv:{tmp_path}"}
        )
        assert "Error" in result.data
        assert "no venv found" in result.data


# -- Happy path (starts a real kernel, slower) ------------------------------


async def test_start_kernel():
    async with Client(mcp) as client:
        result = await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        assert "Kernel started" in result.data
        assert ".venv" in result.data
        assert "connection_file:" in result.data
        assert "npx github:0x0L/jupyter_watch" in result.data


async def test_start_by_jupyter_spec():
    """Start a kernel using a registered Jupyter spec name."""
    async with Client(mcp) as client:
        result = await client.call_tool("kernel_start", {"kernel_name": "python3"})
        assert "Kernel started" in result.data
        assert "Kernel: python3" in result.data
        # Execute something to verify it works
        result = await client.call_tool("kernel_execute", {"code": "1+1"})
        result_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[result]")
        ]
        assert len(result_blocks) == 1
        assert "2" in result_blocks[0].text


async def test_start_by_jupyter_spec_with_cwd(tmp_path):
    """Verify cwd is respected when starting a registered spec."""
    async with Client(mcp) as client:
        result = await client.call_tool(
            "kernel_start", {"kernel_name": "python3", "cwd": str(tmp_path)}
        )
        assert "Kernel started" in result.data
        result = await client.call_tool(
            "kernel_execute", {"code": "import os; print(os.getcwd())"}
        )
        stdout_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[stdout]")
        ]
        assert len(stdout_blocks) == 1
        # tmp_path may be a symlink, resolve both for comparison
        assert str(tmp_path.resolve()) in stdout_blocks[0].text


async def test_status_after_start():
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        result = await client.call_tool("kernel_status", {})
        data = result.data
        assert data["running"] is True
        assert data["alive"] is True
        assert data["project_dir"] == PROJECT_DIR
        assert "connection_file" in data
        assert "shell_port" in data


async def test_double_start_rejected():
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        result = await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        assert "Error" in result.data
        assert "already running" in result.data


async def test_cleanup_resets_state():
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        await _cleanup()
        result = await client.call_tool("kernel_status", {})
        assert result.data == {"running": False}


async def test_start_after_cleanup():
    """Can start a new kernel after cleaning up the previous one."""
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        await _cleanup()
        result = await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        assert "Kernel started" in result.data


# -- Stop / restart kernel ----------------------------------------------------


async def test_stop_kernel_no_kernel():
    async with Client(mcp) as client:
        result = await client.call_tool("kernel_stop", {})
        assert "Error" in result.data
        assert "no kernel is running" in result.data


async def test_stop_kernel():
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        result = await client.call_tool("kernel_stop", {})
        assert "Kernel stopped" in result.data
        status = await client.call_tool("kernel_status", {})
        assert status.data["running"] is False


async def test_restart_kernel_no_kernel():
    async with Client(mcp) as client:
        result = await client.call_tool("kernel_restart", {})
        assert "Error" in result.data
        assert "no kernel is running" in result.data


async def test_restart_kernel():
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        # Set a variable, restart, verify it's gone
        await client.call_tool("kernel_execute", {"code": "x = 42"})
        result = await client.call_tool("kernel_restart", {})
        assert "Kernel restarted" in result.data
        result = await client.call_tool("kernel_execute", {"code": "x"})
        error_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[error]")
        ]
        assert len(error_blocks) == 1
        assert "NameError" in error_blocks[0].text


# -- Interrupt kernel ---------------------------------------------------------


async def test_interrupt_no_kernel():
    async with Client(mcp) as client:
        result = await client.call_tool("kernel_interrupt", {})
        assert "Error" in result.data
        assert "no kernel is running" in result.data


async def test_interrupt_kernel():
    """Interrupting a long sleep should produce a KeyboardInterrupt error."""
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        # Start a long sleep, then interrupt it
        import asyncio

        exec_task = asyncio.create_task(
            client.call_tool("kernel_execute", {"code": "import time; time.sleep(60)"})
        )
        # Give the kernel a moment to start executing
        await asyncio.sleep(1)
        interrupt_result = await client.call_tool("kernel_interrupt", {})
        assert "Interrupt signal sent" in interrupt_result.data

        result = await exec_task
        error_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[error]")
        ]
        assert len(error_blocks) == 1
        assert "KeyboardInterrupt" in error_blocks[0].text


# -- Execute tool -------------------------------------------------------------


async def test_execute_no_kernel():
    async with Client(mcp) as client:
        result = await client.call_tool("kernel_execute", {"code": "1+1"})
        assert len(result.content) == 1
        assert "No kernel running" in result.content[0].text


async def test_execute_print():
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        result = await client.call_tool("kernel_execute", {"code": 'print("hello")'})
        stdout_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[stdout]")
        ]
        assert len(stdout_blocks) == 1
        assert "hello\n" in stdout_blocks[0].text


async def test_execute_expression():
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        result = await client.call_tool("kernel_execute", {"code": "2 + 2"})
        result_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[result]")
        ]
        assert len(result_blocks) == 1
        assert "4" in result_blocks[0].text


async def test_execute_error():
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        result = await client.call_tool("kernel_execute", {"code": "1/0"})
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
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        result = await client.call_tool(
            "kernel_execute", {"code": 'import sys; print("err", file=sys.stderr)'}
        )
        stderr_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[stderr]")
        ]
        assert len(stderr_blocks) == 1
        assert "err\n" in stderr_blocks[0].text


# -- Timeout and pending execution -------------------------------------------


async def test_execute_timeout_returns_partial():
    """A short timeout on slow code should return a [pending] block with msg_id."""
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        result = await client.call_tool(
            "kernel_execute",
            {"code": "import time; time.sleep(10); print('done')", "timeout": 1},
        )
        pending_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[pending]")
        ]
        assert len(pending_blocks) == 1
        assert "msg_id:" in pending_blocks[0].text


async def test_get_output_retrieves_remaining():
    """After a timeout, kernel_get_output retrieves the remaining output."""
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        # Execute code that prints before and after a short sleep
        result = await client.call_tool(
            "kernel_execute",
            {
                "code": "import time; print('before'); time.sleep(3); print('after')",
                "timeout": 0.5,
            },
        )
        pending_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[pending]")
        ]
        assert len(pending_blocks) == 1
        msg_id = pending_blocks[0].text.split("msg_id: ")[1].strip()

        # Wait for completion and retrieve output
        output = await client.call_tool(
            "kernel_get_output", {"msg_id": msg_id, "timeout": 10}
        )
        # Should be complete — no pending block
        pending_blocks = [
            b
            for b in output.content
            if b.type == "text" and b.text.startswith("[pending]")
        ]
        assert len(pending_blocks) == 0
        # Should have stdout with 'after'
        stdout_blocks = [
            b
            for b in output.content
            if b.type == "text" and b.text.startswith("[stdout]")
        ]
        assert len(stdout_blocks) == 1
        assert "after" in stdout_blocks[0].text


async def test_get_output_unknown_msg_id():
    """Requesting output for an unknown msg_id returns an error."""
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        result = await client.call_tool("kernel_get_output", {"msg_id": "bogus-msg-id"})
        assert len(result.content) == 1
        assert "unknown msg_id" in result.content[0].text


async def test_get_output_auto_cleanup():
    """After retrieving completed output, a second call returns unknown."""
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        result = await client.call_tool(
            "kernel_execute",
            {"code": "import time; time.sleep(3); print('hi')", "timeout": 0.5},
        )
        pending_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[pending]")
        ]
        msg_id = pending_blocks[0].text.split("msg_id: ")[1].strip()

        # First retrieval — wait for completion
        await client.call_tool("kernel_get_output", {"msg_id": msg_id, "timeout": 10})
        # Second retrieval — should be gone
        result2 = await client.call_tool("kernel_get_output", {"msg_id": msg_id})
        assert "unknown msg_id" in result2.content[0].text


async def test_cleanup_clears_executions():
    """Pending execution records are cleared after _cleanup()."""
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        await client.call_tool(
            "kernel_execute",
            {"code": "import time; time.sleep(30)", "timeout": 0.5},
        )
        assert len(_executions) == 1
        await _cleanup()
        assert len(_executions) == 0


async def test_restart_clears_executions():
    """Pending execution records are cleared after kernel_restart."""
    import asyncio

    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        # Start a long-running execution with timeout so it becomes pending
        exec_task = asyncio.create_task(
            client.call_tool(
                "kernel_execute",
                {"code": "import time; time.sleep(30)", "timeout": 0.5},
            )
        )
        await exec_task
        assert len(_executions) == 1
        await client.call_tool("kernel_restart", {})
        assert len(_executions) == 0


async def test_status_pending_count():
    """kernel_status should include the pending_executions count."""
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        # Initially no pending
        status = await client.call_tool("kernel_status", {})
        assert status.data["pending_executions"] == 0

        # Create a pending execution
        await client.call_tool(
            "kernel_execute",
            {"code": "import time; time.sleep(30)", "timeout": 0.5},
        )
        status = await client.call_tool("kernel_status", {})
        assert status.data["pending_executions"] == 1


# -- clear_output handling ----------------------------------------------------


async def test_clear_output_immediate():
    """clear_output(wait=False) should discard prior stdout."""
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        code = (
            "from IPython.display import clear_output\n"
            "print('old')\n"
            "clear_output(wait=False)\n"
            "print('new')"
        )
        result = await client.call_tool("kernel_execute", {"code": code})
        stdout_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[stdout]")
        ]
        assert len(stdout_blocks) == 1
        assert "old" not in stdout_blocks[0].text
        assert "new" in stdout_blocks[0].text


async def test_clear_output_wait():
    """clear_output(wait=True) defers the clear until the next output."""
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        # Simulate tqdm-like pattern: print, clear(wait=True), print replacement
        code = (
            "from IPython.display import clear_output\n"
            "for i in range(5):\n"
            "    clear_output(wait=True)\n"
            "    print(f'step {i}')\n"
        )
        result = await client.call_tool("kernel_execute", {"code": code})
        stdout_blocks = [
            b
            for b in result.content
            if b.type == "text" and b.text.startswith("[stdout]")
        ]
        assert len(stdout_blocks) == 1
        # Only the final iteration should survive
        assert "step 4" in stdout_blocks[0].text
        assert "step 0" not in stdout_blocks[0].text


# -- Instructions -------------------------------------------------------------


def test_server_instructions():
    assert mcp.instructions
    assert "kernel_start" in mcp.instructions
    assert "kernel_discover" in mcp.instructions
    assert "kernel_get_output" in mcp.instructions


# -- Content blocks (images, mixed output) ------------------------------------


async def test_execute_display_data_image():
    """display(Image(...)) should produce an ImageContent block."""
    async with Client(mcp) as client:
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        code = (
            "from IPython.display import display, Image\n"
            "import base64\n"
            "# 1x1 red PNG pixel\n"
            "png = base64.b64decode("
            "'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg==')\n"
            "display(Image(data=png, format='png'))"
        )
        result = await client.call_tool("kernel_execute", {"code": code})
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
        await client.call_tool("kernel_start", {"kernel_name": VENV_KERNEL})
        code = (
            "from IPython.display import display, Image\n"
            "import base64\n"
            "print('before image')\n"
            "png = base64.b64decode("
            "'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg==')\n"
            "display(Image(data=png, format='png'))\n"
            "print('after image')"
        )
        result = await client.call_tool("kernel_execute", {"code": code})
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
