import pytest
from mcp.types import ImageContent, TextContent

from ipykernel_mcp.execution import Execution


def text(execution):
    return "\n".join(
        b.text
        for b in execution.to_tool_result().content
        if b.type == "text" and not b.text.startswith("[execution]")
    )


def display(value, display_id=None):
    return {"data": {"text/plain": value}, "transient": {"display_id": display_id}}


def test_output_order_and_adjacent_stream_coalescing():
    r = Execution("id")
    for kind, value in [
        ("stdout", "a"),
        ("stdout", "b"),
        ("stderr", "c"),
        ("stdout", "d"),
    ]:
        r.handle_message("stream", {"name": kind, "text": value})
    assert text(r) == "[stdout]\nab\n[stderr]\nc\n[stdout]\nd"


def test_plain_displays_and_expression_are_preserved():
    r = Execution("id")
    r.handle_message("display_data", display("first"))
    r.handle_message("display_data", display("second"))
    r.handle_message("execute_result", display("third"))
    assert text(r) == "[display]\nfirst\n[display]\nsecond\n[result]\nthird"


def test_prefer_one_image_representation():
    r = Execution("id")
    r.handle_message(
        "display_data",
        {
            "data": {
                "image/png": "cG5n",
                "image/jpeg": "anBlZw==",
                "text/plain": "image",
            }
        },
    )
    images = [b for b in r.to_tool_result().content if isinstance(b, ImageContent)]
    assert len(images) == 1
    assert images[0].mime_type == "image/png"


def test_consumption_releases_payloads_and_replenishes_limits():
    r = Execution("id", max_text_bytes=6)
    r.append("stdout", "before overflow")
    r.append("display", "cG5n", "image/png")
    result = r.to_tool_result()
    assert result.structured_content is not None
    assert result.structured_content["truncated"]
    r.consume()
    assert not r.outputs
    assert r.stored_bytes == r.text_bytes == 0
    assert not r.truncated
    assert not r.delivered
    r.append("stdout", "after")
    assert text(r) == "[stdout]\nafter"
    assert not r.truncated
    assert isinstance(result.content[1], TextContent)
    assert "before" in result.content[1].text
    r.finish("succeeded")
    r.consume()
    assert r.delivered


@pytest.mark.parametrize("wait", [False, True])
def test_clear_preserves_unread_output(wait):
    execution = Execution("id")
    execution.append("stdout", "before")
    execution.handle_message("execute_result", display("value"))
    execution.append("display", "cG5n", "image/png")
    original = execution.to_tool_result()
    size = execution.stored_bytes
    count = len(execution.outputs)
    execution.handle_message("clear_output", {"wait": wait})
    assert execution.to_tool_result() == original
    assert len(execution.outputs) == count
    assert execution.stored_bytes == size
    execution.append("stdout", "after")
    assert "after" in text(execution)
    assert execution.outputs[0].data == "before"


def test_clear_does_not_discard_output_before_error():
    execution = Execution("id")
    execution.append("stdout", "before")
    execution.handle_message("clear_output", {"wait": True})
    execution.handle_message(
        "error", {"ename": "ValueError", "evalue": "bad", "traceback": []}
    )
    assert "before" in text(execution)
    assert "ValueError" in text(execution)


def test_display_update_appends_without_modifying_previous_output():
    execution = Execution("id")
    execution.handle_message("display_data", display("old", "handle"))
    execution.append("stdout", "between")
    original_blocks = tuple(execution.outputs)
    original_result = execution.to_tool_result()
    count = len(execution.outputs)
    execution.handle_message("update_display_data", display("new", "handle"))
    assert tuple(execution.outputs[:count]) == original_blocks
    assert isinstance(original_result.content[0], TextContent)
    assert "new" not in original_result.content[0].text
    assert text(execution).endswith("[display]\nnew")
    assert text(execution) == "[display]\nold\n[stdout]\nbetween\n[display]\nnew"
    assert execution.to_tool_result() == execution.to_tool_result()


@pytest.mark.parametrize("display_id", [None, "unknown"])
def test_update_does_not_require_a_previous_display(display_id):
    execution = Execution("id")
    execution.handle_message("update_display_data", display("new", display_id))
    assert text(execution) == "[display]\nnew"


