import asyncio
import sys
from pathlib import Path

import pytest

from ipykernel_mcp.interpreter import InterpreterError, KernelConfig, check_kernel
from ipykernel_mcp.kernel import Kernel, KernelError


def test_explicit_kernel_and_cwd(tmp_path):
    config = KernelConfig.from_paths(
        str(Path(sys.executable).with_name("jupyter")), "python3", str(tmp_path)
    )
    assert config.kernel_name == "python3"
    assert config.cwd == tmp_path.resolve()


def test_cwd_defaults_to_server_directory():
    assert (
        KernelConfig.from_paths(
            str(Path(sys.executable).with_name("jupyter")), "python3"
        ).cwd
        == Path.cwd().resolve()
    )


@pytest.mark.parametrize(
    "kernel,cwd,message",
    [
        ("", None, "--kernel"),
        ("python3", "/missing/directory", "Working directory does not exist"),
    ],
)
def test_invalid_configuration(kernel, cwd, message):
    with pytest.raises(InterpreterError, match=message):
        KernelConfig.from_paths(
            str(Path(sys.executable).with_name("jupyter")), kernel, cwd
        )


async def test_missing_kernel_fails_startup(tmp_path):
    kernel = Kernel(
        KernelConfig.from_paths(
            str(Path(sys.executable).with_name("jupyter")),
            "missing-kernel",
            str(tmp_path),
        )
    )
    try:
        with pytest.raises(KernelError, match="Jupyter kernel is not installed"):
            await kernel.open()
        assert kernel.manager is None
        assert kernel.state == "unavailable"
    finally:
        await kernel.close()


async def test_kernel_check_reports_missing_kernel():
    with pytest.raises(InterpreterError, match="Jupyter kernel is not installed"):
        await check_kernel(
            KernelConfig.from_paths(
                str(Path(sys.executable).with_name("jupyter")), "missing-kernel"
            )
        )


async def test_cli_rejects_missing_kernel():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ipykernel_mcp.server",
        "--jupyter",
        str(Path(sys.executable).with_name("jupyter")),
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


@pytest.mark.parametrize("value", ["", "/missing/jupyter"])
def test_missing_jupyter_executable(value):
    with pytest.raises(InterpreterError, match="--jupyter"):
        KernelConfig.from_paths(value, "python3")


def test_non_executable_jupyter(tmp_path):
    launcher = tmp_path / "jupyter"
    launcher.write_text("not executable")
    with pytest.raises(InterpreterError, match="--jupyter"):
        KernelConfig.from_paths(str(launcher), "python3")


async def test_selected_cli_owns_discovery_launch_and_restart(tmp_path):
    """A kernel visible only to the selected CLI must work across restart."""
    import json

    from jupyter_client.kernelspec import KernelSpecManager

    spec_dir = tmp_path / "private-data" / "kernels" / "private-test-kernel"
    spec_dir.mkdir(parents=True)
    (spec_dir / "kernel.json").write_text(
        json.dumps(
            {
                "argv": [
                    sys.executable,
                    "-m",
                    "ipykernel_launcher",
                    "-f",
                    "{connection_file}",
                ],
                "display_name": "Private Python",
                "language": "python",
            }
        )
    )
    assert "private-test-kernel" not in KernelSpecManager().find_kernel_specs()
    log = tmp_path / "calls.jsonl"
    launcher = tmp_path / "selected jupyter"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"os.environ['JUPYTER_PATH'] = {str(spec_dir.parent.parent)!r}\n"
        "os.environ['SELECTED_JUPYTER_TEST'] = 'selected environment'\n"
        f"os.execv({str(Path(sys.executable).with_name('jupyter'))!r}, ['jupyter', *sys.argv[1:]])\n"
    )
    launcher.chmod(0o700)
    kernel = Kernel(
        KernelConfig.from_paths(str(launcher), "private-test-kernel", str(tmp_path))
    )
    managers = []
    try:
        await kernel.open()
        managers.append(kernel.manager)
        result = await kernel.execute(
            "import os; print(os.environ['SELECTED_JUPYTER_TEST'])"
        )
        assert result.structured_content is not None
        assert result.structured_content["status"] == "succeeded"
        assert any(
            "selected environment" in b.text for b in result.content if b.type == "text"
        )
        await kernel.restart()
        managers.append(kernel.manager)
        assert kernel.status()["jupyter"] == str(launcher)
        result = await kernel.execute("40 + 2")
        assert result.structured_content is not None
        assert result.structured_content["status"] == "succeeded"
    finally:
        await kernel.close()
    for manager in managers:
        assert manager is not None
        assert manager.process is not None
        assert manager.process.returncode is not None
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call[0] for call in calls] == [
        "kernelspec",
        "kernel",
        "kernelspec",
        "kernel",
    ]


async def test_cancelled_real_launcher_startup_is_reaped(monkeypatch, tmp_path):
    from jupyter_client import AsyncKernelClient

    reached_readiness = asyncio.Event()

    async def stalled_readiness(self, **kwargs):
        reached_readiness.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(AsyncKernelClient, "wait_for_ready", stalled_readiness)
    kernel = Kernel(
        KernelConfig.from_paths(
            str(Path(sys.executable).with_name("jupyter")), "python3", str(tmp_path)
        )
    )
    task = asyncio.create_task(kernel.open())
    try:
        await asyncio.wait_for(reached_readiness.wait(), 10)
        manager = kernel.manager
        assert manager is not None
        process = manager.process
        assert process is not None
        directory = manager._directory
        assert directory is not None
        runtime = Path(directory.name)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.returncode is not None
        assert not runtime.exists()
        assert kernel.manager is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await kernel.close()
