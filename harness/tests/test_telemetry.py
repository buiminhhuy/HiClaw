"""Telemetry emission and redaction.

The point of these tests is less "does it emit" and more "does it refuse to
emit the things we promised never to store".
"""
from __future__ import annotations

import json
import logging

import pytest

from harness_worker import telemetry as tm
from harness_worker.harness.claude import ClaudeHarness
from harness_worker.logging_setup import JSONFormatter


@pytest.fixture
def captured(monkeypatch):
    """Collect envelopes instead of writing them anywhere."""
    events: list[dict] = []

    def _fake(record_event, ctx, **kw):
        attrs = dict(ctx.attributes()) if ctx else {}
        attrs.update({k: v for k, v in (kw.get("attributes") or {}).items() if v is not None})
        # kw first: the merged `attributes` below must win over the raw one.
        events.append({**kw, "event": record_event, "attributes": attrs})

    monkeypatch.setattr(tm.emitter, "emit", _fake)
    return events


def _stream(harness, lines, ctx=None):
    state: dict = {"_ctx": ctx} if ctx else {}
    for line in lines:
        harness.process_stream_line(json.dumps(line), state)
    return state


# ── Redaction ────────────────────────────────────────────────────────────


def test_tool_args_are_digested_not_stored():
    args = {"file_path": "/etc/passwd", "content": "SUPER_SECRET_TOKEN=abc123"}
    out = tm.summarize_tool_args("Write", args)

    blob = json.dumps(out)
    assert "SUPER_SECRET_TOKEN" not in blob
    assert "abc123" not in blob
    assert out["agentteams.tool.target"] == "/etc/passwd"
    assert out["agentteams.tool.class"] == "write"
    assert len(out["agentteams.tool.args_digest"]) == 16


def test_bash_keeps_only_the_first_token():
    out = tm.summarize_tool_args("Bash", {"command": "curl -H 'Authorization: Bearer sk-live-xyz' api"})
    assert out["agentteams.tool.target"] == "curl"
    assert "sk-live-xyz" not in json.dumps(out)


def test_identical_args_digest_identically():
    a = tm.summarize_tool_args("Read", {"file_path": "/a", "x": 1})
    b = tm.summarize_tool_args("Read", {"x": 1, "file_path": "/a"})
    assert a["agentteams.tool.args_digest"] == b["agentteams.tool.args_digest"]


def test_tool_result_records_size_not_content(captured):
    harness = ClaudeHarness()
    ctx = tm.TurnContext(turn_id="t1", worker="w1", room_id="!r:x")
    _stream(
        harness,
        [{
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "LEAKED PAYLOAD", "is_error": False}]},
        }],
        ctx,
    )
    results = [e for e in captured if e["event"] == tm.TOOL_RESULT]
    assert len(results) == 1
    assert "LEAKED PAYLOAD" not in json.dumps(results[0])
    assert results[0]["attributes"]["agentteams.tool.result_chars"] == len("LEAKED PAYLOAD")


# ── Per-turn usage: the thing the old code threw away ────────────────────


def test_per_assistant_message_usage_is_emitted(captured):
    harness = ClaudeHarness()
    ctx = tm.TurnContext(turn_id="t1", worker="w1")
    _stream(
        harness,
        [
            {"type": "assistant", "message": {
                "model": "claude-opus-4-6",
                "usage": {"input_tokens": 3, "output_tokens": 67,
                          "cache_creation_input_tokens": 15627, "cache_read_input_tokens": 0},
                "content": [{"type": "text", "text": "hi"}]}},
            {"type": "assistant", "message": {
                "model": "claude-opus-4-6",
                "usage": {"input_tokens": 1, "output_tokens": 10,
                          "cache_creation_input_tokens": 91, "cache_read_input_tokens": 15627},
                "content": [{"type": "text", "text": "there"}]}},
        ],
        ctx,
    )
    calls = [e for e in captured if e["event"] == tm.LLM_REQUEST]
    assert len(calls) == 2, "one event per LLM call, not one per turn"
    assert calls[0]["attributes"]["gen_ai.usage.output_tokens"] == 67
    assert calls[1]["attributes"]["gen_ai.usage.cache_read_input_tokens"] == 15627
    # Distinct spans so calls can be told apart downstream.
    assert calls[0]["span_id"] != calls[1]["span_id"]


def test_assistant_message_without_usage_emits_nothing(captured):
    harness = ClaudeHarness()
    _stream(harness, [{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}],
            tm.TurnContext(turn_id="t", worker="w"))
    assert [e for e in captured if e["event"] == tm.LLM_REQUEST] == []


