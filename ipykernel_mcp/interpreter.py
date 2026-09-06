"""Validate a fixed Jupyter kernel specification and construct its manager."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from jupyter_client import AsyncKernelManager
from jupyter_client.kernelspec import KernelSpecManager, NoSuchKernel


class InterpreterError(ValueError):
    """The configured Jupyter kernel specification cannot be used."""


@dataclass(frozen=True)
class KernelConfig:
    kernel_name: str
    cwd: Path

    @classmethod
    def from_paths(cls, kernel_name: str, cwd: str | None = None) -> KernelConfig:
        if not kernel_name.strip():
            raise InterpreterError("--kernel must name an installed Jupyter kernel.")
        directory = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
        if not directory.is_dir():
            raise InterpreterError(f"Working directory does not exist: {directory}")
        return cls(kernel_name, directory)


def create_kernel_manager(config: KernelConfig) -> AsyncKernelManager:
    return AsyncKernelManager(kernel_name=config.kernel_name)


async def check_kernel(kernel_name: str) -> None:
    try:
        await asyncio.to_thread(KernelSpecManager().get_kernel_spec, kernel_name)
    except NoSuchKernel as exc:
        raise InterpreterError(
            f"Jupyter kernel is not installed: {kernel_name}. "
            "Install its kernelspec, then use `jupyter kernelspec list` to find its name."
        ) from exc
