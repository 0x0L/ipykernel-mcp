# ipykernel-mcp

A persistent Jupyter kernel for an MCP client. Give your agent a place to compute,
keep intermediate results, and return images as it works through a task. Configure
an installed Jupyter kernel once; variables, imports, and functions survive between calls.

## Why a persistent kernel?

Complex tasks often involve a conversation with the data: load it, inspect it,
try a calculation, then refine the question. The kernel keeps that working state
available across tool calls, so the agent can build on each result.

| Task | What the kernel makes possible |
|---|---|
| Explore data | Load a CSV once, then filter, aggregate, and compare the same records across follow-up questions. |
| Check calculations | Execute an algorithm, inspect the result, and reuse the function with new inputs. |
| Inspect images | Read a local PNG or JPEG and return its image content to a client that supports image tool results. |
| Create plots | Use installed plotting libraries to render a chart, then refine it using data already in memory. |
| Run longer computations | Start work, collect output with `read_output`, and use the results when it finishes. |

For example, ask your agent:

> Load this CSV in the Python kernel and summarize revenue by region. Then compare
> the two largest regions using the same data.

Or:

> Read this PNG through the kernel and show me the image.

The kernel executes code and returns results; the connected model interprets them.
Available libraries and file access come from the configured kernel environment.
State lasts for the server session: resetting the kernel or restarting the server
clears variables. Save results to files when they need to outlive the session.

See the [practical examples](docs/usage.md) for successive computations, CSV analysis,
image display, plotting, and polling long-running work.
See [contributing](CONTRIBUTING.md) for development and release checks, and the
[changelog](CHANGELOG.md) for unreleased changes.

