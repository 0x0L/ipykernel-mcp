# Contributing

For bug reports, include the server version or commit, operating system, Python
version, configured Jupyter executable, kernelspec language, MCP client, and a minimal reproduction. Include the
expected behavior, actual execution metadata, and relevant logs. Remove credentials,
private file contents, and other sensitive data before sharing a report.

Discuss changes to the six-tool contract before implementing a large feature.
Keep pull requests focused and explain the user-visible behavior and validation.

## Local development

Use Python 3.12 or later and uv:

```bash
uv sync --locked --dev
uv run pre-commit install
uv run --locked ruff format --check
uv run --locked ruff check
uv run --locked ty check
uv run --locked pytest tests/ -v
uv build
```

Tests register an isolated Python kernelspec pointing to the test environment;
they do not need or alter your user kernelspec registration. Tests execute real
code and start kernel subprocesses. Linux is covered by CI on Python 3.12–3.14;
Windows, macOS, R, and Julia are not covered by that matrix.

For manual MCP testing, copy the [client examples](examples/) and configure the
kernel environment as described in the [README](README.md#configuration).
Local `.mcp.json` and `.codex/config.toml` files are ignored. Restart the MCP
connection after editing server code.

## Use this checkout in an agent

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

## Changes and dependencies

Add regression tests for changed behavior. Keep the README and usage examples focused on human setup and workflows.
Document tool selection, output consumption, polling, and recovery in MCP server
instructions, tool descriptions, and response field descriptions; agents receive
these through discovery. Verify that metadata through a real MCP client.
Architecture and lifecycle invariants are documented in [AGENTS.md](AGENTS.md).

Commit `uv.lock` with dependency changes. FastMCP is deliberately pinned; review
its migration guidance before updating. CI and pre-commit use locked development
tools. Build dependencies and the uv executable are not pinned by `uv.lock`, so
this setup does not promise byte-for-byte reproducible distributions.
GitHub Actions are pinned to commit SHAs, with Dependabot proposing updates.

## Before a release

1. Finish the checks above and require a passing GitHub Actions run for the exact
   commit being released.
2. Update the version in `pyproject.toml`.
3. Build the source distribution and wheel. Inspect their contents for required
   documentation/license files and accidental local configuration or private data.
4. Smoke-test the wheel in an isolated environment with `uv run --isolated
   --no-project --with /absolute/path/to/package.whl ipykernel-mcp --help`, then
   verify an MCP connection with an explicit Jupyter executable and kernel.
5. Tag the validated commit and describe changes and compatibility limitations
   in its GitHub release notes. Publishing is a separate maintainer action;
   the CI workflow does not publish packages or releases.

Repository settings such as required status checks, branch protection, secret
scanning, and private vulnerability reporting must be configured on GitHub;
they are not enabled by these files. If private vulnerability reporting is
available on the repository's Security tab, use it for sensitive security reports.
