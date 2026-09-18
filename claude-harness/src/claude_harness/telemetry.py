"""Structured telemetry for the harness worker — OTel-*shaped*, no OTel SDK.

Why this exists
---------------
``claude.py`` already logs everything worth knowing (tokens, duration, every
tool call) but as free text, so nothing can query it. This module turns those same
moments into structured events without changing what the worker does.

Why no OpenTelemetry
--------------------
There is no OTLP collector in this deployment yet (ADR-0010). Pulling in the
SDK would mean a dependency, a batch processor and a collector to operate, for
zero extra signal today. Instead the envelope *matches* the OTel log/span data
model and uses GenAI semantic-convention attribute names, so swapping in a real
exporter later is a transport change, not a re-instrumentation.

Two sinks, deliberately unequal
-------------------------------
- **stdout** — always on, cannot fail, picked up by Cloud Logging on GKE. This
  is the backstop: if everything else breaks, the events are still on disk.
- **HTTP ingest** — best effort. Bounded queue, background thread, drops on
  backpressure. A turn must never slow down or fail because telemetry is
  unhappy; when events are dropped we emit an ``observation_gap`` so the gap is
  *visible* rather than silently mistaken for "nothing happened".
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1"

# Event names. Flat namespace, dotted, stable — these become a column people
# filter on for years, so renaming them later is expensive.
TURN_START = "agent.turn.start"
SESSION_INIT = "agent.session.init"
LLM_REQUEST = "agent.llm.request"
TOOL_CALL = "agent.tool.call"
TOOL_RESULT = "agent.tool.result"
TURN_END = "agent.turn.end"
OBSERVATION_GAP = "agent.observation_gap"


def new_trace_id() -> str:
    """128-bit trace id, hex — one per turn."""
    return secrets.token_hex(16)


def new_span_id() -> str:
    """64-bit span id, hex — one per LLM request or tool call."""
    return secrets.token_hex(8)


@dataclass
class TurnContext:
    """Correlation identity for one worker turn.

    Everything downstream joins on these. The harness adapters know nothing
    about rooms or projects, so this is threaded through the ``state`` dict
    that ``process_stream_line`` already receives — no signature changes.
    """

    turn_id: str
    worker: str
    room_id: str = ""
    project_id: str = ""
    task_id: str = ""
    trace_id: str = field(default_factory=new_trace_id)
    started_at: float = field(default_factory=time.time)

    def attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"agentteams.turn_id": self.turn_id}
        if self.room_id:
            attrs["agentteams.room_id"] = self.room_id
        if self.project_id:
            attrs["agentteams.project_id"] = self.project_id
        if self.task_id:
            attrs["agentteams.task_id"] = self.task_id
        return attrs


# ── Tool classification ──────────────────────────────────────────────────
#
# Tool *arguments* are never recorded: they routinely contain file contents,
# credentials and customer data. What gets stored is a digest (so repeated
# identical calls are recognisable) plus a coarse, deliberately lossy target.

_READ_TOOLS = {"Read", "Glob", "Grep", "NotebookRead", "WebFetch", "WebSearch", "TodoRead"}
_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}
_EXEC_TOOLS = {"Bash", "BashOutput", "KillShell"}


def classify_tool(name: str) -> str:
    """Coarse bucket used for dashboards and for write-access review."""
    if name in _WRITE_TOOLS:
        return "write"
    if name in _EXEC_TOOLS:
        return "exec"
    if name in _READ_TOOLS:
        return "read"
    if name.startswith("mcp__"):
        return "mcp"
    return "other"


def _digest(value: Any) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, default=str)
    except Exception:  # pragma: no cover - defensive
        raw = repr(value)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def summarize_tool_args(name: str, args: dict) -> dict[str, Any]:
    """Digest + a coarse target. Never the full argument payload.

    The target is the smallest thing that still makes an event actionable: a
    path, the first word of a command, or the MCP server/tool pair. Anything
    more starts leaking the payload we just decided not to store.
    """
    out: dict[str, Any] = {
        "agentteams.tool.name": name,
        "agentteams.tool.class": classify_tool(name),
        "agentteams.tool.args_digest": _digest(args),
    }
    target = ""
    if isinstance(args, dict):
        if "file_path" in args:
            target = str(args["file_path"])
        elif "path" in args:
            target = str(args["path"])
        elif "command" in args:
            # First token only — enough to see "git" vs "rm", not the arguments.
            target = str(args["command"]).strip().split(maxsplit=1)[:1][0] if str(args["command"]).strip() else ""
        elif "url" in args:
            target = str(args["url"]).split("?", 1)[0]
        elif "pattern" in args:
            target = "<pattern>"
    if name.startswith("mcp__"):
        target = name  # mcp__<server>__<tool> is already the useful identity
    if target:
        out["agentteams.tool.target"] = target[:256]
    return out


class _HTTPSink:
    """Bounded, lossy, never blocks the caller."""

    def __init__(self, endpoint: str, *, max_queue: int, batch: int, timeout: float) -> None:
        self._endpoint = endpoint
        self._q: queue.Queue = queue.Queue(maxsize=max_queue)
        self._batch = batch
        self._timeout = timeout
        self._dropped = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="telemetry-sink", daemon=True)
        self._thread.start()

    def offer(self, envelope: dict) -> int:
        """Enqueue if there is room. Returns number newly dropped (0 or 1)."""
        try:
            self._q.put_nowait(envelope)
            return 0
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return 1

    def take_dropped(self) -> int:
        with self._lock:
            n, self._dropped = self._dropped, 0
        return n

    def _run(self) -> None:
        import httpx  # imported lazily: stdout sink must work even if httpx is broken

        client = httpx.Client(timeout=self._timeout)
        while True:
            batch = [self._q.get()]
            while len(batch) < self._batch:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            try:
                client.post(self._endpoint, json={"events": batch})
            except Exception as exc:  # network hiccups must stay invisible to the turn
                logger.debug("telemetry ingest failed (non-fatal): %s", exc)


class Emitter:
    def __init__(self) -> None:
        self._enabled = True
        self._resource: dict[str, Any] = {}
        self._sink: Optional[_HTTPSink] = None

    def configure(self, *, worker_name: str = "") -> None:
        self._enabled = os.environ.get("AGENTTEAMS_TELEMETRY_ENABLED", "1") != "0"
        self._resource = {
            "service.name": "agentteams-claude-harness",
            "agentteams.worker": worker_name or os.environ.get("AGENTTEAMS_WORKER_NAME", ""),
        }
        pod = os.environ.get("HOSTNAME", "")
        if pod:
            self._resource["k8s.pod.name"] = pod

        endpoint = os.environ.get("AGENTTEAMS_TELEMETRY_ENDPOINT", "").strip()
        if endpoint and self._enabled:
            self._sink = _HTTPSink(
                endpoint,
                max_queue=int(os.environ.get("AGENTTEAMS_TELEMETRY_QUEUE", "1000")),
                batch=int(os.environ.get("AGENTTEAMS_TELEMETRY_BATCH", "50")),
                timeout=float(os.environ.get("AGENTTEAMS_TELEMETRY_TIMEOUT", "5")),
            )
            logger.info("telemetry: ingest endpoint %s", endpoint)

    def emit(
        self,
        event: str,
        ctx: Optional[TurnContext],
        *,
        message: str = "",
        attributes: Optional[dict[str, Any]] = None,
        span_id: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        severity: str = "INFO",
    ) -> None:
        if not self._enabled:
            return
        attrs: dict[str, Any] = {}
        if ctx is not None:
            attrs.update(ctx.attributes())
        if attributes:
            attrs.update({k: v for k, v in attributes.items() if v is not None})

        envelope = {
            "ts": time.time(),
            "schema_version": SCHEMA_VERSION,
            "event": event,
            "severity_text": severity,
            "message": message,
            "resource": self._resource,
            "attributes": attrs,
        }
        if ctx is not None:
            envelope["trace_id"] = ctx.trace_id
        if span_id:
            envelope["span_id"] = span_id
        if parent_span_id:
            envelope["parent_span_id"] = parent_span_id

        # stdout first and unconditionally: this path has no failure mode.
        # `message` stays human-readable so plain `kubectl logs` is still usable.
        #
        # In text mode this drops to DEBUG: the existing claude.py log lines
        # already narrate the same moments, and echoing each one twice makes a
        # developer's terminal unreadable for no gain.
        from claude_harness import logging_setup

        level = logging.INFO if logging_setup.ACTIVE_FORMAT == "json" else logging.DEBUG
        logger.log(level, message or event, extra={"telemetry": envelope})

        if self._sink is not None:
            dropped = self._sink.offer(envelope)
            if dropped:
                self._report_gap(ctx)

    def _report_gap(self, ctx: Optional[TurnContext]) -> None:
        """Backpressure is recorded, never hidden.

        A missing event and an event that never happened look identical
        downstream, so the gap itself has to be a fact in the stream.
        """
        if self._sink is None:
            return
        n = self._sink.take_dropped()
        if not n:
            return
        envelope = {
            "ts": time.time(),
            "schema_version": SCHEMA_VERSION,
            "event": OBSERVATION_GAP,
            "severity_text": "WARN",
            "message": f"telemetry ingest saturated, dropped {n} event(s)",
            "resource": self._resource,
            "attributes": {"agentteams.dropped_events": n, **(ctx.attributes() if ctx else {})},
        }
        logger.warning(envelope["message"], extra={"telemetry": envelope})


emitter = Emitter()


def emit(event: str, ctx: Optional[TurnContext], **kw: Any) -> None:
    """Module-level shorthand so call sites stay one line."""
    emitter.emit(event, ctx, **kw)
