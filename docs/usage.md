# Example conversations

After [configuring the server](../README.md#configuration), ask your agent to use
the kernel in ordinary language. These examples assume a Python kernel; adapt
the request to your selected language when using R or Julia.

## Explore a dataset

> Load sales.csv from my project folder. Show the number of rows, the columns,
> and any missing values. Then summarize revenue by region.

Follow up with:

> Using the same data, compare monthly revenue for the two largest regions.

The loaded data remains available throughout the session, so you can refine the
analysis without loading it again. Ask the agent to save a cleaned dataset or
report when you want to keep it after the session ends.

Python's standard library can handle basic CSV analysis. For larger datasets,
install pandas in your kernel environment:

```bash
uv pip install --python /path/to/project/.venv/bin/python pandas
```

## Calculate and reuse results

> Write a Fibonacci function in the Python kernel and calculate the first 21
> values, starting with zero.

Then:

> Plot those values, then show the same plot with a logarithmic scale.

The function and calculated values remain available for follow-up requests.
Plotting requires a library such as Matplotlib in the selected environment:

```bash
uv pip install --python /path/to/project/.venv/bin/python matplotlib
```

## Inspect an image

> Open /absolute/path/to/chart.png through the kernel and describe what the
> chart shows.

The kernel must be able to read the file. Your client must support image results,
and the model must support vision to interpret them. PNG and JPEG are supported;
interactive HTML widgets and audio are not rendered.

## Refine and save a chart

> Plot monthly revenue as a line chart, with dates on the horizontal axis and
> euros on the vertical axis.

Then:

> Make the labels larger, add a title, and save the chart as revenue.png in my
> project folder.

Saved images stay on disk after the kernel session ends. You can ask the agent
to inspect the saved image and adjust it further.

## Run longer work

> Run this simulation with 100,000 samples. Show progress as it runs, then
> summarize the result.

The agent handles waiting and collecting results. To stop the calculation:

> Stop the simulation and keep the data we already loaded.

To clear the session entirely:

> Restart the kernel so we can start fresh.

Restarting clears in-memory data, variables, and functions. It does not delete
saved files. If you have several configured kernels, name the one you want the
agent to use; each has separate state.
