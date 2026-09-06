"""Deterministic fault injection for paths that real kernels rarely exercise."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from jupyter_client import AsyncKernelClient, AsyncKernelManager

from ipykernel_mcp.execution import Execution
from ipykernel_mcp.interpreter import KernelConfig
from ipykernel_mcp.kernel import Kernel, KernelError


@pytest.fixture
async def fake_kernel(monkeypatch, tmp_path):
    manager = Mock(spec=AsyncKernelManager)
    client = Mock(spec=AsyncKernelClient)
    manager.start_kernel = AsyncMock()
    manager.shutdown_kernel = AsyncMock()
    manager.restart_kernel = AsyncMock()
    manager.is_alive = AsyncMock(return_value=True)
    client.wait_for_ready = AsyncMock()
    manager.client.return_value = client
    monkeypatch.setattr(
        "ipykernel_mcp.kernel.create_kernel_manager", lambda config: manager
    )
    monkeypatch.setattr("ipykernel_mcp.kernel.check_kernel", AsyncMock())
    kernel = Kernel(KernelConfig("python3", tmp_path))
    try:
        yield kernel, manager, client
    finally:
        await kernel.close()


@pytest.mark.parametrize(
    "failure", [RuntimeError("not ready"), OSError("channel failed")]
)
async def test_readiness_failure_cleans_process_and_channels(fake_kernel, failure):
    kernel, manager, client = fake_kernel
    client.wait_for_ready.side_effect = failure
    with pytest.raises(KernelError, match="Failed to start Jupyter kernel"):
        await kernel.open()
    manager.shutdown_kernel.assert_awaited_once()
    client.stop_channels.assert_called_once()
    assert kernel.manager is None
    assert kernel.state == "unavailable"


async def test_cancelled_startup_reaps_kernel(fake_kernel):
    kernel, manager, client = fake_kernel
    ready = asyncio.Event()

    async def wait_for_ready(**kwargs):
        ready.set()
        await asyncio.Event().wait()

    client.wait_for_ready.side_effect = wait_for_ready
    task = asyncio.create_task(kernel.open())
    await asyncio.wait_for(ready.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    manager.shutdown_kernel.assert_awaited_once()
    assert kernel.manager is None
    assert not kernel.background_tasks


async def test_cancelled_close_finishes_shutdown(fake_kernel):
    kernel, manager, client = fake_kernel
    kernel.manager, kernel.client = manager, client
    began, finish = asyncio.Event(), asyncio.Event()

    async def shutdown(**kwargs):
        began.set()
        await finish.wait()

    manager.shutdown_kernel.side_effect = shutdown
    task = asyncio.create_task(kernel.close())
    await asyncio.wait_for(began.wait(), 2)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kernel.manager is None
    assert kernel.state == "closed"


async def test_reset_failure_resolves_execution_and_reaps_kernel(fake_kernel):
    kernel, manager, client = fake_kernel
    kernel.manager, kernel.client = manager, client
    execution = Execution("active")
    kernel.executions["active"] = execution
    kernel.active_execution_id = "active"
    client.wait_for_ready.side_effect = OSError("reset failed")
    with pytest.raises(KernelError, match="reset failed"):
        await kernel.reset()
    assert execution.done_event.is_set()
    assert execution.status == "cancelled"
    assert kernel.manager is None
    assert manager.shutdown_kernel.await_count == 2


async def test_reader_failure_resolves_pending_wait(fake_kernel):
    kernel, manager, client = fake_kernel
    kernel.manager, kernel.client = manager, client
    kernel.state = "busy"
    execution = Execution("active")
    kernel.executions["active"] = execution
    kernel.active_execution_id = "active"
    client.get_iopub_msg = AsyncMock(side_effect=ConnectionError("socket closed"))
    await kernel._read_channel("iopub")
    assert execution.status == "failed"
    assert execution.done_event.is_set()
    assert kernel.state == "unavailable"
    assert kernel.active_execution_id is None


async def test_shutdown_failure_preserves_ownership_for_retry(fake_kernel):
    kernel, manager, client = fake_kernel
    kernel.manager, kernel.client = manager, client
    manager.shutdown_kernel.side_effect = [OSError("shutdown failed"), None]
    with pytest.raises(OSError, match="shutdown failed"):
        await kernel.close()
    assert kernel.manager is manager
    assert kernel.state == "unavailable"
    await kernel.close()
    assert kernel.manager is None
