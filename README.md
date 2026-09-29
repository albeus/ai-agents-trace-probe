# ai-agents-trace-probe

A small, standalone probe that tests whether one `trace_id` can tie together a model call (through LiteLLM) and MCP tool calls, and whether a deliberately induced timeout can be located from the log alone.

> **Part of the [AI Agents Lab](https://github.com/albeus/ai-agents-lab).** This is a scripted probe, not an agent and not a distributed-tracing stack. The trace ID is created and logged by the probe itself. It is **not** propagated automatically through LiteLLM or the MCP server, and the probe calls the MCP server directly rather than through the gateway.

## Safety notice

- The probe creates a slow operation and enforces a client timeout. A client timeout does not mean the server stopped its work.
- Use it only against a lab MCP server with harmless, read-only tools. Do not point it at write-capable or consequential tools.
- The lab MCP server has no authentication. Keep it on loopback or a trusted lab network.
- Trace logs can contain model names, URLs, timings and identifiers from your environment. `agent-trace.jsonl` is git-ignored; only the sanitised `agent-trace.jsonl.example` belongs in the repository.

## Goal

Answer one question: given only a trace ID, can I tell which step failed, when, how long it ran, and roughly whether the fault lay with the model call, the tool call or the client's own deadline?

The probe correlates:

- an agent-level run event;
- one model call through LiteLLM;
- MCP `tools/list` and `tools/call` requests to the lab server;
- a deliberate 3-second client timeout against a tool that sleeps for up to 10 seconds.

Out of scope: a real multi-step agent loop, a dashboard, persistent storage, and any use against production systems.

## Prerequisites

- Python 3.12 or later.
- A running LiteLLM Proxy with a model route, as in [`ai-agents-litellm-gateway`](https://github.com/albeus/ai-agents-litellm-gateway).
- The lab MCP server from [`ai-agents-mcp-itsops`](https://github.com/albeus/ai-agents-mcp-itsops), running over Streamable HTTP, with the extra `slow_probe` tool described below.
- `jq` for reading the log.

## Configuration

Supply the LiteLLM key through an environment variable or an untracked `.env` file. Do not commit it.

## Tutorial

1. **Add a harmless, controllable slow tool to the lab MCP server.** This is a change to the `mcp-itsops` server, separate from this repository:

   ```python
   import time

   @mcp.tool()
   def slow_probe(seconds: int = 10) -> str:
       """Deliberately slow lab-only probe for timeout and tracing tests."""
       if seconds < 0 or seconds > 10:
           return "Error: seconds must be between 0 and 10."
       time.sleep(seconds)
       return f"Completed after {seconds} seconds."
   ```

   Restart the MCP server so the tool is registered. Remove the tool afterwards if you do not want it left in place.

2. **What `trace_probe.py` does.** It generates one `trace_id` at the start (the format is a UTC timestamp plus a short random suffix, for example `20260914T180354Z-a55ab7`), then appends these JSONL events to `agent-trace.jsonl`, all sharing that ID:

   - `agent_run` with `outcome: "started"`.
   - `llm_call`: a call through LiteLLM with the trace ID passed as request metadata, logging model name, gateway URL, duration and token counts.
   - `tool_call` for MCP `tools/list`, a cheap and safe request.
   - `tool_call_started` and then `tool_call` for `slow_probe`, using a 3-second client timeout. The start event records an argument hash, not the raw arguments. The final event records the outcome: `ok`, `timeout` or `error`.
   - A final `agent_run` with total duration, iteration count, and an outcome of `ok` or `partial_result`.

3. **Run it:**

   ```bash
   python trace_probe.py
   ```

4. **Filter the log by trace ID:**

   ```bash
   TRACE_ID="$(tail -n 1 agent-trace.jsonl | jq -r '.trace_id')"
   jq -c --arg id "$TRACE_ID" 'select(.trace_id == $id)' agent-trace.jsonl
   ```

## Example result

A recorded run, trace `20260914T180354Z-a55ab7`, produced this sequence. The full sanitised trace is in `agent-trace.jsonl.example`.

| Event | Outcome | Detail |
| --- | --- | --- |
| `agent_run` | started | Run begins |
| `llm_call` | ok | 15,024 ms, 17 tokens in, 108 tokens out, via LiteLLM |
| `tool_call` (`tools/list`) | ok | 3 ms; the MCP endpoint was reachable |
| `tool_call` (`slow_probe`) | timeout | Ended after 3,004 ms with `timeout_ms: 3000` |
| `agent_run` | partial_result | 18,033 ms total, 1 iteration |

The MCP server's own log showed `POST /mcp` returning `200 OK` for that request. So the server did not report an error, and it probably continued the sleep on its side. That was not measured. The failure was entirely on the client: the probe's 3-second deadline expired while a healthy server was still working.

An earlier run produced an HTTP 400 on `tools/call` because of a request-format mistake in the probe. It was logged as a `tool_call` event with `outcome: "error"`, which shows the same trace can isolate a malformed request as well as a timeout.

## What this shows and does not show

| Shown | Not shown |
| --- | --- |
| One trace ID can order a model call, a tools/list call and a tools/call in a single log | Automatic cross-service tracing: LiteLLM and the MCP server do not receive or record this trace ID unless you check that separately |
| A client-side timeout is distinguishable from a server error in the log pair | That the server cancelled or finished the timed-out work |
| A `partial_result` outcome can be recorded rather than failing the whole run | A defined retry, cancellation or alerting policy |
| A malformed request is also visible in the trace | Behaviour through the LiteLLM MCP gateway: the probe calls the MCP server directly |

## Summary of the exercise

- The trace ID stood for one scripted request, from a model call through a partial tool interaction.
- It contained one model call and two MCP calls.
- `slow_probe` timed out at 3,000 ms because the client's deadline was shorter than the tool's runtime, even though the server returned `200 OK`.
- The run reported a partial result instead of retrying. A real system would need a deliberate policy for that case: retry, allow a longer budget, cancel server-side, or surface a degraded response.

## Next steps

- Make LiteLLM's own logs carry the trace ID, so model-level facts (routing, latency, tokens, gateway errors) can be joined to the probe log. They will still not capture tool selection, arguments or agent-level retry decisions.
- Emit the same trace ID from the agent, the gateway and the MCP client and server, and send it to a central log store.
- Log the MCP server's own request duration next to the client's, so a trace can show how long the server actually took.
- Add a second failure mode, such as a closed port or a genuine server-side error, to compare with the client-timeout case.
- Decide what the policy should be for a timed-out tool that may still be running, and how to monitor it.
- Optional: a simple Grafana/Loki view filtered on `trace_id`.

## Related

- [`ai-agents-litellm-gateway`](https://github.com/albeus/ai-agents-litellm-gateway): the proxy used for the model call.
- [`ai-agents-mcp-itsops`](https://github.com/albeus/ai-agents-mcp-itsops): the MCP server used for the tool calls.
- [Lab index](https://github.com/albeus/ai-agents-lab): reading order and the other projects.

## Licence

Released under the [MIT Licence](LICENSE). This is a learning lab, provided as is, without warranty.
