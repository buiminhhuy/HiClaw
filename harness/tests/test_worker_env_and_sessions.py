"""Subprocess environment isolation and per-room CLI session tracking."""
from __future__ import annotations

import json
from pathlib import Path

from harness_worker.config import WorkerConfig
from harness_worker.worker import Worker, _subprocess_env


def _worker(tmp_path: Path) -> Worker:
    return Worker(
        WorkerConfig(
            worker_name="w1",
            minio_endpoint="localhost:9000",
            minio_access_key="k",
            minio_secret_key="s",
            install_dir=tmp_path,
        )
    )


# --------------------------------------------------------------------------
# Subprocess environment allowlist
# --------------------------------------------------------------------------


def test_worker_secrets_are_not_forwarded_to_the_cli(monkeypatch) -> None:
    for name in (
        "AGENTTEAMS_WORKER_MATRIX_TOKEN",
        "AGENTTEAMS_FS_ACCESS_KEY",
        "AGENTTEAMS_FS_SECRET_KEY",
        "AGENTTEAMS_WORKER_GATEWAY_KEY",
        "MATRIX_ALLOWED_USERS",
    ):
        monkeypatch.setenv(name, "secret")
    monkeypatch.delenv("AGENTTEAMS_HARNESS_ENV_PASSTHROUGH", raising=False)

    env = _subprocess_env({})

    assert not any(key.startswith("AGENTTEAMS_") for key in env)
    assert "MATRIX_ALLOWED_USERS" not in env
    assert "secret" not in env.values()


def test_allowlisted_and_cli_owned_names_are_forwarded(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gw")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/cfg")
    monkeypatch.delenv("AGENTTEAMS_HARNESS_ENV_PASSTHROUGH", raising=False)

    env = _subprocess_env({})

    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/root"
    assert env["ANTHROPIC_BASE_URL"] == "http://gw"
    assert env["CLAUDE_CONFIG_DIR"] == "/cfg"


def test_harness_env_overrides_inherited_values(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://stale")
    monkeypatch.delenv("AGENTTEAMS_HARNESS_ENV_PASSTHROUGH", raising=False)

    env = _subprocess_env({"ANTHROPIC_BASE_URL": "http://fresh"})

    assert env["ANTHROPIC_BASE_URL"] == "http://fresh"


def test_operator_can_opt_extra_names_in(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("UNRELATED", "nope")
    monkeypatch.setenv("AGENTTEAMS_HARNESS_ENV_PASSTHROUGH", "GITHUB_TOKEN, OTHER")

    env = _subprocess_env({})

    assert env["GITHUB_TOKEN"] == "ghp_x"
    assert "UNRELATED" not in env


# --------------------------------------------------------------------------
# Per-room session map
# --------------------------------------------------------------------------


def test_sessions_are_tracked_per_room(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    worker._save_session("!a:localhost", "sess-a")
    worker._save_session("!b:localhost", "sess-b")

    stored = json.loads(worker._sessions_path.read_text())
    assert stored == {"!a:localhost": "sess-a", "!b:localhost": "sess-b"}

    reloaded = _worker(tmp_path)._load_sessions()
    assert reloaded["!a:localhost"] == "sess-a"
    assert reloaded["!b:localhost"] == "sess-b"


def test_legacy_worker_wide_session_file_is_ignored(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    legacy = worker._harness_home / "sessions" / "current"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("sess-shared")

    assert worker._load_sessions() == {}


def test_corrupt_session_map_does_not_break_startup(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    worker._sessions_path.parent.mkdir(parents=True, exist_ok=True)
    worker._sessions_path.write_text("{not json")

    assert worker._load_sessions() == {}
