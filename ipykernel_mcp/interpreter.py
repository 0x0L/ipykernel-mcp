"""Discover and launch kernels through an explicitly configured Jupyter CLI."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from jupyter_client import AsyncKernelClient
from jupyter_client.kernelspec import KernelSpec


class InterpreterError(ValueError):
    """The configured Jupyter executable or kernel cannot be used."""


@dataclass(frozen=True)
class KernelConfig:
    jupyter: Path
    kernel_name: str
    cwd: Path

    @classmethod
    def from_paths(
        cls, jupyter: str, kernel_name: str, cwd: str | None = None
    ) -> KernelConfig:
        # Preserve the executable's path: resolving a venv symlink can change its env.
        executable = Path(jupyter).expanduser().absolute()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise InterpreterError(
                f"--jupyter must point to an executable file: {jupyter}"
            )
        if not kernel_name.strip():
            raise InterpreterError("--kernel must name an installed Jupyter kernel.")
        directory = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
        if not directory.is_dir():
            raise InterpreterError(f"Working directory does not exist: {directory}")
        return cls(executable, kernel_name, directory)


async def check_kernel(config: KernelConfig) -> KernelSpec:
    """Query the selected Jupyter installation, never the server's kernelspec paths."""
    process = await asyncio.create_subprocess_exec(
        str(config.jupyter),
        "kernelspec",
        "list",
        "--json",
        cwd=str(config.cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise InterpreterError(
            f"Jupyter kernelspec discovery failed: {stderr.decode(errors='replace')[-4096:]}"
        )
    try:
        specs = json.loads(stdout)["kernelspecs"]
        spec = specs[config.kernel_name]
    except KeyError as exc:
        raise InterpreterError(
            f"Jupyter kernel is not installed: {config.kernel_name}. "
            f"Use `{config.jupyter} kernelspec list` to find its name."
        ) from exc
    except (ValueError, TypeError) as exc:
        raise InterpreterError("Jupyter returned invalid kernelspec JSON.") from exc
    return KernelSpec(resource_dir=spec["resource_dir"], **spec["spec"])


class JupyterKernelManager:
    """Own a Jupyter CLI process and connect to its kernel over Jupyter channels.

    A private config runs a small lifecycle relay in the launcher environment.
    It delegates interrupts to Jupyter's manager (including signal-mode kernels),
    requests orderly shutdown, and exits when the kernel dies. No server package
    needs to be installed in the selected Jupyter environment.
    """

    def __init__(self, config: KernelConfig):
        self.config = config
        self.kernel_spec: KernelSpec | None = None
        self.process: asyncio.subprocess.Process | None = None
        self._directory: tempfile.TemporaryDirectory | None = None
        self._client: AsyncKernelClient | None = None

    async def start_kernel(self, *, cwd: str) -> None:
        self.kernel_spec = await check_kernel(self.config)
        self._directory = tempfile.TemporaryDirectory(prefix="ipykernel-mcp-")
        runtime = Path(self._directory.name)
        connection = runtime / "connection.json"
        env = dict(os.environ, IPYKERNEL_MCP_RUNTIME_DIR=str(runtime))
        # Never let CLI logging or kernel output corrupt the MCP stdout transport.
        self.process = await asyncio.create_subprocess_exec(
            str(self.config.jupyter),
            "kernel",
            "--kernel",
            self.config.kernel_name,
            "--JupyterApp.config_file",
            str(Path(__file__).with_name("launcher_config.py")),
            f"--KernelManager.connection_file={connection}",
            "--KernelManager.autorestart=False",
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=None,
        )
        async with asyncio.timeout(30):
            while True:
                if self.process.returncode is not None:
                    raise InterpreterError(
                        f"Jupyter launcher exited with status {self.process.returncode}; see server stderr."
                    )
                try:
                    if not (runtime / "ready").exists():
                        raise FileNotFoundError("Waiting for Jupyter lifecycle relay")
                    client = AsyncKernelClient(connection_file=str(connection))
                    client.load_connection_file()
                except (OSError, ValueError):
                    await asyncio.sleep(0.05)
                else:
                    self._client = client
                    return

    def client(self) -> AsyncKernelClient:
        """Return the client after the launcher has written its connection file."""
        assert self._client is not None
        return self._client

    async def is_alive(self) -> bool:
        """The relay exits when its kernel dies; it never restarts the kernel."""
        return self.process is not None and self.process.returncode is None

    async def interrupt_kernel(self) -> None:
        """Ask the launcher's manager to use the kernelspec's interrupt mode."""
        assert self._directory is not None
        request = Path(self._directory.name) / "interrupt"
        request.touch()
        async with asyncio.timeout(5):
            while request.exists():
                if not await self.is_alive():
                    raise InterpreterError(
                        "Jupyter launcher exited before delivering the interrupt."
                    )
                await asyncio.sleep(0.02)

    async def shutdown_kernel(self, *, now: bool = True) -> None:
        """Request orderly CLI/kernel shutdown, then reap the launcher and files."""
        if self.process is not None and self.process.returncode is None:
            assert self._directory is not None
            (Path(self._directory.name) / "shutdown").touch()
            try:
                await asyncio.wait_for(self.process.wait(), 15)
            except TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 10)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        if self._directory is not None:
            self._directory.cleanup()
            self._directory = None


def create_kernel_manager(config: KernelConfig) -> JupyterKernelManager:
    return JupyterKernelManager(config)
