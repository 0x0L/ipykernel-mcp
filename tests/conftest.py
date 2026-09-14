"""Keep real-kernel tests independent of the user's registered Python kernel."""

import json
import os
import sys

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_python_kernelspec(tmp_path_factory):
    data = tmp_path_factory.mktemp("jupyter-data")
    spec = data / "kernels" / "python3"
    spec.mkdir(parents=True)
    (spec / "kernel.json").write_text(
        json.dumps(
            {
                "argv": [
                    sys.executable,
                    "-m",
                    "ipykernel_launcher",
                    "-f",
                    "{connection_file}",
                ],
                "display_name": "Test Python",
                "language": "python",
            }
        )
    )
    with pytest.MonkeyPatch.context() as patch:
        previous = os.environ.get("JUPYTER_PATH")
        patch.setenv(
            "JUPYTER_PATH", str(data) + (os.pathsep + previous if previous else "")
        )
        yield


def execution_metadata(result):
    """Read the public content-only execution header, including on the MCP wire."""
    assert result.structured_content is None
    block = result.content[0]
    assert block.type == "text"
    assert block.text.startswith("[metadata]\n")
    return parse_metadata(block.text)


def parse_metadata(text):
    data = json.loads(text.removeprefix("[metadata]\n"))
    assert set(data) == {"execution_id", "status", "truncated", "error"}
    assert data["status"] in {"running", "succeeded", "failed", "cancelled"}
    assert isinstance(data["truncated"], bool)
    assert data["error"] is None or set(data["error"]) == {"type", "message"}
    return data


def drained_metadata(result):
    assert result.structured_content is None
    if (
        len(result.content) == 1
        and result.content[0].type == "text"
        and result.content[0].text == "No pending output or outcomes."
    ):
        return []
    execution_metadata(result)
    return [
        parse_metadata(block.text)
        for block in result.content
        if block.type == "text" and block.text.startswith("[metadata]\n")
    ]
