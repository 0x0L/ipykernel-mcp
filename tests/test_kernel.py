from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from ipykernel_mcp.interpreter import KernelConfig
from ipykernel_mcp.kernel import Kernel, KernelError

PROJECT = str(Path(__file__).resolve().parent.parent)


def metadata(result):
    return result.structured_content


def output(result):
    return "\n".join(
        b.text
        for b in result.content
        if b.type == "text" and not b.text.startswith("[execution]")
    )


@pytest.fixture
async def kernel():
    kernel = Kernel(
        KernelConfig.from_paths(
            str(Path(sys.executable).with_name("jupyter")), "python3", PROJECT
        )
    )
    try:
        await kernel.open()
        yield kernel
    finally:
        await kernel.close()


async def test_error_and_input_fail_promptly(kernel):
    result = await kernel.execute("1 / 0")
    assert metadata(result)["status"] == "failed"
    assert "ZeroDivisionError" in output(result)
    assert "\x1b" not in output(result)
    result = await kernel.execute("input('value?')", wait_seconds=3)
    assert metadata(result)["status"] == "failed"
    assert "StdinNotImplementedError" in output(result)
    assert metadata(await kernel.execute("42"))["status"] == "succeeded"


async def test_displays_updates_and_clear(kernel):
    result = await kernel.execute(
        "from IPython.display import display; display({'answer': 42})"
    )
    assert "'answer': 42" in output(result)
    result = await kernel.execute(
        "h = display('old', display_id=True); h.update('new')"
    )
    assert "old" in output(result)
    assert "new" in output(result)
    for wait in (True, False):
        result = await kernel.execute(
            f"from IPython.display import clear_output; print('old'); clear_output(wait={wait}); print('new')"
        )
        assert "old" in output(result)
        assert "new" in output(result)


async def test_png_output(kernel):
    result = await kernel.execute(
        "from IPython.display import display, Image; import base64; display(Image(data=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=='), format='png'))"
    )
    assert any(b.type == "image" and b.mime_type == "image/png" for b in result.content)


async def test_incremental_retrieval_consumes_output(kernel):
    initial = await kernel.execute(
        "import time; print('before', flush=True); time.sleep(1); print('after')",
        wait_seconds=0.4,
    )
    info = metadata(initial)
    assert info["status"] == "running"
    assert "before" in output(initial)
    result = await kernel.read_output(info["execution_id"], wait_seconds=3)
    assert metadata(result)["status"] == "succeeded"
    assert "after" in output(result)
    assert "before" not in output(result)
    assert not kernel.executions
    with pytest.raises(KernelError, match="consumed"):
        await kernel.read_output(info["execution_id"])


async def test_busy_rejection_and_interrupt(kernel):
    initial = await kernel.execute("import time; time.sleep(30)", wait_seconds=0.2)
    key = metadata(initial)["execution_id"]
    assert Path(kernel.status()["connection_file"]).is_file()
    with pytest.raises(KernelError, match=key):
        await kernel.execute('print("must not execute")')
    await kernel.interrupt()
    result = await kernel.read_output(key, wait_seconds=3)
    assert metadata(result)["status"] == "cancelled"
    assert metadata(await kernel.execute("42"))["status"] == "succeeded"


async def test_restart_clears_variables(kernel):
    await kernel.execute("answer = 42")
    await kernel.restart()
    assert metadata(await kernel.execute("answer"))["error"]["type"] == "NameError"


async def test_kernel_death_resolves_wait_and_can_restart(kernel):
    result = await kernel.execute("import os; os._exit(0)", wait_seconds=5)
    assert metadata(result)["status"] == "failed"
    assert (kernel.status())["state"] == "unavailable"
    assert kernel.status()["connection_file"] is None
    await kernel.restart()
    assert metadata(await kernel.execute("42"))["status"] == "succeeded"


async def test_cancelled_tool_wait_leaves_execution_recoverable(kernel):
    task = asyncio.create_task(
        kernel.execute("import time; time.sleep(0.5); print('done')")
    )
    while kernel.active_execution_id is None:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    key = (kernel.status())["active_execution_id"]
    result = await kernel.read_output(key, wait_seconds=3)
    assert metadata(result)["status"] == "succeeded"
    assert "done" in output(result)


async def test_concurrent_retrievals_are_safe(kernel):
    initial = await kernel.execute("import time; time.sleep(0.2); 42", wait_seconds=0)
    key = metadata(initial)["execution_id"]
    results = await asyncio.gather(
        kernel.read_output(key, wait_seconds=3),
        kernel.read_output(key, wait_seconds=3),
        return_exceptions=True,
    )
    assert sum(isinstance(result, KernelError) for result in results) == 1
    success = next(
        result for result in results if not isinstance(result, BaseException)
    )
    assert metadata(success)["status"] == "succeeded"
    assert "42" in output(success)
    assert not kernel.executions


async def test_bounded_unread_retention_and_expiry(kernel):
    from ipykernel_mcp.execution import Execution

    kernel.max_retained_executions = 2
    for i in range(3):
        record = Execution(str(i))
        record.append("stdout", "unread")
        record.finish("succeeded")
        kernel.executions[str(i)] = record
    assert len(kernel.status()["executions"]) == 2
    with pytest.raises(KernelError, match="expired"):
        await kernel.read_output("0")
    kernel.executions["2"].finished_at = (
        time.monotonic() - kernel.result_retention_seconds - 1
    )
    with pytest.raises(KernelError, match="expired"):
        await kernel.read_output("2")


