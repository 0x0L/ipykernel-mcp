# ipykernel-mcp

An [MCP](https://modelcontextprotocol.io/) server that manages an IPython kernel, allowing LLMs to execute Python code in a project's virtual environment.

Built with [FastMCP](https://github.com/PrefectHQ/fastmcp) and [jupyter-client](https://github.com/jupyter/jupyter_client).

## Tools

| Tool | Description |
|------|-------------|
| `kernel_start(project_dir)` | Start a kernel using the `.venv` from the given project directory |
| `kernel_execute(code, timeout)` | Execute code and return tagged output blocks (`[stdout]`, `[stderr]`, `[result]`, `[error]`, images). On timeout, returns partial output + `[pending]` with a `msg_id` |
| `kernel_get_output(msg_id, timeout)` | Retrieve remaining output for a timed-out execution. Auto-cleans up once complete |
| `kernel_interrupt()` | Send SIGINT to cancel a long-running execution without losing kernel state |
| `kernel_restart()` | Restart the kernel, clearing all variables and state |
| `kernel_stop()` | Stop the kernel and clean up resources |
| `kernel_status()` | Return kernel status: running, alive, project_dir, connection_file, pending_executions, ports |

## Installation

```bash
uv sync
```

The project directory passed to `kernel_start` must contain a `.venv` with `ipykernel` installed.

## Usage

Run the MCP server:

```bash
ipykernel-mcp
```

Or add it to your MCP client configuration:

```json
{
  "mcpServers": {
    "ipykernel-mcp": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/0x0L/ipykernel-mcp", "ipykernel-mcp"]
    }
  }
}
```

## Monitoring

[jupyter_watch](https://github.com/0x0L/jupyter_watch) can monitor kernel output (stdout, stderr, display data) in real-time in a browser:

```bash
npx github:0x0L/jupyter_watch <connection_file>
```

The connection file path is printed by `kernel_start` when the kernel starts.

## Development

```bash
uv sync --dev
uv run pytest tests/ -v
```

Pre-commit hooks are configured to run formatting, linting, and type checking:

```bash
uv run pre-commit install
```
