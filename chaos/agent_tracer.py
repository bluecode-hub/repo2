"""
agent_tracer.py - local, self-hosted replacement for LangSmith.

Captures the same kind of data LangSmith would (chain/LLM/tool start-end, latency,
inputs/outputs, token usage, errors) via LangChain's standard callback interface, and
writes it to a local JSONL file instead of sending anything to an external service.

Why not just use LangSmith: it ships traces to smith.langchain.com, which is an external
service. The hackathon rules say the solution "must run entirely inside the team's own
local or sandbox environment" and list "anything outside the team's own sandbox" as out
of scope. This gets your mentor's actual ask -- visibility into agent behavior -- without
that risk, and without a live demo depending on an internet connection.

Usage:
    from agent_tracer import LocalTracer
    tracer = LocalTracer()
    result = graph.invoke(input, config={"callbacks": [tracer]})

    # or for a single LLM/chain call:
    llm.invoke(prompt, config={"callbacks": [tracer]})

Output (./data/agent_traces.jsonl): one JSON line per event, read by dashboard_server.py.
"""
import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

TRACE_FILE = Path("data") / "agent_traces.jsonl"
MAX_FIELD_CHARS = 2000  # keep the dashboard/trace file readable; full prompts can be huge


def _truncate(value: Any) -> Any:
    s = str(value)
    return s if len(s) <= MAX_FIELD_CHARS else s[:MAX_FIELD_CHARS] + f"... [{len(s)} chars total]"


class LocalTracer(BaseCallbackHandler):
    """One instance per invocation is fine, or reuse one across a whole session --
    run_id disambiguates concurrent/nested calls either way."""

    def __init__(self, out_path: Path = TRACE_FILE):
        self.out_path = out_path
        self.out_path.parent.mkdir(exist_ok=True)
        self._starts: dict[UUID, float] = {}

    def _write(self, event: dict):
        event.setdefault("trace_id", str(uuid.uuid4())[:8])
        with self.out_path.open("a") as f:
            f.write(json.dumps(event) + "\n")

    def _elapsed_ms(self, run_id: UUID) -> Optional[float]:
        t0 = self._starts.pop(run_id, None)
        return round((time.time() - t0) * 1000, 1) if t0 is not None else None

    # ---------------- chains (includes LangGraph nodes/graphs) ----------------
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None,
                        metadata=None, **kwargs):
        self._starts[run_id] = time.time()
        name = (metadata or {}).get("langgraph_node") or (serialized or {}).get("name") or "chain"
        self._write({
            "ts": time.time(), "event": "chain_start", "run_id": str(run_id),
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "name": name, "input": _truncate(inputs),
        })

    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
        self._write({
            "ts": time.time(), "event": "chain_end", "run_id": str(run_id),
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "duration_ms": self._elapsed_ms(run_id), "output": _truncate(outputs),
            "status": "ok",
        })

    def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._write({
            "ts": time.time(), "event": "chain_end", "run_id": str(run_id),
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "duration_ms": self._elapsed_ms(run_id), "status": "error",
            "error": _truncate(error),
        })

    # ---------------- LLM calls ----------------
    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, tags=None,
                      metadata=None, **kwargs):
        self._starts[run_id] = time.time()
        model = (serialized or {}).get("name") or (metadata or {}).get("ls_model_name") or "llm"
        self._write({
            "ts": time.time(), "event": "llm_start", "run_id": str(run_id),
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "name": model, "input": _truncate(prompts),
        })

    def on_llm_end(self, response: LLMResult, *, run_id, parent_run_id=None, **kwargs):
        text = response.generations[0][0].text if response.generations else ""
        usage = (response.llm_output or {}).get("token_usage", {}) if response.llm_output else {}
        self._write({
            "ts": time.time(), "event": "llm_end", "run_id": str(run_id),
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "duration_ms": self._elapsed_ms(run_id), "output": _truncate(text),
            "tokens": usage or None, "status": "ok",
        })

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._write({
            "ts": time.time(), "event": "llm_end", "run_id": str(run_id),
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "duration_ms": self._elapsed_ms(run_id), "status": "error",
            "error": _truncate(error),
        })

    # ---------------- tool calls ----------------
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        self._starts[run_id] = time.time()
        self._write({
            "ts": time.time(), "event": "tool_start", "run_id": str(run_id),
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "name": (serialized or {}).get("name", "tool"), "input": _truncate(input_str),
        })

    def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs):
        self._write({
            "ts": time.time(), "event": "tool_end", "run_id": str(run_id),
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "duration_ms": self._elapsed_ms(run_id), "output": _truncate(output),
            "status": "ok",
        })

    def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._write({
            "ts": time.time(), "event": "tool_end", "run_id": str(run_id),
            "parent_run_id": str(parent_run_id) if parent_run_id else None,
            "duration_ms": self._elapsed_ms(run_id), "status": "error",
            "error": _truncate(error),
        })
