# ipykernel-mcp

An [MCP](https://modelcontextprotocol.io/) server that manages an IPython kernel, allowing LLMs to execute Python code in a project's virtual environment.

Built with [FastMCP](https://github.com/PrefectHQ/fastmcp) and [jupyter-client](https://github.com/jupyter/jupyter_client).

## Tools

| Tool | Description |
|------|-------------|
| `start_kernel(project_dir)` | Start a kernel using the `.venv` from the given project directory |
| `stop_kernel()` | Stop the running kernel and clean up resources |
| `restart_kernel()` | Restart the kernel (clears state, preserves connection) |
| `status()` | Return kernel status (running, alive, ports, etc.) |
| `execute(code, timeout=30)` | Execute Python code and return stdout, stderr, result, and errors |

## Installation

```bash
uv sync
```

The project directory passed to `start_kernel` must contain a `.venv` with `ipykernel` installed.

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

The connection file path is printed by `start_kernel` when the kernel starts.

## Development

```bash
uv sync --dev
uv run pytest tests/ -v
```

Pre-commit hooks are configured to run formatting, linting, and type checking:

```bash
uv run pre-commit install
```
