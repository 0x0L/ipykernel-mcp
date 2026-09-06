# ipykernel-mcp

Give your AI agent a persistent Jupyter kernel for calculations, data analysis,
and plots. Data, variables, and functions stay available as you ask follow-up
questions, so the agent can build on its previous work.

For example:

> Load sales.csv and summarize revenue by region.

> Now plot the monthly trend for the two largest regions using the same data.

You choose the Jupyter installation and kernel. No shell activation or notebook
is needed, and the MCP server can run in a separate environment from your analysis
libraries. Python, R, and Julia kernels can be selected; automated tests cover Python.

## Configuration

You need an MCP-compatible client and a Jupyter installation with the language
kernel you want to use.

The MCP server requires Python 3.12 or later. This requirement is independent of
the selected kernel's language and interpreter version.

The examples use [uv](https://docs.astral.sh/uv/) for convenience. It is optional:
`uvx` handles installing and running the server in an isolated environment, so
you do not need to install the server manually. You can also install the server
with pip or pipx and run `ipykernel-mcp` directly.

### Prepare a Python environment

If you do not already have an environment, create one:

```bash
uv venv /path/to/project/.venv
```

Install Jupyter's kernel launcher and the Python kernel there, then list the
available kernels:

```bash
uv pip install --python /path/to/project/.venv/bin/python jupyter-client ipykernel
/path/to/project/.venv/bin/jupyter kernelspec list
```

Install any analysis libraries you need in that environment too. For example:

```bash
uv pip install --python /path/to/project/.venv/bin/python pandas matplotlib
```

### Connect your MCP client

Add this server entry to your client's MCP configuration, replacing the paths
with absolute paths on your machine:

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

| Argument | What to choose |
|---|---|
| `--jupyter` (required) | The executable from the Jupyter installation you want to use. |
| `--kernel` (required) | A kernel name reported by that executable's `kernelspec list` command. |
| `--cwd` (optional) | The folder where the kernel starts and resolves relative file paths. Defaults to the server's working directory. |

The MCP server does not need to be installed in your Jupyter environment.
Restart the MCP connection after changing the configuration. If you are upgrading
an existing configuration, add `--jupyter` alongside `--kernel`.

To run this repository's checkout, see the [local development setup](CONTRIBUTING.md#use-this-checkout-in-an-agent)
and the [client configuration examples](examples/).

### Choose another environment or language

You can use any kernel listed by your selected Jupyter installation:

```bash
/path/to/project/.venv/bin/jupyter kernelspec list
```

Set `--kernel` to a name from that list. Each kernel determines its own language,
interpreter, and libraries, which can belong to a different environment from
the Jupyter installation.

Add separate MCP server entries to make several environments available to your
agent. Each gets its own kernel and independent in-memory state.

## Using it

Once connected, try:

> Use the Python kernel to calculate the mean of 10, 20, and 30.

The answer should be `20.0`. The kernel starts automatically when the client
connects. You can then ask for data exploration, calculations, image inspection,
or charts in ordinary language. See [example conversations](docs/usage.md).

Data stays in memory for the session. Restarting the kernel clears
that state; saved files remain on disk. Ask the agent to save important results.
You can also ask it to stop a computation or start over with a fresh kernel.

Plots and local PNG/JPEG images can be returned to clients that support image
results. Image interpretation also requires a model with vision support.
Interactive HTML widgets and audio are not rendered.

### Connect another tool to the same kernel

Ask the agent for the kernel's connection file path, exposed by `status()` as
`connection_file`. Use it in a local tool that supports Jupyter connection files,
for example with Jupyter Console installed:

```bash
jupyter console --existing /full/path/to/connection.json
```

Both clients share variables and imports. Restarting the kernel produces a new
connection file; stopping the MCP server shuts the kernel down. The path is
`null` when the kernel is not ready or busy. The MCP server tracks only its own
executions, so coordinate work between clients.

## Troubleshooting

- **The server will not start:** check that `--jupyter` points to an executable
  file and run that exact executable with `kernelspec list` to verify the kernel
  name. Check your client's MCP logs for the startup error.
- **An import fails:** install the missing library in the selected kernel's
  environment. Ask the agent to report its Python executable if you are unsure
  which environment is running.
- **Files cannot be found:** use absolute paths or set `--cwd` to the folder
  containing your data.
- **The kernel crashes or becomes unresponsive:** ask the agent to restart it.
  In-memory work will be lost, so save important results as you go.
- **Large results are incomplete:** ask the agent to save the full result to a
  file and show a summary or sample in the conversation.

The kernel runs with your local user's permissions and can access files,
subprocesses, and the network. Use an appropriately isolated environment for
untrusted code or data.

For development and release checks, see [Contributing](CONTRIBUTING.md).
