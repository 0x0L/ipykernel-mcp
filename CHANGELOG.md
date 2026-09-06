# Changelog

## Unreleased

- Require `--jupyter` and `--kernel`; discover and launch through the selected
  Jupyter CLI, independent of the MCP server environment. Report the launcher
  path in status and retain it across reset. Relay lifecycle requests to Jupyter.

- Use one configured persistent Jupyter kernel with six tools: `execute`,
  `read_output`, `drain_output`, `interrupt`, `reset`, and `status`.
- Consume returned output, retain unfinished work across cancelled waits, and
  expose explicit completion, failure, cancellation, and recovery metadata.
- Return text and PNG/JPEG images with bounded unread buffers and completed-result
  retention. Merge consecutive stream messages before applying the block limit.
- Fall back to available JPEG or text representations when a PNG does not fit.
  Report omitted images as truncated output.
- Limit drain responses to an 8 MiB JSON budget, preserving excess whole execution
  results for later calls.
- Provide portable client configuration examples; ignore local active configs.

### Compatibility

The six-tool interface replaces the earlier `kernel_*` tools. `--jupyter` and `--kernel` replace
`--python`; choose a Jupyter executable and a kernel visible to it. Existing
`--kernel` configurations must add `--jupyter`. Server `--project`,
environment discovery, explicit start/stop tools, `timeout`/`msg_id` aliases, and
replay cursors are removed. Returned results cannot be replayed. See the
[README](README.md) for the current contract and migration notes.
