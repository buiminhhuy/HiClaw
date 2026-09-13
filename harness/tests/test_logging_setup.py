"""Log format selection.

The in-cluster heuristic is load-bearing: get it wrong and every worker pod
ships unqueryable text to a log backend that was built to index JSON, and
nobody notices because the logs still *look* fine.
"""
from __future__ import annotations

import logging

import pytest

from harness_worker import logging_setup


@pytest.fixture(autouse=True)
def _isolate_root_logger():
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    yield
    root.handlers[:] = saved
    root.setLevel(level)


def test_explicit_setting_wins(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.4.0.1")
    monkeypatch.setenv("AGENTTEAMS_LOG_FORMAT", "text")
    assert logging_setup.configure_logging() == "text"


def test_defaults_to_json_in_a_pod(monkeypatch):
    monkeypatch.delenv("AGENTTEAMS_LOG_FORMAT", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.4.0.1")
    assert logging_setup.configure_logging() == "json"


def test_defaults_to_text_on_a_laptop(monkeypatch):
    monkeypatch.delenv("AGENTTEAMS_LOG_FORMAT", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert logging_setup.configure_logging() == "text"


def test_service_account_token_is_not_the_signal(monkeypatch, tmp_path):
    """Worker pods run without a mounted token on purpose.

    An earlier version keyed off that file and silently fell back to text in
    every worker pod — the exact bug this asserts against.
    """
    monkeypatch.delenv("AGENTTEAMS_LOG_FORMAT", raising=False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.4.0.1")
    assert logging_setup.configure_logging() == "json", (
        "in-cluster detection must not depend on a service account token"
    )


def test_garbage_value_falls_back_rather_than_crashing(monkeypatch):
    monkeypatch.setenv("AGENTTEAMS_LOG_FORMAT", "yaml-please")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert logging_setup.configure_logging() == "text"


def test_configure_replaces_handlers_rather_than_stacking(monkeypatch):
    monkeypatch.setenv("AGENTTEAMS_LOG_FORMAT", "json")
    logging_setup.configure_logging()
    logging_setup.configure_logging()
    # Two handlers would print every line twice.
    assert len(logging.getLogger().handlers) == 1
