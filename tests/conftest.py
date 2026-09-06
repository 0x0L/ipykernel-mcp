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
