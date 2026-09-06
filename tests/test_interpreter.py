import asyncio
import sys
import venv
from pathlib import Path

import pytest

from ipykernel_mcp.interpreter import InterpreterError, KernelConfig, check_python
from ipykernel_mcp.kernel import Kernel, KernelError


def test_explicit_interpreter_and_cwd(tmp_path):
    config = KernelConfig.from_paths(sys.executable, str(tmp_path))
    assert config.python == Path(sys.executable)
    assert config.cwd == tmp_path.resolve()


def test_cwd_defaults_to_server_directory():
    assert KernelConfig.from_paths(sys.executable).cwd == Path.cwd().resolve()


def test_interpreter_symlink_is_not_resolved(tmp_path):
    python = tmp_path / "custom-python"
    python.symlink_to(sys.executable)
    config = KernelConfig.from_paths(str(python))
    assert config.python == python


def test_relative_python_uses_server_cwd_not_configured_cwd(tmp_path, monkeypatch):
    python = tmp_path / "python"
    python.symlink_to(sys.executable)
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(tmp_path)
    config = KernelConfig.from_paths("python", "work")
    assert config.python == python
    assert config.cwd == cwd.resolve()


@pytest.mark.parametrize(
    "python,cwd,message",
    [
        ("", None, "--python"),
        ("/missing/python", None, "executable does not exist"),
        (sys.executable, "/missing/directory", "Working directory does not exist"),
    ],
)
def test_invalid_configuration(python, cwd, message):
    with pytest.raises(InterpreterError, match=message):
        KernelConfig.from_paths(python, cwd)


async def test_missing_ipykernel_fails_startup(tmp_path):
    # Use an arbitrary environment name, not the old .venv convention.
    environment = tmp_path / "analysis-python"
    await asyncio.to_thread(
        venv.EnvBuilder(with_pip=False, symlinks=sys.platform != "win32").create,
        environment,
    )
    python = environment / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    kernel = Kernel(KernelConfig.from_paths(str(python), str(tmp_path)))
    try:
        with pytest.raises(KernelError, match="ipykernel is missing"):
            await kernel.open()
        assert kernel.manager is None
        assert kernel.state == "unavailable"
    finally:
        await kernel.close()

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ipykernel_mcp.server",
        "--python",
        str(python),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 1
    assert not stdout
    assert b"ipykernel is missing" in stderr
    assert b"Traceback" not in stderr


async def test_interpreter_check_reports_os_error(tmp_path):
    with pytest.raises(InterpreterError, match="Cannot run interpreter"):
        await check_python(tmp_path / "missing")


async def test_preflight_cancellation_reaps_child(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from ipykernel_mcp.interpreter import check_python

    process = Mock()
    process.returncode = None
    began = asyncio.Event()

    async def communicate():
        began.set()
        await asyncio.Event().wait()

    process.communicate = communicate
    process.wait = AsyncMock()
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    task = asyncio.create_task(check_python(Path("/python")))
    await asyncio.wait_for(began.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
