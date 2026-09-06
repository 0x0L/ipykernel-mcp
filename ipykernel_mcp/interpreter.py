"""Validate a fixed Python interpreter and construct its kernel launch spec."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from anyio import CancelScope
from jupyter_client import AsyncKernelManager
from jupyter_client.kernelspec import KernelSpec


class InterpreterError(ValueError):
    """The configured Python executable cannot run an IPython kernel."""


@dataclass(frozen=True)
class KernelConfig:
    python: Path
    cwd: Path

    @classmethod
    def from_paths(cls, python: str, cwd: str | None = None) -> KernelConfig:
        if not python.strip():
            raise InterpreterError("--python must name a Python executable.")
        # Resolving the executable's symlink would bypass its virtual environment.
        executable = Path(python).expanduser().absolute()
        directory = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
        if not executable.is_file():
            raise InterpreterError(f"Python executable does not exist: {executable}")
        if not directory.is_dir():
            raise InterpreterError(f"Working directory does not exist: {directory}")
        return cls(executable, directory)


def create_kernel_manager(config: KernelConfig) -> AsyncKernelManager:
    manager = AsyncKernelManager()
    # jupyter-client has no public setter for an ad-hoc KernelSpec. Keep this
    # dependency isolated here and covered by real-interpreter tests.
    manager._kernel_spec = KernelSpec(
        argv=[
            str(config.python),
            "-m",
            "ipykernel_launcher",
            "-f",
            "{connection_file}",
        ],
        display_name="Python",
        language="python",
    )
    return manager


async def check_python(python: Path) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            str(python),
            "-c",
            "import importlib.util, sys\n"
            "if importlib.util.find_spec('ipykernel') is None:\n"
            "    sys.exit(3)\n"
            "import ipykernel\n",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise InterpreterError(f"Cannot run interpreter {python}: {exc}") from exc
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), 10)
    except BaseException as exc:
        with CancelScope(shield=True):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
        if isinstance(exc, TimeoutError):
            raise InterpreterError(
                f"Interpreter check timed out for {python} after 10 seconds."
            ) from exc
        raise
    if process.returncode == 3:
        raise InterpreterError(
            f"ipykernel is missing from {python}. Install ipykernel in that environment."
        )
    if process.returncode:
        raise InterpreterError(
            f"Interpreter check failed for {python}: {stderr.decode(errors='replace')[-2000:]}"
        )
