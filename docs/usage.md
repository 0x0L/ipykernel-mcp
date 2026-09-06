# Practical examples

These examples show code an agent can submit to the `execute` tool. Each Python
block is a separate call; run the blocks within each example in order. Variables
remain available between calls in the same server session. The MCP entry in the
example configurations is named `jupyter-python`.
Configure required `--jupyter /path/to/env/bin/jupyter` and `--kernel python3`
arguments before connecting. Discovery and launch use that executable; no shell
activation is needed. The selected kernelspec determines the code environment.
`status().jupyter` reports the fixed launcher path; reset reuses it.

These examples require a Python kernel. For R, Julia, or another configured
language, use its syntax and display facilities with the same six MCP tools.
Keep follow-up calls on the same server: variables and execution IDs belong to
that server only. Separate kernels can exchange data through saved files.

## Choose the next tool

The kernel starts with the server. No separate start call is needed. Send Python
source as the `code` argument of `execute`, without Markdown fences. Tool calls
such as `read_output` are MCP requests, not functions to execute inside the kernel.

| Situation | Next action |
|---|---|
| Calculate, load data, or reuse a variable | Call `execute` with code. |
| `execute` or `read_output` returns `running` | Call `read_output` with that execution ID and a positive `wait_seconds`, such as 10. Do not resubmit the code. |
| A final outcome is returned | Use its output; the execution record is now removed. |
| Collect all pending results | Repeat `drain_output` until `executions` is empty; it also returns silent completions. |
| Find active work or pending results | Call `status`; it does not consume output. |
| Stop work while keeping variables | Call `interrupt`, then read the targeted execution's eventual outcome. |
| A code exception occurs | Inspect the error and fix the code; earlier variable/file changes may remain. |
| Kernel state is `unavailable` | Inspect the error, then use `reset` to recover with a fresh kernel. |

After a lost or cancelled tool response, inspect `status` before deciding what to
do next. The code may still be running or may already have produced side effects.
Consumed output cannot be replayed. Use one output consumer at a time, choosing
either individual reads or a drain. A zero unread count excludes neither running
work nor silent completed outcomes; inspect execution statuses too.

`status().cwd` reports the configured initial directory, not a live directory
lookup. If needed, execute `import os; print(os.getcwd())` to inspect the current
directory. `reset` restores the initial directory and clears in-memory state; it
does not undo file writes or consume old pending results.

## Compute once, ask follow-up questions

Define a function in one call:

```python
def fibonacci(n):
    if n < 0:
        raise ValueError("n must be non-negative")
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


fibonacci(20)
```

The result is `6765`, using F₀ = 0 and F₁ = 1. A later call can reuse the function:

```python
fib_values = [fibonacci(n) for n in range(21)]
fib_values[-5:]
```

The result is `[987, 1597, 2584, 4181, 6765]`. Both the function and the list remain
available for further calculations or a plot.

## Explore data across a conversation

This self-contained example uses only standard-library modules:

```python
import csv
import io
from collections import defaultdict
from decimal import Decimal

sales_csv = """region,revenue
North,120.50
South,90.00
North,80.25
South,150.00
"""
sales = list(csv.DictReader(io.StringIO(sales_csv)))
totals = defaultdict(Decimal)
for row in sales:
    totals[row["region"]] += Decimal(row["revenue"])
dict(totals)
```

This returns North: `200.75` and South: `240.00`. To use a real file, replace the
`sales` assignment with `with open("sales.csv", newline="") as f:` followed by
`sales = list(csv.DictReader(f))` inside that block. Relative paths start from the
configured working directory unless executed code changes it.

A follow-up question can use the existing totals:

```python
leader = max(totals, key=totals.get)
share = totals[leader] / sum(totals.values()) * 100
f"{leader}: {share:.1f}% of revenue"
```

The result is `'South: 54.5% of revenue'`. The original records remain available
for a different grouping or calculation. For larger datasets, use pandas or other
analysis libraries if they are installed in the configured interpreter.

## Show a local image to the agent

Replace the path below with a PNG or JPEG accessible to the kernel:

