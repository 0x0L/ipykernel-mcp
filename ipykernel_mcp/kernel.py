"""One kernel, one active execution, and explicit lifecycle ownership."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from queue import Empty

from anyio import CancelScope
from fastmcp.tools import ToolResult
from jupyter_client import AsyncKernelClient, AsyncKernelManager

from .execution import Execution
from .interpreter import KernelConfig, check_kernel, create_kernel_manager
from .schemas import DrainOutput, ExecutionStatus

logger = logging.getLogger(__name__)
DEFAULT_WAIT_SECONDS = 10.0
MAX_WAIT_SECONDS = 60.0
RESULT_RETENTION_SECONDS = 600.0
MAX_RETAINED_EXECUTIONS = 16


class KernelError(ValueError):
    """A tool operation could not be performed (distinct from a code error)."""


def validate_wait_seconds(value: float) -> float:
    if not math.isfinite(value) or not 0 <= value <= MAX_WAIT_SECONDS:
        raise KernelError(f"wait_seconds must be between 0 and {MAX_WAIT_SECONDS:g}.")
    return value


class Kernel:
    def __init__(
        self,
        config: KernelConfig,
        *,
        result_retention_seconds: float = RESULT_RETENTION_SECONDS,
        max_retained_executions: int = MAX_RETAINED_EXECUTIONS,
    ) -> None:
        if result_retention_seconds <= 0 or max_retained_executions < 1:
            raise ValueError(
                "Result retention and execution capacity must be positive."
            )
        self.config = config
        self.manager: AsyncKernelManager | None = None
        self.client: AsyncKernelClient | None = None
        self.state = "closed"
        self.error: dict[str, str] | None = None
        self.active_execution_id: str | None = None
        self.executions: dict[str, Execution] = {}
        self.background_tasks: list[asyncio.Task] = []
        self.lifecycle_lock = asyncio.Lock()
        self.result_retention_seconds = result_retention_seconds
        self.max_retained_executions = max_retained_executions

    def _prune_results(self, *, reserve: bool = False) -> None:
        now = time.monotonic()
        for key, execution in list(self.executions.items()):
            if (
                execution.finished_at is not None
                and now - execution.finished_at >= self.result_retention_seconds
            ):
                del self.executions[key]
        limit = self.max_retained_executions - int(reserve)
        for key, execution in list(self.executions.items()):
            if len(self.executions) <= limit:
                break
            if execution.done:
                del self.executions[key]

    def _finish_active(
        self, status: ExecutionStatus, error_type: str, message: str
    ) -> None:
        execution = (
            self.executions.get(self.active_execution_id)
            if self.active_execution_id is not None
            else None
        )
        if execution is not None:
            execution.finish(status, error_type, message)
        self.active_execution_id = None

    def _fail(self, error_type: str, message: str) -> None:
        self.error = {"type": error_type, "message": message}
        self.state = "unavailable"
        self._finish_active("failed", error_type, message)
        logger.warning("%s", message)

    async def _read_channel(self, channel: str) -> None:
        assert self.client is not None
        client = self.client
        receive = client.get_iopub_msg if channel == "iopub" else client.get_shell_msg
        try:
            while True:
                try:
                    message = await receive(timeout=1)
                except Empty:
                    continue
                key = message.get("parent_header", {}).get("msg_id")
                execution = self.executions.get(key)
                if execution is None:
                    continue
                execution.handle_message(message["msg_type"], message["content"])
                if execution.done and self.active_execution_id == key:
                    self.active_execution_id = None
                    if self.state == "busy":
                        self.state = "ready"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Kernel output reader failed")
            self._fail(
                "OutputConnectionError",
                f"Kernel {channel} reader failed: {exc}. Call reset() to recover.",
            )

    async def _watch_process(self) -> None:
        try:
            while True:
                await asyncio.sleep(0.25)
                self._prune_results()
                if self.manager is not None and not await self.manager.is_alive():
                    self._fail(
                        "KernelDied",
                        "Kernel process exited. State was lost; call reset() to recover.",
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Kernel health check failed")
            self._fail("OutputConnectionError", f"Kernel health check failed: {exc}")

    def _start_background_tasks(self) -> None:
        self.background_tasks = [
            asyncio.create_task(self._read_channel("iopub")),
            asyncio.create_task(self._read_channel("shell")),
            asyncio.create_task(self._watch_process()),
        ]

    async def _cancel_background_tasks(self) -> None:
        for task in self.background_tasks:
            task.cancel()
        await asyncio.gather(*self.background_tasks, return_exceptions=True)
        self.background_tasks.clear()

    async def _release_resources(self) -> None:
        await self._cancel_background_tasks()
        if self.client is not None:
            self.client.stop_channels()
            self.client = None
        if self.manager is not None:
            # Keep ownership if shutdown fails, allowing reset or close to retry.
            await self.manager.shutdown_kernel(now=True)
            self.manager = None

    async def _cleanup_resources(self) -> None:
        """Complete resource cleanup under asyncio and AnyIO cancellation."""
        with CancelScope(shield=True):
            cleanup = asyncio.create_task(self._release_resources())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

    async def _start_kernel(self) -> None:
        self.error = None
        try:
            await check_kernel(self.config.kernel_name)
            self.manager = create_kernel_manager(self.config)
            await self.manager.start_kernel(cwd=str(self.config.cwd))
            self.client = self.manager.client()
            self.client.start_channels()
            await self.client.wait_for_ready(timeout=30)
        except BaseException as exc:
            try:
                await self._cleanup_resources()
            except Exception:
                logger.exception("Cleanup after failed startup failed")
            self.state = "unavailable"
            self.error = {"type": type(exc).__name__, "message": str(exc)}
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise KernelError(
                f"Failed to start Jupyter kernel {self.config.kernel_name!r}: {exc}"
            ) from exc
        self.state = "ready"
        self._start_background_tasks()

    async def open(self) -> None:
        """Start the configured interpreter when the owning server starts."""
        async with self.lifecycle_lock:
            if self.manager is not None:
                raise KernelError("The configured Jupyter kernel is already running.")
            self.state = "starting"
            await self._start_kernel()

    async def reset(self) -> dict:
        """Replace the Jupyter kernel, recovering from process or reader failure."""
        async with self.lifecycle_lock:
            self.state = "resetting"
            self._finish_active(
                "cancelled",
                "WorkspaceReset",
                "The workspace was reset before execution completed.",
            )
            try:
                await self._cleanup_resources()
                await self._start_kernel()
            except BaseException as exc:
                self.state = "unavailable"
                self.error = {"type": type(exc).__name__, "message": str(exc)}
                raise
            return self.status()

    async def interrupt(self) -> dict:
        async with self.lifecycle_lock:
            if self.manager is None or self.state not in ("ready", "busy"):
                raise KernelError("Workspace is unavailable. Call reset() to recover.")
            execution_id = self.active_execution_id
            if execution_id is not None:
                await self.manager.interrupt_kernel()
            return {
                "execution_id": execution_id,
                "interrupt_sent": execution_id is not None,
            }

    async def execute(
        self, code: str, wait_seconds: float = DEFAULT_WAIT_SECONDS
    ) -> ToolResult:
        validate_wait_seconds(wait_seconds)
        if not code.strip():
            raise KernelError("code must not be empty.")
        async with self.lifecycle_lock:
            if self.client is None or self.state not in ("ready", "busy"):
                raise KernelError("Workspace is unavailable. Call reset() to recover.")
            if self.active_execution_id:
                raise KernelError(
                    f"Kernel is busy. Retrieve or interrupt execution_id={self.active_execution_id} before submitting more code."
                )
            self._prune_results(reserve=True)
            # Sending and registering are synchronous: readers cannot interleave here.
            key = self.client.execute(code, allow_stdin=False, stop_on_error=True)
            execution = Execution(key)
            self.executions[key] = execution
            self.active_execution_id = key
            self.state = "busy"
        # Request cancellation cancels only this wait. The execution remains
        # discoverable through status and can be retrieved or interrupted.
        await self._wait_for_completion(execution, wait_seconds)
        return self._consume_result(execution)

    @staticmethod
    async def _wait_for_completion(execution: Execution, seconds: float) -> None:
        if not execution.done and seconds:
            try:
                await asyncio.wait_for(execution.done_event.wait(), seconds)
            except TimeoutError:
                pass

    async def read_output(
        self,
        execution_id: str,
        wait_seconds: float = 0,
    ) -> ToolResult:
        validate_wait_seconds(wait_seconds)
        self._prune_results()
        execution = self.executions.get(execution_id)
        if execution is None:
            raise KernelError(
                "Unknown, consumed, or expired execution_id. Use status() to find pending executions."
            )
        await self._wait_for_completion(execution, wait_seconds)
        return self._consume_result(execution)

    def _consume_result(self, execution: Execution) -> ToolResult:
        # No awaits between building and consuming: competing readers cannot
        # receive the same payload or completed outcome, even after waiting.
        if execution.delivered:
            raise KernelError(
                "Execution result was already consumed by another reader."
            )
        result = execution.to_tool_result()
        self._consume(execution)
        return result

    def _consume(self, execution: Execution) -> None:
        execution.consume()
        if execution.done:
            self.executions.pop(execution.execution_id, None)

    async def drain_output(self) -> ToolResult:
        """Atomically return pending output and outcomes, grouped by execution."""
        self._prune_results()
        pending = [
            execution
            for execution in self.executions.values()
            if execution.outputs or execution.truncated or execution.done
        ]
        # Build the entire response before releasing anything. No await permits
        # channel readers, other consumers, or cancellation to interleave here.
        results = [execution.to_tool_result() for execution in pending]
        metadata = DrainOutput.model_validate(
            {"executions": [result.structured_content for result in results]}
        ).model_dump()
        result = ToolResult(
            content=[block for result in results for block in result.content],
            structured_content=metadata,
        )
        for execution in pending:
            self._consume(execution)
        return result

    def status(self) -> dict:
        self._prune_results()
        return {
            "state": self.state,
            "kernel_name": self.config.kernel_name,
            "cwd": str(self.config.cwd),
            "active_execution_id": self.active_execution_id,
            "executions": [
                {
                    "execution_id": execution_id,
                    "status": execution.status,
                    "unread_output_count": len(execution.outputs),
                }
                for execution_id, execution in self.executions.items()
            ],
            "unread_output_count": sum(
                len(e.outputs) for e in self.executions.values()
            ),
            "error": self.error,
        }

    async def close(self) -> None:
        """Finish active waits and release the interpreter on server shutdown."""
        async with self.lifecycle_lock:
            self._finish_active(
                "cancelled",
                "ServerClosed",
                "The server closed before execution completed.",
            )
            try:
                await self._cleanup_resources()
            finally:
                self.state = "unavailable" if self.manager is not None else "closed"
                self.executions.clear()
