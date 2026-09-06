"""CLI entry point: ``harness-remote`` (local-environment harness worker).

Two modes:

  harness-remote [run-options]   # run the remote worker (default, no subcommand)
  harness-remote attach          # attach an interactive `claude` session to one of
                                 # the worker's rooms (REPL slash commands)

Config is **env-backed**: every option binds to its ``AGENTTEAMS_*`` env var so the
documented environment contract works without a container entrypoint. Flags
override env. Unlike the in-cluster ``harness-worker``, the default install dir
is local (``~/.agentteams/agents``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path
from typing import Optional

import typer

from harness_worker.config import WorkerConfig
from harness_worker.remote_worker import RemoteWorker
from harness_worker.worker import Worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = typer.Typer(
    add_completion=False,
    help="AgentTeams remote (local-environment) harness worker.",
    no_args_is_help=False,
)


def _default_install_dir() -> Path:
    env = os.environ.get("AGENTTEAMS_INSTALL_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".agentteams" / "agents"


@app.callback(invoke_without_command=True)
def run(
    ctx: typer.Context,
    name: Optional[str] = typer.Option(None, "--name", envvar="AGENTTEAMS_WORKER_NAME", help="Worker identity"),
    fs: Optional[str] = typer.Option(None, "--fs", envvar="AGENTTEAMS_FS_ENDPOINT", help="MinIO endpoint (externally reachable)"),
    fs_key: Optional[str] = typer.Option(None, "--fs-key", envvar="AGENTTEAMS_FS_ACCESS_KEY", help="MinIO access key"),
    fs_secret: Optional[str] = typer.Option(None, "--fs-secret", envvar="AGENTTEAMS_FS_SECRET_KEY", help="MinIO secret key"),
    fs_bucket: str = typer.Option("agentteams-storage", "--fs-bucket", envvar="AGENTTEAMS_FS_BUCKET", help="MinIO bucket"),
    fs_secure: bool = typer.Option(False, "--fs-secure", envvar="AGENTTEAMS_FS_SECURE", help="Use TLS for MinIO"),
    matrix_domain: Optional[str] = typer.Option(None, "--matrix-domain", envvar="AGENTTEAMS_MATRIX_DOMAIN", help="Matrix homeserver domain"),
    matrix_homeserver: Optional[str] = typer.Option(None, "--matrix-homeserver", envvar="AGENTTEAMS_MATRIX_URL", help="Override Matrix homeserver URL (e.g. http://localhost:6167 when port-forwarding)"),
    matrix_token: Optional[str] = typer.Option(None, "--matrix-token", envvar="AGENTTEAMS_WORKER_MATRIX_TOKEN", help="Pre-provisioned Matrix access token (override)"),
    gateway_url: Optional[str] = typer.Option(None, "--gateway-url", envvar="AGENTTEAMS_AI_GATEWAY_URL", help="Higress gateway URL (externally reachable)"),
    gateway_key: Optional[str] = typer.Option(None, "--gateway-key", envvar="AGENTTEAMS_WORKER_GATEWAY_KEY", help="Higress consumer key"),
    use_subscription: bool = typer.Option(False, "--use-subscription", envvar="AGENTTEAMS_USE_CLAUDE_SUBSCRIPTION", help="Use claude.ai subscription via OAuth (run `claude login` first). Skips AI gateway."),
    model: Optional[str] = typer.Option(None, "--model", envvar="AGENTTEAMS_MODEL", help="Override LLM model (e.g. claude-opus-4-5). In subscription mode defaults to claude-sonnet-4-5 if not set."),
    install_dir: Optional[Path] = typer.Option(None, "--install-dir", envvar="AGENTTEAMS_INSTALL_DIR", help="Local workspace root (default ~/.agentteams/agents)"),
    sync_interval: int = typer.Option(60, "--sync-interval", envvar="AGENTTEAMS_SYNC_INTERVAL", help="Pull interval (seconds)"),
    harness_type: str = typer.Option("claude", "--harness-type", envvar="AGENTTEAMS_HARNESS_TYPE", help="Harness CLI: claude|gemini|opencode|codex"),
) -> None:
    # When a subcommand (e.g. `attach`) is invoked, do not start the worker.
    if ctx.invoked_subcommand is not None:
        return

    required = {
        "--name / AGENTTEAMS_WORKER_NAME": name,
        "--fs / AGENTTEAMS_FS_ENDPOINT": fs,
        "--fs-key / AGENTTEAMS_FS_ACCESS_KEY": fs_key,
        "--fs-secret / AGENTTEAMS_FS_SECRET_KEY": fs_secret,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise typer.BadParameter("missing required values: " + ", ".join(missing))

    # Export the values that inherited code + the bridge read directly from the
    # environment (so passing them as flags works the same as exporting them).
    os.environ["AGENTTEAMS_WORKER_NAME"] = name
    if matrix_domain:
        os.environ["AGENTTEAMS_MATRIX_DOMAIN"] = matrix_domain
    if matrix_homeserver:
        os.environ["AGENTTEAMS_MATRIX_URL"] = matrix_homeserver
    if matrix_token:
        os.environ["AGENTTEAMS_WORKER_MATRIX_TOKEN"] = matrix_token
    if use_subscription:
        os.environ["AGENTTEAMS_USE_CLAUDE_SUBSCRIPTION"] = "1"
    elif gateway_url:
        os.environ["AGENTTEAMS_AI_GATEWAY_URL"] = gateway_url
    if gateway_key:
        os.environ["AGENTTEAMS_WORKER_GATEWAY_KEY"] = gateway_key
    if model:
        os.environ["AGENTTEAMS_MODEL"] = model

    config = WorkerConfig(
        worker_name=name,
        minio_endpoint=fs,
        minio_access_key=fs_key,
        minio_secret_key=fs_secret,
        minio_bucket=fs_bucket,
        minio_secure=fs_secure,
        sync_interval=sync_interval,
        install_dir=install_dir.expanduser() if install_dir else _default_install_dir(),
        harness_type=harness_type,
        model=model,
    )
    _run_worker(RemoteWorker(config))


def _load_session_map(path: Path) -> dict[str, str]:
    """Read the ``{room_id: session_id}`` map written by the worker."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


