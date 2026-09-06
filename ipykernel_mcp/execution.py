"""Bounded unread execution output, released when returned to the client."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from fastmcp.tools import ToolResult
from mcp.types import ImageContent, TextContent

from .schemas import ExecutionMetadata, ExecutionStatus

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_OUTPUT_BLOCKS = 1000


@dataclass(frozen=True)
class OutputBlock:
    kind: str
    data: str
    mime: str | None = None


@dataclass
class Execution:
    execution_id: str
    status: ExecutionStatus = "running"
    outputs: list[OutputBlock] = field(default_factory=list)
    truncated: bool = False
    idle: bool = False
    reply_status: str | None = None
    reply_error: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    finished_at: float | None = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    max_bytes: int = MAX_OUTPUT_BYTES
    max_text_bytes: int = MAX_TEXT_BYTES
    max_output_blocks: int = MAX_OUTPUT_BLOCKS
    stored_bytes: int = 0
    text_bytes: int = 0

    delivered: bool = False

    def consume(self) -> None:
        """Release returned payloads and replenish the unread-output budget."""
        self.outputs.clear()
        self.stored_bytes = 0
        self.text_bytes = 0
        self.truncated = False
        if self.done:
            self.delivered = True

    @property
    def done(self) -> bool:
        return self.status != "running"

    def finish(
        self,
        status: ExecutionStatus,
        error_type: str | None = None,
        message: str | None = None,
    ) -> None:
        if self.done:
            return
        self.status = status
        if message:
            self.error = {"type": error_type, "message": message}
        self.finished_at = time.monotonic()
        self.done_event.set()

    def append(
        self,
        kind: str,
        data: str,
        mime: str | None = None,
    ) -> None:
        remaining = self.max_bytes - self.stored_bytes
        if not mime:
            remaining = min(remaining, self.max_text_bytes - self.text_bytes)
        encoded = data.encode()
        if len(self.outputs) >= self.max_output_blocks or remaining <= 0:
            self.truncated = True
            return
        if len(encoded) > remaining:
            self.truncated = True
            if mime:
                return  # Never emit an incomplete base64 image.
            data = encoded[:remaining].decode(errors="ignore")
        if not data:
            return
        size = len(data.encode())
        self.outputs.append(OutputBlock(kind, data, mime))
        self.stored_bytes += size
        if not mime:
            self.text_bytes += size

    def _display(self, content: dict, kind: str) -> None:
        data = content.get("data", {})
        for mime in ("image/png", "image/jpeg"):
            if mime in data:
                self.append(kind, data[mime], mime)
                return
        if "text/plain" in data:
            self.append(kind, data["text/plain"])
        elif data:
            self.append(
                "display",
                "Unsupported display formats: " + ", ".join(data),
            )

    def handle_message(self, msg_type: str, content: dict) -> None:
        # Notebook presentation commands cannot erase an agent's transcript.
        if self.done or msg_type == "clear_output":
            return
        if msg_type == "stream":
            self.append(content["name"], content["text"])
        elif msg_type in ("display_data", "execute_result", "update_display_data"):
            self._display(
                content, "result" if msg_type == "execute_result" else "display"
            )
        elif msg_type == "error":
            self.set_error(content)
        elif msg_type == "status" and content.get("execution_state") == "idle":
            self.idle = True
        elif msg_type == "execute_reply":
            self.reply_status = content["status"]
            if self.reply_status == "error" and self.error is None:
                # Channels can arrive out of order. Defer a shell-only fallback
                # until idle so it cannot precede earlier IOPub stdout/displays.
                self.reply_error = {
                    k: str(content.get(k, ""))[:4096] for k in ("ename", "evalue")
                }
                self.reply_error["traceback"] = [
                    "\n".join(content.get("traceback", []))[: self.max_text_bytes]
                ]
            if self.reply_status in ("aborted", "abort"):
                self.finish(
                    "failed",
                    "ExecutionAborted",
                    "The kernel aborted this execution before it ran.",
                )
        if self.idle and self.reply_status is not None:
            if self.error is None and self.reply_error is not None:
                self.set_error(self.reply_error)
            self.reply_error = None
            status = "succeeded" if self.reply_status == "ok" else "failed"
            if self.error and self.error.get("type") == "KeyboardInterrupt":
                status = "cancelled"
            self.finish(status)

    def set_error(self, content: dict) -> None:
        # Shell and IOPub may report the same exception in either arrival order.
        if self.error is not None:
            return
        # Bound metadata as well as rendered tracebacks.
        self.error = {
            "type": str(content.get("ename", ""))[:4096],
            "message": str(content.get("evalue", ""))[:4096],
        }
        tb = ANSI_ESCAPE.sub("", "\n".join(content.get("traceback", [])))
        self.append("error", f"{self.error['type']}: {self.error['message']}\n{tb}")

    def to_tool_result(self) -> ToolResult:
        """Build a response before its owner commits consumption."""
        blocks: list[TextContent | ImageContent] = []
        last_kind: str | None = None
        for output in self.outputs:
            if output.mime:
                blocks.append(
                    ImageContent(type="image", data=output.data, mime_type=output.mime)
                )
            elif (
                output.kind in ("stdout", "stderr")
                and last_kind == output.kind
                and blocks
                and isinstance(blocks[-1], TextContent)
            ):
                blocks[-1].text += output.data
            else:
                blocks.append(
                    TextContent(type="text", text=f"[{output.kind}]\n{output.data}")
                )
            last_kind = output.kind if not output.mime else None
        metadata = ExecutionMetadata.model_validate(
            {
                "execution_id": self.execution_id,
                "status": self.status,
                "truncated": self.truncated,
                "error": self.error,
            }
        ).model_dump()
        # Text-only MCP clients receive the execution outcome too.
        summary = (
            f"[execution]\nexecution_id: {self.execution_id}\nstatus: {self.status}"
        )
        if self.truncated:
            summary += "\nOutput truncated by server limits."
        if self.error:
            summary += f"\n{self.error['type']}: {self.error['message']}"
        blocks.insert(0, TextContent(type="text", text=summary))
        return ToolResult(content=blocks, structured_content=metadata)