```python
from IPython.display import Image, display

with open("/absolute/path/to/image.png", "rb") as f:
    image_bytes = f.read()
display(Image(data=image_bytes))
```

The tool returns image content, allowing a model with vision support to inspect
the picture when the client forwards image results. This is useful for screenshots,
photographs, and saved charts. Reading bytes alone does not display an image;
`display(Image(...))` produces the rich output.

`IPython.display` is provided by IPython, a dependency of ipykernel; it is not part
of Python's standard library. PNG and JPEG are image formats. HTML and interactive
widgets and audio are not rendered by this server. If an image exceeds the unread
buffer budget, the server tries another supported representation from the same
display bundle, then text; it does not resize or convert images. Save or resize
large images in the kernel before displaying them when necessary.

## Generate and refine a plot

This example reuses `fib_values` from the first example and requires Matplotlib
in the configured kernel environment. It is included in this repository's development
dependencies (`uv sync --locked --dev`), but is not a runtime dependency of the
server. For another kernel environment, install it there before running the example:

```bash
uv pip install --python /path/to/project/.venv/bin/python matplotlib
```

Submit this code to `execute`:

```python
import matplotlib.pyplot as plt
from IPython.display import display

fig, ax = plt.subplots(figsize=(8, 4))
ax.plot(range(21), fib_values, marker="o")
ax.set(title="Fibonacci numbers", xlabel="n", ylabel="F(n)")
ax.grid(alpha=0.2)
fig.tight_layout()
display(fig)
plt.close(fig)
```

The chart is returned as an image. In another call, reuse the figure to inspect
growth on a logarithmic scale and save the result:

```python
ax.set_yscale("symlog", linthresh=1)
ax.set_title("Fibonacci numbers — symmetric log scale")
fig.tight_layout()
display(fig)
fig.savefig("fibonacci.png", dpi=150)
```

Closing the figure removes it from pyplot's active figures; the `fig` reference
still permits editing, display, and saving. The saved PNG remains on disk after
the kernel session ends.

## Keep track of longer work

Submit code with `wait_seconds=0` to return without waiting for completion. These
are MCP tool-call examples, not Python functions to run inside the kernel:

```text
execute(code="import time\nfor step in range(3):\n    time.sleep(1)\n    print(f'Step {step + 1}/3', flush=True)\nresult = 42", wait_seconds=0)
```

When the response says `running`, copy its `execution_id` into
the next call:

```text
read_output(execution_id="<returned ID>", wait_seconds=10)
```

Repeat while the execution is running. Each response consumes its returned output;
subsequent reads return only new output. The completed record is removed when its
final outcome is returned. After success,
`execute(code="result")` returns `42`. A wait timeout does not stop computation,
and only one execution runs at a time. If you lose track of an active call, use
`status()` to find its ID. `interrupt()` asks it to stop while preserving the
kernel; `reset()` clears the kernel and starts fresh.

## Inspect and clear pending output

Call `status()` to see the global `unread_output_count` and the count for each
pending execution. These count text/image blocks after adjacent messages from the
same stream are merged, excluding internal Jupyter messages.
Use `drain_output()` to retrieve a batch of unread output, grouped by execution,
plus completed outcomes even for code that printed nothing. The drain frees returned
payloads and removes completed records. Running code keeps going; its future output
is available to the next call. Responses have an 8 MiB JSON budget and include
whole execution results; excess results remain unread. Repeat until `executions`
is empty to finish draining available results. An empty drain returns `{"executions": []}`.

Output returned directly by `execute` is consumed too. Reads cannot be replayed,
so use one consumer per execution. If another call consumes a final outcome while
your read waits, your read receives a tool error. Draining output leaves functions,
variables, and datasets in the Python kernel intact.

In-memory Python state lasts until reset, server shutdown, or a kernel failure.
Unread completed results expire 10 minutes after completion; at most 16 executions
are tracked. Consumed completed records are removed immediately. Expiry and
consumption do not delete Python variables. Save large outputs to files rather than
relying on bounded tool buffers. See [recovery and limits](../README.md#recovery-and-limits)
for the full contract.