@app.command()
def attach(
    name: Optional[str] = typer.Option(None, "--name", envvar="AGENTTEAMS_WORKER_NAME", help="Worker identity"),
    install_dir: Optional[Path] = typer.Option(None, "--install-dir", envvar="AGENTTEAMS_INSTALL_DIR", help="Local workspace root"),
    harness_type: str = typer.Option("claude", "--harness-type", envvar="AGENTTEAMS_HARNESS_TYPE", help="Harness CLI"),
    room: Optional[str] = typer.Option(None, "--room", help="Matrix room id to attach to (required when the worker has more than one session)"),
) -> None:
    """Attach an interactive session to one of the worker's conversations.

    Runs ``claude --resume <session>`` in the workspace so a developer can issue
    REPL-only slash commands (``/clear``, ``/compact``, ``/model``) and steer
    manually. Sessions are tracked per Matrix room, so ``--room`` selects which
    conversation to attach to; it may be omitted when only one session exists.
    Caveat: do this while the relay is idle — a relay-spawned ``claude -p`` and
    this interactive session can race on the session file.
    """
    if not name:
        raise typer.BadParameter("missing --name / AGENTTEAMS_WORKER_NAME")
    if harness_type != "claude":
        raise typer.BadParameter("attach currently supports --harness-type claude only")

    base = install_dir.expanduser() if install_dir else _default_install_dir()
    workspace = base / name
    if not workspace.is_dir():
        raise typer.BadParameter(f"workspace not found: {workspace} (run the worker first)")

    # Source .harness/.env so any local secrets are available to the session.
    Worker._load_env_file(workspace / ".harness" / ".env")

    sessions = _load_session_map(workspace / ".harness" / "sessions" / "rooms.json")
    session_id = None
    if room:
        session_id = sessions.get(room)
        if session_id is None:
            known = ", ".join(sorted(sessions)) or "(none)"
            raise typer.BadParameter(f"no session for room {room}. Known rooms: {known}")
    elif len(sessions) == 1:
        room, session_id = next(iter(sessions.items()))
    elif len(sessions) > 1:
        listing = "\n".join(f"  {rid}  →  {sid}" for rid, sid in sorted(sessions.items()))
        raise typer.BadParameter(
            f"worker has {len(sessions)} sessions; pass --room to pick one:\n{listing}"
        )

    argv = ["claude"]
    if session_id:
        argv += ["--resume", session_id]
        typer.echo(f"Attaching to session {session_id} (room {room}) in {workspace}")
    else:
        typer.echo(f"No saved session; starting a fresh interactive claude in {workspace}")

    os.chdir(workspace)
    # Hand the TTY directly to claude (replace this process).
    os.execvpe(argv[0], argv, os.environ.copy())


def _run_worker(worker: RemoteWorker) -> None:
    async def _async_run() -> None:
        loop = asyncio.get_running_loop()

        def _shutdown() -> None:
            asyncio.create_task(worker.stop())

        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

        await worker.run()

    try:
        asyncio.run(_async_run())
    except KeyboardInterrupt:
        pass


def main() -> None:
    app()


if __name__ == "__main__":
    main()
