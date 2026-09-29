import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone

import requests

TRACE_ID = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
LOG_PATH = "agent-trace.jsonl"

LITELLM_URL = "http://127.0.0.1:4000/v1/chat/completions"
MCP_URL = "http://127.0.0.1:8000/mcp"
MODEL = "qwen3-local"
MCP_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {
        "name": "trace-probe",
        "version": "0.1",
    },
}


def log(event, **fields):
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "trace_id": TRACE_ID,
        "event": event,
        **fields,
    }
    with open(LOG_PATH, "a") as file:
        file.write(json.dumps(record) + "\n")


def mcp_request(method, params, timeout):
    started = time.monotonic()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": method,
    }
    if "name" in params:
        headers["mcp-name"] = params["name"]

    try:
        response = requests.post(
            MCP_URL,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": {"_meta": MCP_META, **params},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        result = response.json()
        log(
            "tool_call",
            mcp_server="itsops",
            tool=method,
            duration_ms=round((time.monotonic() - started) * 1000),
            outcome="ok",
        )
        return result
    except requests.Timeout:
        log(
            "tool_call",
            mcp_server="itsops",
            tool=method,
            duration_ms=round((time.monotonic() - started) * 1000),
            outcome="timeout",
            timeout_ms=round(timeout * 1000),
        )
        return None
    except requests.RequestException as error:
        body = None
        if getattr(error, "response", None) is not None:
            body = error.response.text[:500]
        log(
                "tool_call",
                mcp_server="itsops",
                tool=method,
                duration_ms=round((time.monotonic() - started) * 1000),
                outcome="error",
                error=str(error),
                response_body=body,
                )
        return None


def main():
    run_started = time.monotonic()
    log("agent_run", task="trace controlled model and MCP timeout", outcome="started")

    model_started = time.monotonic()
    response = requests.post(
        LITELLM_URL,
        headers={
            "Authorization": f"Bearer {os.environ['ITSOPS_SERVICE_KEY']}",
            "Content-Type": "application/json",
            "x-litellm-trace-id": TRACE_ID,
        },
        json={
            "model": MODEL,
            "messages": [
                {"role": "user", "content": "Reply with exactly: model decision recorded"}
            ],
            "temperature": 0,
            "metadata": {"trace_id": TRACE_ID},
        },
        timeout=30,
    )
    response.raise_for_status()
    usage = response.json().get("usage", {})
    log(
        "llm_call",
        model=MODEL,
        gateway=LITELLM_URL,
        duration_ms=round((time.monotonic() - model_started) * 1000),
        tokens_in=usage.get("prompt_tokens"),
        tokens_out=usage.get("completion_tokens"),
        outcome="ok",
    )

    tools = mcp_request("tools/list", {}, timeout=3)
    assert tools is not None, "Tool discovery unexpectedly failed"

    slow_probe_args = {"name": "slow_probe", "arguments": {"seconds": 10}}
    log(
        "tool_call_started",
        mcp_server="itsops",
        tool="slow_probe",
        args_hash=hashlib.sha256(
            json.dumps(slow_probe_args, sort_keys=True).encode()
        ).hexdigest()[:12],
    )
    result = mcp_request("tools/call", slow_probe_args, timeout=3)

    log(
        "agent_run",
        task="trace controlled model and MCP timeout",
        duration_ms=round((time.monotonic() - run_started) * 1000),
        iterations=1,
        outcome="partial_result" if result is None else "ok",
    )


if __name__ == "__main__":
    main()
