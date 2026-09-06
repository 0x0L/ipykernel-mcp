import asyncio
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from ipykernel_mcp.interpreter import KernelConfig
from ipykernel_mcp.kernel import Kernel
from ipykernel_mcp.server import create_server

PROJECT = str(Path(__file__).resolve().parent.parent)


def metadata(result):
    assert result.structured_content is not None
    return result.structured_content


def configured_kernel():
    return Kernel(KernelConfig.from_paths("python3", PROJECT))


async def test_six_tools_with_required_arguments_and_no_aliases():
    async with Client(create_server(configured_kernel())) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert set(tools) == {
            "execute",
            "read_output",
            "drain_output",
            "interrupt",
            "reset",
            "status",
        }
        assert set(tools["execute"].input_schema["properties"]) == {
            "code",
            "wait_seconds",
        }
        assert tools["read_output"].input_schema["required"] == ["execution_id"]
        assert set(tools["read_output"].input_schema["properties"]) == {
            "execution_id",
            "wait_seconds",
        }
        for name in ("interrupt", "reset", "status", "drain_output"):
            assert not tools[name].input_schema["properties"]


async def test_server_owns_startup_and_shutdown():
    kernel = configured_kernel()
    assert kernel.manager is None
    async with Client(create_server(kernel)) as client:
        state = metadata(await client.call_tool("status", {}))
        assert state["state"] == "ready"
        assert state["kernel_name"] == "python3"
        assert state["cwd"] == PROJECT
        assert "running" not in state
        result = await client.call_tool("execute", {"code": "42"})
        assert metadata(result)["status"] == "succeeded"
        assert metadata(result)["execution_id"]
        result = await client.call_tool("execute", {"code": "1/0"})
        assert not result.is_error
        assert metadata(result)["status"] == "failed"
        assert metadata(result)["error"]["type"] == "ZeroDivisionError"
        with pytest.raises(ToolError, match="Unknown, consumed, or expired"):
            await client.call_tool("read_output", {"execution_id": "bogus"})
    assert kernel.manager is None
    assert not kernel.executions


async def test_servers_are_isolated():
    async with (
        Client(create_server(configured_kernel())) as first,
        Client(create_server(configured_kernel())) as second,
    ):
        await first.call_tool("execute", {"code": "answer = 42"})
        result = await second.call_tool("execute", {"code": "answer"})
        assert metadata(result)["status"] == "failed"
        assert metadata(result)["error"]["type"] == "NameError"


async def test_stdio_explicit_interpreter_smoke():
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=sys.executable,
        args=[
            "-m",
            "ipykernel_mcp.server",
            "--kernel",
            "python3",
            "--cwd",
            PROJECT,
        ],
        cwd=PROJECT,
    )
    async with Client(transport, timeout=30) as client:
        assert metadata(await client.call_tool("status", {}))["state"] == "ready"
        result = await client.call_tool("execute", {"code": "6 * 7"})
        assert metadata(result)["status"] == "succeeded"
        assert any(
            b.type == "text" and "[result]\n42" in b.text for b in result.content
        )


@pytest.mark.parametrize("args", [[], ["--project", PROJECT]])
async def test_cli_rejects_missing_or_invalid_interpreter(args):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ipykernel_mcp.server",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
    assert process.returncode == 2
    assert not stdout
    assert b"error:" in stderr