async def test_large_output_is_bounded(kernel):
    result = await kernel.execute("print('x' * 200000)")
    assert metadata(result)["truncated"]
    assert not kernel.executions
    assert len(output(result).encode()) < 66000
    assert metadata(result)["status"] == "succeeded"


async def test_updates_belong_to_the_execution_that_produced_them(kernel):
    first = await kernel.execute(
        "from IPython.display import display; display('old', display_id='shared')"
    )
    first_id = metadata(first)["execution_id"]
    second = await kernel.execute(
        "from IPython.display import update_display; update_display('new', display_id='shared')"
    )
    assert "new" in output(second)
    assert "old" not in output(second)
    assert not kernel.executions
    with pytest.raises(KernelError, match="consumed"):
        await kernel.read_output(first_id)
    await kernel.restart()
    third = await kernel.execute(
        "from IPython.display import display, update_display; display('another', display_id='shared'); update_display('different', display_id='shared')"
    )
    assert "another" in output(third) and "different" in output(third)
    assert not kernel.executions


async def test_polling_after_clear_and_update_returns_only_new_output(kernel):
    first = await kernel.execute(
        "import time; from IPython.display import display, clear_output, update_display; "
        "display('before', display_id='handle'); time.sleep(1); "
        "clear_output(wait=True); update_display('after', display_id='handle')",
        wait_seconds=0.4,
    )
    info = metadata(first)
    assert info["status"] == "running"
    assert "before" in output(first)
    result = await kernel.read_output(info["execution_id"], wait_seconds=3)
    assert metadata(result)["status"] == "succeeded"
    assert "after" in output(result) and "before" not in output(result)
    assert not kernel.executions


async def test_persistent_state_and_configured_interpreter(kernel):
    assert kernel.state == "ready"
    await kernel.execute("answer = 42")
    assert "42" in output(await kernel.execute("answer"))
    result = await kernel.execute(
        "import os, sys; print(os.getcwd()); print(sys.executable)"
    )
    assert PROJECT in output(result)
    assert sys.executable in output(result)
    assert kernel.status()["state"] == "ready"


@pytest.mark.parametrize(
    "action, error_type", [("close", "ServerClosed"), ("restart", "KernelRestarted")]
)
async def test_lifecycle_wakes_waiters(kernel, action, error_type):
    task = asyncio.create_task(
        kernel.execute("import time; time.sleep(30)", wait_seconds=60)
    )
    async with asyncio.timeout(2):
        while kernel.active_execution_id is None:
            await asyncio.sleep(0.01)
    execution_id = kernel.active_execution_id
    await getattr(kernel, action)()
    result = await asyncio.wait_for(task, 2)
    assert metadata(result)["status"] == "cancelled"
    assert metadata(result)["error"]["type"] == error_type
    if action == "restart":
        with pytest.raises(KernelError, match="consumed"):
            await kernel.read_output(execution_id)
        assert metadata(await kernel.execute("42"))["status"] == "succeeded"
    else:
        assert not kernel.executions


async def test_restart_restores_initial_directory(kernel, tmp_path):
    await kernel.execute(f"import os; os.chdir({str(tmp_path)!r})")
    await kernel.restart()
    assert PROJECT in output(await kernel.execute("import os; print(os.getcwd())"))
    assert kernel.config.kernel_name == "python3"


@pytest.mark.parametrize("budget", [-1, 61, float("inf"), float("nan")])
async def test_invalid_wait_budget_does_not_execute(kernel, budget):
    with pytest.raises(KernelError, match="wait_seconds"):
        await kernel.execute("42", wait_seconds=budget)
    assert kernel.active_execution_id is None
    assert not kernel.executions


async def test_empty_code_and_unknown_execution(kernel):
    with pytest.raises(KernelError, match="empty"):
        await kernel.execute("  ")
    with pytest.raises(KernelError, match="Unknown"):
        await kernel.read_output("missing")


async def test_idle_interrupt_is_noop(kernel):
    assert await kernel.interrupt() == {"interrupt_sent": False, "execution_id": None}
    assert metadata(await kernel.execute("42"))["status"] == "succeeded"


async def test_simultaneous_opens_create_one_kernel():
    kernel = Kernel(
        KernelConfig.from_paths(
            str(Path(sys.executable).with_name("jupyter")), "python3", PROJECT
        )
    )
    try:
        results = await asyncio.gather(
            kernel.open(), kernel.open(), return_exceptions=True
        )
        assert sum(isinstance(result, KernelError) for result in results) == 1
        assert metadata(await kernel.execute("42"))["status"] == "succeeded"
    finally:
        await kernel.close()


async def test_drain_real_kernel_keeps_future_output_and_variables(kernel):
    initial = await kernel.execute(
        "import time; print('before', flush=True); time.sleep(0.7); "
        "print('after', flush=True); answer = 42",
        wait_seconds=0,
    )
    key = metadata(initial)["execution_id"]
    async with asyncio.timeout(3):
        while kernel.status()["unread_output_count"] == 0:
            await asyncio.sleep(0.01)
    first = await kernel.drain_output()
    assert "before" in output(first)
    assert metadata(first)["executions"][0]["status"] == "running"
    assert kernel.status()["unread_output_count"] == 0
    assert key in kernel.executions
    async with asyncio.timeout(3):
        await kernel.executions[key].done_event.wait()
    last = await kernel.drain_output()
    assert "after" in output(last) and "before" not in output(last)
    assert metadata(last)["executions"][0]["status"] == "succeeded"
    assert not kernel.executions
    assert metadata(await kernel.drain_output()) == {"executions": []}
    assert "42" in output(await kernel.execute("answer"))
