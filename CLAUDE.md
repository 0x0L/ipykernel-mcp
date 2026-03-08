# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ipykernel-mcp is an MCP (Model Context Protocol) server that manages IPython kernels, allowing LLMs to execute Python code within a project's virtual environment. Built with FastMCP and jupyter-client.

## Commands

```bash
uv sync --dev                    # Install all dependencies
uv run pytest tests/ -v          # Run all tests
uv run pytest tests/ -v -k test_name  # Run a single test
uv run ruff format --check       # Check formatting
uv run ruff check                # Lint
uv run ty check                  # Type check
uv run pre-commit install        # Install git hooks (ruff format, ruff check, ty check)
```

## Architecture

Single module server (`ipykernel_mcp/server.py`) using FastMCP's async lifespan pattern. The `KernelSession` class encapsulates all kernel state and lifecycle (manager, client, cwd, executions, reader task). A module-level `_session` singleton is used by thin `@mcp.tool` wrappers. Tests import `_cleanup` (alias for `_session.stop`) and `_executions` (same dict object as `_session.executions`).

**File layout:** imports → constants/dataclasses → pure helpers → `KernelSession` class → singleton + aliases → FastMCP lifespan/server → `@mcp.tool` wrappers → `main()`

**Tools exposed via MCP:** `kernel_discover`, `kernel_start`, `kernel_execute`, `kernel_get_output`, `kernel_status`, `kernel_stop`, `kernel_restart`, `kernel_interrupt`

**Key design decisions:**
- `kernel_discover` lists registered Jupyter kernel specs (via `KernelSpecManager`) and optionally scans a directory for a `.venv` with ipykernel. Returns structured entries with a `name` field used by `kernel_start`
- `kernel_start(kernel_name)` accepts either a registered spec name (e.g. `"python3"`) or a venv reference (`"venv:/path/to/project"`). For registered specs, it delegates to `AsyncKernelManager(kernel_name=...)`. For venv references, it creates an ad-hoc `KernelSpec` pointing to `.venv/bin/python`
- A background iopub reader task (started after `wait_for_ready`) continuously routes iopub messages into `ExecutionRecord` objects keyed by `msg_id`. This ensures output is never lost, even when `kernel_execute` times out. Reader lifecycle is managed by `_start_reader()` / `_cancel_reader()` methods, eliminating duplicated cancel logic
- `kernel_execute` waits on `ExecutionRecord.done_event`; on timeout it returns partial output with a `[pending]` block containing the `msg_id`. `kernel_get_output` retrieves the remaining output
- `kernel_execute` returns structured MCP `ToolResult` content blocks (stdout, stderr, images, results, errors as separate tagged blocks) rather than plain text
- `clear_output` iopub messages are handled (both immediate and deferred/`wait=True`) to prevent tqdm-style progress bars from accumulating
- ANSI escape codes are stripped from tracebacks before returning to the LLM
- Image extraction from MIME bundles supports PNG and JPEG, returned as MCP `ImageContent`
- Cleanup runs via FastMCP lifespan on shutdown

## Testing

Tests use `pytest-asyncio` with `asyncio_mode = "auto"`. The `clean_kernel_state` autouse fixture ensures no kernel leaks between tests. Tests use the repo's own `.venv` as the target project (requires `ipykernel` in dev deps).

Error-path tests (no kernel needed) are fast. Happy-path tests start a real kernel and are slower.

## CI

GitHub Actions runs on Python 3.12, 3.13, 3.14: format check → lint → type check → tests.
