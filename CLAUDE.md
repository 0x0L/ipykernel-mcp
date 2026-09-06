# Repository guidance

## Purpose and documentation

Help an MCP agent work incrementally: retain loaded data, reuse functions and
intermediate results, inspect local images, and refine plots across calls. The
README introduces these workflows; `docs/usage.md` contains runnable examples.
Keep examples aligned with the six-tool contract and identify optional libraries
that must be installed in the configured kernel environment. Image interpretation
depends on the client's support for image tool results and the model's vision
capabilities. Distinguish session state from retained output and saved files.

## Product contract

One configured Python interpreter, one persistent Jupyter kernel, one active execution.
`--python` is required; `--cwd` defaults to the server's working directory. The server
opens Python in its lifespan and closes it on shutdown. Configuration cannot change
through MCP tools. There is no environment discovery, kernel-spec selection, `.venv`
convention, or public start/stop operation.

The six tools are `execute`, `read_output`, `drain_output`, `interrupt`, `reset`,
and `status`.
Keep names aligned between the README, MCP tools, and Python methods. `reset` clears
the Jupyter kernel. Returned output is consumed, including execute responses.

## Commands

```bash
uv sync --locked --dev
uv run ruff format --check
uv run ruff check
uv run ty check
uv run pytest tests/ -v
uv run pre-commit install
```

## Architecture

- `server.py`: six MCP tools, required interpreter configuration, lifespan ownership.
- `schemas.py`: validated response models shared with MCP output schemas.
- `interpreter.py`: immutable `KernelConfig`, interpreter preflight, and the ad-hoc
  Jupyter launch adapter. Do not resolve executable symlinks: they select venvs.
- `kernel.py`: `Kernel` owns the process, readers, lifecycle lock, and retained
  executions. `open`/`close` belong to the server lifespan; `reset` replaces the process
  using the same configuration, including after a crash.
- `execution.py`: `Execution` handles Jupyter messages and its bounded unread
  `OutputBlock` list, consumption, terminal outcome, and MCP result rendering.

## Invariants

Normal completion requires shell execute_reply plus IOPub idle. Map protocol
outcomes to `running`, `succeeded`, `failed`, or `cancelled`. Error metadata is
`{type, message}`. Never expose protocol status names as new public outcomes.

Reset/shutdown/death/reader failure finish active executions and wake waiters.
Cancelling a tool wait does not cancel code; status exposes the execution ID.
No implicit retry or crash recovery. Stdin is disabled. Lifecycle operations
serialize, but output waits never hold the lifecycle lock. Cleanup must finish
under both asyncio and AnyIO cancellation.

Reads consume all returned text and images. Completed records are removed once
their final outcome is returned, including silent completions. No cursors or replay.
Build responses before committing consumption, with no await between those steps.
Concurrent consumers must never receive the same output or final outcome twice;
a waiter whose final outcome was consumed elsewhere receives a tool error.
`drain_output` groups pending output by execution and includes silent completed
outcomes and truncation notices. It keeps active records and leaves future output
unread. `status` counts unread text/image blocks globally and per execution, excluding
metadata and internal Jupyter messages. Consuming output resets byte/block budgets
and the truncation flag; it does not release Python variables in the kernel.
Ignore clear_output; append update_display_data to its own execution. Do not track
display IDs or mutate older output. Messages arriving after completion are ignored.
Bound unread buffers and expire/evict unread completed records. Reset preserves
unread results; closing the server clears them.

## Tests

Use isolated `Kernel` fixtures and `create_server(kernel)`. Message reducer tests
exercise ordering, consumption, limits, and errors. Real-kernel tests use the test Python
executable explicitly; fault injection covers cancellation and connection failures.
The stdio smoke test checks the actual CLI and transport. CI uses locked tools on
Python 3.12–3.14. Keep README examples and tool schemas synchronized.

## FastMCP conventions

Use the FastMCP 4 public imports (`from fastmcp.tools import ToolResult`) and SDK v2
snake_case Python fields. `mcp.types` remains the supported protocol-type import.
Tools publish output schemas and explicit behavior annotations; inputs use strict
schema validation. Translate only intentional `KernelError` messages to `ToolError`;
unexpected failures are masked by FastMCP. Retain code exceptions as execution outcomes.
Keep the FastMCP version pinned and review its migration guide when updating.
CI and pre-commit must use the lockfile's tool versions. Tests treat FastMCP
deprecation warnings as errors. Use `structured_content` for assertions on wire data,
since the client's `data` can be a schema-derived model.