For tool selection and recovery, start with the
[agent workflow guide](docs/usage.md#choose-the-next-tool). The same essential
guidance is published directly in MCP server instructions, tool descriptions,
and input/output field descriptions so clients receive it during discovery.

## Configuration

Both `--jupyter` and `--kernel` are required. Pass an absolute path to the
Jupyter executable and a kernel name visible to that installation. `--cwd` sets
the initial working directory and defaults to the server's working directory.
Configure one MCP server entry per Jupyter/kernel combination.

You need `uv`, an MCP client that launches a local stdio server, and a Jupyter
installation with `jupyter-client` and the desired language kernel. For Python:

```bash
uv pip install --python /path/to/project/.venv/bin/python jupyter-client ipykernel
/path/to/project/.venv/bin/jupyter kernelspec list
```

Configure that executable directly; shell activation is unnecessary:

```json
{
  "mcpServers": {
    "jupyter-python": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/0x0L/ipykernel-mcp",
        "ipykernel-mcp",
        "--jupyter", "/path/to/project/.venv/bin/jupyter",
        "--kernel", "python3",
        "--cwd", "/path/to/project"
      ]
    }
  }
}
```

The server runs the selected executable's `kernelspec list --json` for discovery
and `kernel --kernel NAME` for launch, then connects using Jupyter's connection
file. Its own Python environment does not discover or launch language kernels.
The selected Jupyter installation does not need this MCP package installed.
Missing executables or kernels fail startup with a diagnostic.

A *kernelspec* is Jupyter's launch recipe: its executable and environment determine
where code runs. It may point to an environment different from the Jupyter
installation. To register a separate Python environment explicitly:

```bash
/path/to/other/.venv/bin/python -m ipykernel install --user --name project-python --display-name "Project Python"
/path/to/project/.venv/bin/jupyter kernelspec list
```

Then select `--kernel project-python`. Install analysis libraries in the kernel's
environment. For R or Julia, install and register IRkernel or IJulia, then use the
name reported by the configured Jupyter executable. Each server entry owns an
isolated kernel process and state, even when entries use the same language.
Variables and execution IDs cannot cross entries; files can be shared on disk.

The server starts the launcher and kernel on connection and closes both on
shutdown. A private Jupyter config relays interrupt and shutdown requests to
Jupyter's kernel manager. Interrupts respect the kernelspec's signal/message mode;
kernel death is reported without automatic restart. Reset reuses the same
executable, kernel name, and initial directory. These settings are fixed for the
server's lifetime and cannot be changed through tools.

Use absolute paths in MCP configuration. Relative paths are interpreted from the
server's working directory. The launcher inherits the server's environment
variables, including Jupyter path overrides; no shell activation script is run.
This server does not sandbox executed code: it can access files, environment
variables, subprocesses, and the network wherever the kernel process can.
Use a suitably isolated environment when working with untrusted code or data.

After connecting, ask: “Use jupyter-python to set `values = [10, 20, 30]`, then
compute their mean in a second call.” The result should be `20.0`. The server
starts its kernel automatically; there is no notebook to create or start tool to call.

## Six tools

| Tool | Meaning |
|---|---|
| `execute(code, wait_seconds=10)` | Run source in the persistent Jupyter kernel |
| `read_output(execution_id, wait_seconds=0)` | Return and consume one execution's unread output and outcome |
| `drain_output()` | Return and consume a bounded batch of pending output and outcomes |
| `interrupt()` | Ask the currently running code to stop, preserving kernel state |
| `reset()` | Start a fresh kernel using the same Jupyter executable, kernelspec, and initial working directory |
| `status()` | Inspect the kernel, pending execution IDs, and unread output counts |

The following code examples use Python. Use the selected language's syntax for
other kernels; the six MCP tools and polling workflow are the same.

```text
execute(code="values = [10, 20, 30]")
execute(code="sum(values) / len(values)")
```

The second call returns `20.0`. Only one execution can run at a time. A busy error
includes the active execution ID; read its output or interrupt it before submitting
more code. Interactive stdin is disabled; provide inputs directly in code.

## Waiting and consuming output

`wait_seconds` controls how long a tool waits for completion, **not how long code
may run**. It accepts 0–60 seconds. When the wait ends, the response contains the
currently unread output and metadata:

```json
{
  "execution_id": "...",
  "status": "running",
  "truncated": false,
  "error": null
}
```

If `status` is `running`, call:

```text
read_output(execution_id="<returned ID>", wait_seconds=10)
```

Every `execute` or `read_output` response consumes the output it returns. A later
read returns only new output; there are no cursors or replay. Returning the final
outcome removes the completed execution record, including executions with no output.
Reading that ID again produces a tool error. Use one consumer per execution; if
another reader consumes the final outcome while a call waits, that call errors.
Cancelling a wait before a response is built leaves its unread output available.
Output is released when the server builds the response, without a client receipt
acknowledgement; a response lost in transit cannot be replayed.

### Discover and drain pending output

`status()` reports `unread_output_count` globally and for each pending execution:

```json
{
  "state": "ready",
  "jupyter": "/path/to/project/.venv/bin/jupyter",
  "kernel_name": "project-python",
  "cwd": "/path/to/project",
  "active_execution_id": null,
  "unread_output_count": 2,
  "executions": [
    {"execution_id": "...", "status": "succeeded", "unread_output_count": 2}
  ],
  "error": null
}
```

Counts measure buffered text/image blocks after consecutive messages from the same
stream are merged. They exclude internal Jupyter messages and execution metadata. A
completed execution may have zero blocks and still have an undelivered outcome.

Use `drain_output()` to immediately retrieve a batch of pending output and completed
outcomes. Each response has an 8 MiB JSON budget and contains whole execution
results; excess results remain unread. Repeat until `executions` is empty to
finish draining currently available results. Content is grouped by execution,
with an `[execution]` metadata header
before each group's text and images. Structured metadata contains an `executions`
list of outcomes. A drain consumes what it returns and removes completed records.
Running executions remain tracked; output arriving afterward is available for the
next read. Running executions with neither unread output nor a truncation notice
are omitted. An empty drain returns `{"executions": []}`.

Display clears are ignored; display updates append output to the execution that
produced them. Consuming output frees the server's payload buffers and replenishes
their capacity. It does **not** delete variables or datasets in the Python kernel.

Execution outcomes are:

| Status | Meaning |
|---|---|
| `running` | Code has not finished |
| `succeeded` | Code completed normally |
| `failed` | Code raised an exception, or execution could not complete |
| `cancelled` | Kernel reset, server shutdown, or a reported `KeyboardInterrupt` |

Other kernels may report an interruption as `failed`; inspect the error details.
An interrupt request alone does not determine the execution outcome.

Failures and cancellations include `error: {type, message}`. Tracebacks appear in
the readable output. An exception from executed code is an execution outcome;
invalid tool arguments, a busy kernel, and expired IDs are MCP tool errors.

## Recovery and limits

- Cancelling a tool call stops waiting; it does not stop code. Use `status()` to
  find the execution, then `read_output` or `interrupt`.
- `interrupt()` sends a signal that code may catch or defer. Read the execution to
  see its eventual outcome. `reset()` replaces the kernel and clears all variables.
- A crashed kernel or failed output connection makes the kernel unavailable and
  resolves active waits. Call `reset()` to recover. Code is never silently rerun.
- `status()` reports kernel `state`, configured `jupyter`, `kernel_name`, and `cwd`,
  `active_execution_id`, pending `executions`, global and per-execution
  `unread_output_count`, and any kernel `error`. Normal
  states are `ready` and `busy`; `resetting` and `unavailable` describe recovery.
- Output supports plain text and PNG/JPEG images, including display updates as new
  output. If PNG does not fit, available JPEG and plain-text representations in
  the same display bundle are tried next; the server does not convert images.
  An omitted image sets `truncated=true`, with an explanatory notice when space
  permits. HTML/widgets and audio are not rendered. All output arriving after its execution
  has completed is ignored, including late display updates.
- Each execution buffers at most **64 KiB of unread text, 4 MiB of unread payload,
  and 1,000 unread output blocks**. Exceeding a limit drops excess output and sets
  `truncated=true`; code continues. A read clears that flag and replenishes the
  buffer budget. Save large results to files when the full data is needed.
- Unread completed results expire after **10 minutes**; at most **16 executions**
  are tracked. Oldest completed results are evicted first when capacity is needed.
  Delivered results are removed immediately. Reset preserves unread results; server
  shutdown removes them.

## Development

### Use this checkout in an agent

Copy the [Claude Code example](examples/mcp.json) to `.mcp.json`, or merge the
[Codex example](examples/codex-config.toml) into `.codex/config.toml`.
Both local configuration files are ignored by Git. Replace the example absolute
paths with your checkout and desired working directory. Both examples run:

```bash
uv run --project /absolute/path/to/ipykernel-mcp --locked --dev ipykernel-mcp \
  --jupyter /absolute/path/to/ipykernel-mcp/.venv/bin/jupyter \
  --kernel python3 \
  --cwd /absolute/path/to/project
```

`--dev` includes `ipykernel` and Matplotlib in the project's environment.
Select its `.venv/bin/jupyter` and `python3` kernel to use those libraries, or
point `--jupyter` at another installation. An explicitly registered kernelspec
can still select a different interpreter.
When upgrading an existing local config, add `"--jupyter", "/absolute/path/to/ipykernel-mcp/.venv/bin/jupyter"`
to the server's `args` in `.mcp.json` or `.codex/config.toml`, alongside
`"--kernel", "python3"`. Keep any existing per-tool approval settings.
After restarting the MCP connection, call `status()` and check that `jupyter`
reports your chosen executable and `kernel_name` reports `python3`.

Restart the MCP connection after changing source code or launch arguments. Claude Code may ask to
approve the project server; Codex loads project configuration for trusted projects.

[`uv run --project`](https://docs.astral.sh/uv/reference/cli/#uv-run) selects a local
project directory and runs its editable installation using `uv.lock`.
`uv run file:///path/to/project` does not launch a project package.
To use the checkout from another project's MCP configuration, keep `--project`
pointing here and change `--jupyter`, `--kernel`, and `--cwd` for that project's installed kernel
and working directory.

### Checks

```bash
uv sync --locked --dev
uv run ipykernel-mcp --jupyter "$PWD/.venv/bin/jupyter" --kernel python3 --cwd /path/to/project
uv run ruff format --check
uv run ruff check
uv run ty check
uv run pytest tests/ -v
uv build
```

`server.py` defines the MCP tools and CLI. `schemas.py` defines their validated
response contracts. `kernel.py` owns the Jupyter kernel process and
execution lifecycle. `execution.py` collects output and outcomes. `interpreter.py`
validates the Jupyter executable and manages CLI discovery, launch, and connection.
`launcher_config.py` relays lifecycle requests inside the selected Jupyter process.
Tests cover real Python kernels, output ordering, cancellation, recovery, and stdio.
R and Julia execution are not covered by the automated suite; display and interrupt
behavior depend on the installed kernel implementation.
CI runs Python 3.12–3.14 on Linux, builds the source distribution and wheel, and
smoke-tests the installed wheel's CLI. Windows and macOS are not covered by CI.

FastMCP is pinned to **4.0.3**; all dependencies and development tools are resolved
in `uv.lock`. CI and pre-commit use those locked tools. To update dependencies,
review the FastMCP release pin, run `uv lock --upgrade` and `uv sync --locked --dev`,
then run the checks above. FastMCP deprecation warnings fail the test suite.

Each tool publishes an output schema and MCP behavior annotations. `status` is read-only;
`read_output` and `drain_output` consume output and are non-idempotent mutations.
Execution, interruption, and reset can change the kernel and trigger arbitrary
code side effects. Inputs are validated against
the schema without coercion. Expected kernel errors are actionable MCP tool errors;
unexpected server exceptions are logged and masked. Execution exceptions remain
normal structured outcomes. For FastMCP Python clients, use `structured_content`
for the JSON dictionary; `data` may be a decoded model.

These conventions follow FastMCP's [tool documentation](https://gofastmcp.com/servers/tools),
[lifespan guidance](https://gofastmcp.com/servers/lifespan), and
[versioning guidance](https://gofastmcp.com/getting-started/installation).
The stdio transport and the existing polling contract require no task extension.

This API replaces the previous `kernel_*` tools. Configure required `--jupyter` and `--kernel`, plus optional
`--cwd`; use the six tools above. The server's `--python` option has been replaced
by `--jupyter` and `--kernel`: select the desired Jupyter installation and a
kernelspec visible to it. Existing configurations must add `--jupyter`.
`--project`, discovery, explicit
start/stop tools, and the `timeout`/`msg_id` aliases have been removed.

Consuming reads replace the previous replayable transcript API. `cursor` and
`next_cursor` have been removed. Clients should read each response once, continue
polling by execution ID while running, and use `drain_output()` for all pending
output. A completed ID disappears as soon as its final outcome is returned.
