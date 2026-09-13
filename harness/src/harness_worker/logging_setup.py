"""Logging setup shared by ``harness-worker`` and ``harness-remote``.

Two formats, one call site:

- ``text`` — what we have always had. Still the right default for a laptop.
- ``json`` — one object per line for log backends that index fields. The
  human-readable string is preserved verbatim under ``message``, so
  ``kubectl logs`` remains readable by a person and does not become a wall of
  escaped JSON that only a query engine can love.

Telemetry events ride the same path: ``telemetry.emit`` attaches an envelope
via ``extra={"telemetry": ...}``, which the JSON formatter merges into the
record. In text mode that envelope is simply ignored and the human line is
printed — so turning structured logging off never loses the narrative.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

TEXT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Set by configure_logging(). Telemetry reads it to avoid echoing every event a
# second time in text mode, where the existing human log lines already say it.
ACTIVE_FORMAT = "text"

# Attributes LogRecord always carries; anything else was passed via `extra`.
_RESERVED = frozenset(
    """args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName relativeCreated
    stack_info thread threadName taskName telemetry""".split()
)


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "severity_text": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        envelope = getattr(record, "telemetry", None)
        if isinstance(envelope, dict):
            # Telemetry envelope wins on overlapping keys: it is the structured
            # truth, the log line is its human rendering.
            payload.update(envelope)
            payload["message"] = envelope.get("message") or record.getMessage()

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload.setdefault(key, value)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> str:
    """Install the formatter chosen by ``AGENTTEAMS_LOG_FORMAT``.

    Defaults to ``json`` when running inside Kubernetes (``HOSTNAME`` plus a
    service account token is a reliable enough tell) and ``text`` otherwise.
    Returns the format actually used, for logging it back.
    """
    global ACTIVE_FORMAT
    choice = os.environ.get("AGENTTEAMS_LOG_FORMAT", "").strip().lower()
    if choice not in ("json", "text"):
        in_cluster = os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")
        choice = "json" if in_cluster else "text"
    ACTIVE_FORMAT = choice

    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter() if choice == "json" else logging.Formatter(TEXT_FORMAT))

    root = logging.getLogger()
    for existing in root.handlers[:]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    return choice
