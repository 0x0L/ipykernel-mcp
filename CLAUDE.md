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

Single module server (`ipykernel_mcp/server.py`) using FastMCP's async lifespan pattern. The server enforces exactly one kernel at a time via module-level globals (`_kernel_manager`, `_kernel_client`, `_project_dir`, `_executions`, `_reader_task`).

**Tools exposed via MCP:** `kernel_start`, `kernel_execute`, `kernel_get_output`, `kernel_status`, `kernel_stop`, `kernel_restart`, `kernel_interrupt`

**Key design decisions:**
- `kernel_start` locates the `.venv/bin/python` in a given project directory and creates a kernel spec pointing to it — this is how the kernel runs in the project's environment
- A background `_iopub_reader` task (started after `wait_for_ready`) continuously routes iopub messages into `ExecutionRecord` objects keyed by `msg_id` in `_executions`. This ensures output is never lost, even when `kernel_execute` times out
- `kernel_execute` waits on `ExecutionRecord.done_event`; on timeout it returns partial output with a `[pending]` block containing the `msg_id`. `kernel_get_output` retrieves the remaining output
- `kernel_execute` returns structured MCP `ToolResult` content blocks (stdout, stderr, images, results, errors as separate tagged blocks) rather than plain text
- `clear_output` iopub messages are handled (both immediate and deferred/`wait=True`) to prevent tqdm-style progress bars from accumulating
- ANSI escape codes are stripped from tracebacks before returning to the LLM
- Image extraction from MIME bundles supports PNG and JPEG, returned as MCP `ImageContent`
- The `_iopub_reader` is cancelled before `restart_kernel`/`_cleanup` and restarted after `wait_for_ready` to avoid message contention
- Cleanup runs via FastMCP lifespan on shutdown

## Testing

Tests use `pytest-asyncio` with `asyncio_mode = "auto"`. The `clean_kernel_state` autouse fixture ensures no kernel leaks between tests. Tests use the repo's own `.venv` as the target project (requires `ipykernel` in dev deps).

Error-path tests (no kernel needed) are fast. Happy-path tests start a real kernel and are slower.

## CI

GitHub Actions runs on Python 3.12, 3.13, 3.14: format check → lint → type check → tests.