def test_result_summary_is_stashed_for_turn_end():
    harness = ClaudeHarness()
    state = _stream(harness, [{
        "type": "result", "session_id": "s1", "duration_ms": 5507, "num_turns": 2,
        "total_cost_usd": 0.108,
        "usage": {"input_tokens": 4, "output_tokens": 77, "cache_read_input_tokens": 15627},
    }], tm.TurnContext(turn_id="t", worker="w"))
    summary = state["_result_summary"]
    assert summary["agentteams.cost_usd"] == 0.108
    assert summary["agentteams.num_turns"] == 2
    assert summary["gen_ai.usage.cache_read_input_tokens"] == 15627


# ── Correlation ──────────────────────────────────────────────────────────


def test_events_carry_turn_correlation(captured):
    harness = ClaudeHarness()
    ctx = tm.TurnContext(turn_id="turn-7", worker="alice", room_id="!room:momo", project_id="p1")
    _stream(harness, [{"type": "system", "subtype": "init", "session_id": "sess-1"}], ctx)
    init = [e for e in captured if e["event"] == tm.SESSION_INIT][0]
    assert init["attributes"]["agentteams.turn_id"] == "turn-7"
    assert init["attributes"]["agentteams.room_id"] == "!room:momo"
    assert init["attributes"]["agentteams.project_id"] == "p1"


def test_missing_context_does_not_break_the_stream(captured):
    """A harness invoked outside a turn (e.g. parse_output) must still work."""
    harness = ClaudeHarness()
    state = _stream(harness, [
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "assistant", "message": {"usage": {"input_tokens": 1, "output_tokens": 2},
                                          "content": [{"type": "text", "text": "ok"}]}},
    ])
    assert "".join(state["text_chunks"]) == "ok"


def test_trace_ids_are_valid_otel_widths():
    ctx = tm.TurnContext(turn_id="t", worker="w")
    assert len(ctx.trace_id) == 32 and int(ctx.trace_id, 16) >= 0
    assert len(tm.new_span_id()) == 16


# ── Log formatting ───────────────────────────────────────────────────────


def test_json_formatter_keeps_human_message_and_merges_envelope():
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "claude tool call: Bash", None, None)
    rec.telemetry = {"event": "agent.tool.call", "attributes": {"agentteams.tool.class": "exec"},
                     "message": "claude tool call: Bash"}
    out = json.loads(JSONFormatter().format(rec))

    assert out["message"] == "claude tool call: Bash", "kubectl logs must stay readable"
    assert out["event"] == "agent.tool.call"
    assert out["attributes"]["agentteams.tool.class"] == "exec"
    assert out["severity_text"] == "INFO"


def test_json_formatter_survives_unserializable_extras():
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "msg", None, None)
    rec.weird = object()
    json.loads(JSONFormatter().format(rec))  # must not raise


# ── Backpressure ─────────────────────────────────────────────────────────


def test_dropped_events_surface_as_observation_gap(monkeypatch, caplog):
    em = tm.Emitter()
    em._enabled = True
    em._resource = {"service.name": "t"}

    class _FullSink:
        def offer(self, _):
            return 1

        def take_dropped(self):
            return 3

    em._sink = _FullSink()
    with caplog.at_level(logging.WARNING, logger="harness_worker.telemetry"):
        em.emit(tm.TOOL_CALL, tm.TurnContext(turn_id="t", worker="w"), message="x")

    gaps = [r for r in caplog.records if getattr(r, "telemetry", {}).get("event") == tm.OBSERVATION_GAP]
    assert gaps, "a dropped event must be visible, not silently absent"
    assert gaps[0].telemetry["attributes"]["agentteams.dropped_events"] == 3


def test_disabled_emitter_is_a_noop(caplog):
    """Capture at DEBUG: text mode emits at DEBUG, so an INFO-only assertion
    would pass even when the emitter is very much enabled."""
    em = tm.Emitter()
    em._resource = {}

    em._enabled = True
    with caplog.at_level(logging.DEBUG, logger="harness_worker.telemetry"):
        em.emit(tm.TOOL_CALL, None, message="marker")
    assert [r for r in caplog.records if "marker" in r.getMessage()], "guard: enabled must emit"

    caplog.clear()
    em._enabled = False
    with caplog.at_level(logging.DEBUG, logger="harness_worker.telemetry"):
        em.emit(tm.TOOL_CALL, None, message="marker")
    assert not [r for r in caplog.records if "marker" in r.getMessage()]
