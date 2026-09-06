import asyncio
import sys
from pathlib import Path

import pytest

from ipykernel_mcp.interpreter import InterpreterError, KernelConfig, check_kernel
from ipykernel_mcp.kernel import Kernel, KernelError


def test_explicit_kernel_and_cwd(tmp_path):
    config = KernelConfig.from_paths("python3", str(tmp_path))
    assert config.kernel_name == "python3"
    assert config.cwd == tmp_path.resolve()


def test_cwd_defaults_to_server_directory():
    assert KernelConfig.from_paths("python3").cwd == Path.cwd().resolve()


@pytest.mark.parametrize(
    "kernel,cwd,message",
    [
        ("", None, "--kernel"),
        ("python3", "/missing/directory", "Working directory does not exist"),
    ],
)
def test_invalid_configuration(kernel, cwd, message):
    with pytest.raises(InterpreterError, match=message):
        KernelConfig.from_paths(kernel, cwd)


async def test_missing_kernel_fails_startup(tmp_path):
    kernel = Kernel(KernelConfig.from_paths("missing-kernel", str(tmp_path)))
    try:
        with pytest.raises(KernelError, match="Jupyter kernel is not installed"):
            await kernel.open()
        assert kernel.manager is None
        assert kernel.state == "unavailable"
    finally:
        await kernel.close()


async def test_kernel_check_reports_missing_kernel():
    with pytest.raises(InterpreterError, match="Jupyter kernel is not installed"):
        await check_kernel("missing-kernel")


async def test_cli_rejects_missing_kernel():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ipykernel_mcp.server",
        "--kernel",
        "missing-kernel",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 1
    assert not stdout
    assert b"Jupyter kernel is not installed" in stderr
    assert b"Traceback" not in stderr
