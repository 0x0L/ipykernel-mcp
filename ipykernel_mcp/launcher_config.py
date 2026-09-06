"""Private Jupyter CLI config, executed in the selected launcher's environment.

Only Jupyter and Python's standard library are required here. File requests keep
interrupts separate from launcher termination and work without POSIX signals.
The enclosing server owns the private directory and removes it after shutdown.
"""

import os
import signal
from pathlib import Path

from jupyter_client.kernelapp import KernelApp
from tornado.ioloop import PeriodicCallback

_runtime = Path(os.environ["IPYKERNEL_MCP_RUNTIME_DIR"])
_app = KernelApp.instance()


def _poll_requests():
    (_runtime / "ready").touch(exist_ok=True)
    if (_runtime / "shutdown").exists():
        _relay.stop()
        _app.shutdown(signal.SIGTERM)
    elif not _app.km.is_alive():
        _relay.stop()
        _app.loop.stop()
    elif (_runtime / "interrupt").exists():
        _app.km.interrupt_kernel()
        (_runtime / "interrupt").unlink()


_relay = PeriodicCallback(_poll_requests, 100)
_relay.start()