async def test_tool_annotations_and_output_schemas():
    async with Client(create_server(configured_kernel())) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        for name, tool in tools.items():
            annotations = tool.annotations
            assert annotations is not None
            read_only = name == "status"
            assert annotations.read_only_hint is read_only
            assert annotations.destructive_hint is not read_only
            assert annotations.idempotent_hint is read_only
            assert annotations.open_world_hint is (
                name in ("execute", "interrupt", "reset")
            )
            assert tool.output_schema is not None
            assert tool.output_schema["type"] == "object"
            assert tool.output_schema["required"]
        schema = tools["execute"].output_schema
        assert schema == tools["read_output"].output_schema
        assert schema["properties"]["status"]["enum"] == [
            "running",
            "succeeded",
            "failed",
            "cancelled",
        ]
        assert set(schema["required"]) == {
            "execution_id",
            "status",
            "truncated",
            "error",
        }
        assert tools["execute"].input_schema["properties"]["wait_seconds"][
            "description"
        ]


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("execute", {"code": "42", "wait_seconds": "1"}),
        ("execute", {"code": "42", "wait_seconds": True}),
        ("execute", {"code": "42", "timeout": 1}),
        ("execute", {"code": ""}),
        ("read_output", {"execution_id": "id", "cursor": 0}),
        ("read_output", {"execution_id": "id", "wait_seconds": True}),
        ("read_output", {"execution_id": ""}),
    ],
)
async def test_strict_validation_rejects_invalid_arguments_before_execution(
    tool, arguments
):
    kernel = configured_kernel()
    async with Client(create_server(kernel)) as client:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
        assert result.is_error
        assert not kernel.executions


async def test_unexpected_errors_are_masked_but_expected_errors_are_actionable(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    kernel = configured_kernel()
    async with Client(create_server(kernel)) as client:
        missing = await client.call_tool(
            "read_output", {"execution_id": "missing"}, raise_on_error=False
        )
        assert missing.is_error
        assert "Unknown, consumed, or expired" in missing.content[0].text
        monkeypatch.setattr(
            kernel,
            "execute",
            AsyncMock(side_effect=RuntimeError("internal diagnostic detail")),
        )
        failure = await client.call_tool(
            "execute", {"code": "42"}, raise_on_error=False
        )
        assert failure.is_error
        assert "internal diagnostic detail" not in failure.content[0].text


async def test_drain_wire_schema_images_silent_outcomes_and_counts():
    from ipykernel_mcp.execution import Execution

    kernel = configured_kernel()
    async with Client(create_server(kernel)) as client:
        image = Execution("image")
        image.append("display", "cG5n", "image/png")
        image.finish("succeeded")
        silent = Execution("silent")
        silent.finish("succeeded")
        kernel.executions.update(image=image, silent=silent)
        state = metadata(await client.call_tool("status", {}))
        assert state["unread_output_count"] == 1
        assert [e["unread_output_count"] for e in state["executions"]] == [1, 0]
        result = await client.call_tool("drain_output", {})
        assert [e["execution_id"] for e in metadata(result)["executions"]] == [
            "image",
            "silent",
        ]
        assert any(b.type == "image" and b.data == "cG5n" for b in result.content)
        assert not kernel.executions
        assert metadata(await client.call_tool("drain_output", {})) == {
            "executions": []
        }


async def test_discovery_publishes_documented_fields_and_executable_examples():
    """Check the metadata a model actually receives, including nested schemas."""
    import json
    import re

    from ipykernel_mcp.server import INSTRUCTIONS

    def check_fields(schema):
        if isinstance(schema, dict):
            for name, field in schema.get("properties", {}).items():
                assert field.get("description"), f"Missing field guidance: {name}"
            for value in schema.values():
                check_fields(value)
        elif isinstance(schema, list):
            for value in schema:
                check_fields(value)

    async with Client(create_server(configured_kernel())) as client:
        assert client.instructions == INSTRUCTIONS
        tools = {tool.name: tool for tool in await client.list_tools()}
        for tool in tools.values():
            assert tool.description and tool.title
            check_fields(tool.input_schema)
            check_fields(tool.output_schema)
        description = tools["execute"].description
        assert description is not None
        examples = re.findall(r"(?:Example|Follow-up): (\{[^\n]+\})", description)
        assert len(examples) == 2
        for arguments, expected in zip(examples, ("60", "20.0"), strict=True):
            result = await client.call_tool("execute", json.loads(arguments))
            assert metadata(result)["status"] == "succeeded"
            assert any(
                block.type == "text" and block.text == f"[result]\n{expected}"
                for block in result.content
            )
        state = metadata(await client.call_tool("status", {}))
        assert state["executions"] == []
        assert state["unread_output_count"] == 0
