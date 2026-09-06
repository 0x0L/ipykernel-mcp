"""Deterministic consuming-read behavior without kernel timing dependencies."""

import asyncio

import pytest

from ipykernel_mcp.execution import Execution
from ipykernel_mcp.interpreter import KernelConfig
from ipykernel_mcp.kernel import Kernel, KernelError


@pytest.fixture
def workspace(tmp_path):
    return Kernel(KernelConfig("python3", tmp_path))


def add(workspace, key, *, done=False):
    execution = Execution(key)
    workspace.executions[key] = execution
    if done:
        execution.finish("succeeded")
    return execution


async def test_status_counts_blocks_without_consuming_and_drain_groups(workspace):
    first = add(workspace, "first")
    first.append("stdout", "a")
    first.append("stdout", "b")
    first.append("display", "cG5n", "image/png")
    first.finish("succeeded")
    silent = add(workspace, "silent", done=True)
    running = add(workspace, "running")
    running.append("stderr", "pending")
    state = workspace.status()
    assert state == workspace.status()
    assert state["unread_output_count"] == 3
    assert [e["unread_output_count"] for e in state["executions"]] == [2, 0, 1]
    result = await workspace.drain_output()
    assert [e["execution_id"] for e in result.structured_content["executions"]] == [
        "first",
        "silent",
        "running",
    ]
    assert [block.type for block in result.content] == [
        "text",
        "text",
        "image",
        "text",
        "text",
        "text",
    ]
    assert result.content[1].text == "[stdout]\nab"
    assert set(workspace.executions) == {"running"}
    for execution in (first, silent, running):
        assert not execution.outputs
        assert execution.stored_bytes == execution.text_bytes == 0
    assert workspace.status()["unread_output_count"] == 0
    running.append("stdout", "later")
    assert workspace.status()["unread_output_count"] == 1
    assert "later" in (await workspace.read_output("running")).content[1].text


async def test_drain_delivers_truncation_without_blocks_and_replenishes(workspace):
    execution = add(workspace, "running")
    execution.max_bytes = 3
    execution.append("display", "cG5n", "image/png")
    assert workspace.status()["unread_output_count"] == 0
    result = await workspace.drain_output()
    assert result.structured_content["executions"][0]["truncated"]
    assert not execution.truncated
    assert (await workspace.drain_output()).structured_content == {"executions": []}
    execution.append("stdout", "ok")
    assert not (await workspace.read_output("running")).structured_content["truncated"]


async def test_drain_wins_against_waiting_reader_without_replaying(workspace):
    execution = add(workspace, "pending")
    reader = asyncio.create_task(workspace.read_output("pending", wait_seconds=5))
    await asyncio.sleep(0)  # Let the reader acquire its execution reference.
    execution.append("stdout", "once")
    execution.finish("succeeded")
    result = await workspace.drain_output()
    assert "once" in result.content[1].text
    with pytest.raises(KernelError, match="already consumed"):
        await reader
    assert not workspace.executions


async def test_cancelled_read_keeps_unread_output(workspace):
    execution = add(workspace, "pending")
    execution.append("stdout", "keep")
    reader = asyncio.create_task(workspace.read_output("pending", wait_seconds=5))
    await asyncio.sleep(0)
    reader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reader
    assert workspace.status()["unread_output_count"] == 1
    assert "keep" in (await workspace.read_output("pending")).content[1].text


async def test_failed_response_build_does_not_partially_drain(workspace, monkeypatch):
    first = add(workspace, "first")
    first.append("stdout", "keep")
    first.finish("succeeded")
    second = add(workspace, "second", done=True)

    def fail():
        raise RuntimeError("serialization failed")

    monkeypatch.setattr(second, "to_tool_result", fail)
    with pytest.raises(RuntimeError, match="serialization failed"):
        await workspace.drain_output()
    assert set(workspace.executions) == {"first", "second"}
    assert first.outputs[0].data == "keep"
    assert not first.delivered


async def test_drain_budget_leaves_whole_results_for_subsequent_calls(
    workspace, monkeypatch
):
    import ipykernel_mcp.kernel as kernel_module

    monkeypatch.setattr(kernel_module, "MAX_DRAIN_BYTES", 3000)
    for key in ("first", "second", "third"):
        execution = add(workspace, key)
        execution.append("display", "A" * 1200, "image/png")
        execution.finish("succeeded")
    for key in ("first", "second", "third"):
        result = await workspace.drain_output()
        assert [e["execution_id"] for e in result.structured_content["executions"]] == [
            key
        ]
        assert result.content[1].data == "A" * 1200
        assert key not in workspace.executions
        assert all(e.stored_bytes == 1200 for e in workspace.executions.values())
    assert (await workspace.drain_output()).structured_content == {"executions": []}


async def test_drain_budget_accounts_for_json_escaping(workspace, monkeypatch):
    import ipykernel_mcp.kernel as kernel_module

    monkeypatch.setattr(kernel_module, "MAX_DRAIN_BYTES", 3000)
    for key in ("first", "second"):
        execution = add(workspace, key)
        execution.append("stdout", "\x00" * 200)
        execution.finish("succeeded")
    result = await workspace.drain_output()
    assert len(result.structured_content["executions"]) == 1
    assert "second" in workspace.executions


async def test_single_result_exceeding_drain_budget_is_not_consumed(
    workspace, monkeypatch
):
    import ipykernel_mcp.kernel as kernel_module

    monkeypatch.setattr(kernel_module, "MAX_DRAIN_BYTES", 1024)
    execution = add(workspace, "first", done=True)
    with pytest.raises(KernelError, match="read_output"):
        await workspace.drain_output()
    assert not execution.delivered
    assert "first" in workspace.executions
    assert (await workspace.read_output("first")).structured_content[
        "status"
    ] == "succeeded"