def test_image_update_appends_a_new_image():
    execution = Execution("id")
    for msg_type, payload in (
        ("display_data", "b2xk"),
        ("update_display_data", "bmV3"),
    ):
        execution.handle_message(
            msg_type,
            {"data": {"image/png": payload}, "transient": {"display_id": "handle"}},
        )
    assert [block.data for block in execution.outputs] == ["b2xk", "bmV3"]
    assert execution.to_tool_result().content[-1].type == "image"


def test_clear_and_update_cannot_bypass_output_limits():
    execution = Execution("id", max_text_bytes=5)
    execution.handle_message("display_data", display("first", "handle"))
    for wait in (False, True):
        execution.handle_message("clear_output", {"wait": wait})
        execution.handle_message("update_display_data", display("later", "handle"))
    assert text(execution) == "[display]\nfirst"
    assert execution.truncated
    assert len(execution.outputs) == 1
    assert execution.text_bytes == 5


@pytest.mark.parametrize(
    "message", ["stream", "display_data", "update_display_data", "clear_output"]
)
def test_completed_output_is_immutable(message):
    execution = Execution("id")
    execution.handle_message("display_data", display("original", "handle"))
    execution.finish("succeeded")
    original = execution.to_tool_result()
    execution.handle_message(
        message,
        {**display("late", "handle"), "name": "stdout", "text": "late", "wait": False},
    )
    assert execution.to_tool_result() == original


def test_text_limit_and_unicode_boundary():
    r = Execution("id", max_text_bytes=5)
    r.append("stdout", "ééé")
    assert text(r) == "[stdout]\néé"
    assert r.truncated
    assert r.text_bytes <= 5


def test_image_limit_never_returns_corrupt_image():
    r = Execution("id", max_bytes=3)
    r.append("display", "cG5n", "image/png")
    assert not r.outputs
    assert r.truncated


def test_event_count_limit():
    r = Execution("id", max_output_blocks=2)
    for _ in range(20):
        r.append("stdout", "a")
    assert len(r.outputs) == 2
    assert r.truncated


def test_completion_needs_reply_and_idle_in_either_order():
    for first in ("execute_reply", "status"):
        r = Execution("id")
        messages = {
            "execute_reply": {"status": "ok"},
            "status": {"execution_state": "idle"},
        }
        r.handle_message(first, messages.pop(first))
        assert not r.done
        second, content = messages.popitem()
        r.handle_message(second, content)
        assert r.status == "succeeded"
        assert r.done_event.is_set()


def test_aborted_reply_is_never_success():
    r = Execution("id")
    r.handle_message("execute_reply", {"status": "aborted"})
    assert r.status == "failed"
    assert r.error is not None


def test_shell_only_error_and_ansi_cleanup():
    r = Execution("id")
    r.handle_message(
        "execute_reply",
        {
            "status": "error",
            "ename": "ValueError",
            "evalue": "bad",
            "traceback": ["\x1b[31mbad\x1b[0m"],
        },
    )
    r.handle_message("status", {"execution_state": "idle"})
    assert r.status == "failed"
    assert "\x1b" not in text(r)


def test_terminal_outcome_cannot_be_overwritten():
    r = Execution("id")
    r.finish("cancelled", "ServerClosed", "Server closed")
    r.handle_message("execute_reply", {"status": "ok"})
    r.handle_message("status", {"execution_state": "idle"})
    assert r.status == "cancelled"


def test_shell_and_iopub_error_are_not_duplicated():
    for first, second in (("error", "execute_reply"), ("execute_reply", "error")):
        r = Execution("id")
        content = {
            "status": "error",
            "ename": "ValueError",
            "evalue": "bad",
            "traceback": [],
        }
        r.handle_message(first, content)
        r.handle_message(second, content)
        r.handle_message("status", {"execution_state": "idle"})
        assert r.status == "failed"
        assert len([event for event in r.outputs if event.kind == "error"]) == 1


def test_early_shell_error_does_not_reorder_iopub_output():
    r = Execution("id")
    content = {
        "status": "error",
        "ename": "ValueError",
        "evalue": "bad",
        "traceback": [],
    }
    r.handle_message("execute_reply", content)
    r.handle_message("stream", {"name": "stdout", "text": "before"})
    r.handle_message("error", content)
    r.handle_message("status", {"execution_state": "idle"})
    assert [event.kind for event in r.outputs] == ["stdout", "error"]
    assert r.status == "failed"
