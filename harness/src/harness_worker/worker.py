"""Harness Worker main entry point."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Dict, Optional

from rich.console import Console
from rich.panel import Panel

from harness_worker.bridge import bridge_openclaw_to_harness, _is_in_container, _port_remap
from harness_worker.config import WorkerConfig
from harness_worker.harness import build_harness
from harness_worker.matrix_relay import MatrixRelay
from harness_worker.sync import FileSync, push_loop, sync_loop

console = Console()

# ── Subprocess environment allowlist ──────────────────────────────────────────
# The CLI agent — and every shell command it runs — inherits whatever we hand to
# create_subprocess_exec. The worker pod's own environment holds the Matrix
# access token, the object-storage access/secret keys and the gateway consumer
# key, so it must never be forwarded wholesale. Only these names, the CLI-owned
# prefixes below, and whatever the operator opts into via
# AGENTTEAMS_HARNESS_ENV_PASSTHROUGH cross the boundary.
_SUBPROCESS_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TERM_PROGRAM",
    "PWD", "TMPDIR", "TZ",
    "LANG", "LC_ALL", "LC_CTYPE",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
})

# CLI-owned configuration namespaces. AGENTTEAMS_* is deliberately absent.
_SUBPROCESS_ENV_PREFIXES = (
    "ANTHROPIC_", "CLAUDE_", "GEMINI_", "GOOGLE_", "OPENAI_", "CODEX_",
    "OPENCODE_", "NODE_", "NPM_", "XDG_",
)


def _subprocess_env(harness_env: Dict[str, str]) -> Dict[str, str]:
    """Build the CLI subprocess environment from an allowlist of os.environ."""
    extra = {
        name.strip()
        for name in os.environ.get("AGENTTEAMS_HARNESS_ENV_PASSTHROUGH", "").split(",")
        if name.strip()
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _SUBPROCESS_ENV_ALLOWLIST
        or key in extra
        or key.startswith(_SUBPROCESS_ENV_PREFIXES)
    }
    env.update(harness_env)
    return env


logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.worker_name = config.worker_name
        self.sync: Optional[FileSync] = None
        self._harness_home: Path = config.harness_home
        self._relay_task: Optional[asyncio.Task] = None
        self._stopping = False
        self._harness = None
        # CLI session id per Matrix room. A single worker-wide session would
        # splice unrelated rooms into one conversation.
        self._sessions: Dict[str, str] = {}

    async def run(self) -> None:
        if not await self.start():
            return
        try:
            await self._run_matrix_relay()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        console.print("[yellow]Stopping harness worker...[/yellow]")
        if self._relay_task and not self._relay_task.done():
            self._relay_task.cancel()
            try:
                await self._relay_task
            except (asyncio.CancelledError, Exception):
                pass
        console.print("[green]Harness worker stopped.[/green]")

    async def start(self) -> bool:
        console.print(
            Panel.fit(
                f"[bold green]Harness Worker[/bold green]\n"
                f"Worker: [cyan]{self.worker_name}[/cyan]\n"
                f"Harness type: [cyan]{self.config.harness_type}[/cyan]\n"
                f"HARNESS_HOME: [cyan]{self._harness_home}[/cyan]",
                title="Starting",
            )
        )

        self._ensure_mc()

        self.sync = FileSync(
            endpoint=self.config.minio_endpoint,
            access_key=self.config.minio_access_key,
            secret_key=self.config.minio_secret_key,
            bucket=self.config.minio_bucket,
            worker_name=self.worker_name,
            secure=self.config.minio_secure,
            local_dir=self.config.workspace_dir,
        )

        console.print("[yellow]Pulling all files from MinIO...[/yellow]")
        try:
            self.sync.mirror_all()
        except Exception as exc:
            console.print(f"[red]Failed to mirror from MinIO: {exc}[/red]")
            return False

        try:
            openclaw_cfg = self.sync.get_config()
        except Exception as exc:
            console.print(f"[red]Failed to read openclaw.json: {exc}[/red]")
            return False

        openclaw_cfg = self._matrix_relogin(openclaw_cfg)

        self._harness_home.mkdir(parents=True, exist_ok=True)

        console.print("[yellow]Bridging openclaw.json → harness config...[/yellow]")
        try:
            self._harness = build_harness(self.config.harness_type)
            self._harness.bridge_config(openclaw_cfg, self._harness_home)
        except Exception as exc:
            console.print(f"[red]Bridge failed: {exc}[/red]")
            return False

        self._load_env_file(self._harness_home / ".env")
        self._apply_matrix_env(openclaw_cfg)

        self._sessions = self._load_sessions()
        if self._sessions:
            console.print(
                f"[dim]Restored {len(self._sessions)} CLI session(s) from disk[/dim]"
            )

        asyncio.create_task(
            sync_loop(
                self.sync,
                interval=self.config.sync_interval,
                on_pull=self._on_files_pulled,
            )
        )
        asyncio.create_task(push_loop(self.sync, check_interval=5))

        self._mark_ready()

        console.print("[bold green]Harness worker initialized.[/bold green]")
        return True

    def _mark_ready(self) -> None:
        """Drop the readiness marker the container entrypoint polls for.

        The entrypoint reports readiness to the controller once this file
        appears, so it must be written only after the bridge has produced a
        usable harness config.
        """
        try:
            self._harness_home.mkdir(parents=True, exist_ok=True)
            (self._harness_home / "ready").write_text(str(int(time.time())))
        except OSError as exc:
            logger.warning("failed to write readiness marker: %s", exc)

    async def _run_matrix_relay(self) -> None:
        from harness_worker.policies import DualAllowList, HistoryBuffer

        openclaw_cfg = self.sync.get_config() if self.sync else {}
        matrix_cfg = openclaw_cfg.get("channels", {}).get("matrix", {})
        homeserver = _port_remap(matrix_cfg.get("homeserver", ""), _is_in_container())
        access_token = matrix_cfg.get("accessToken", "")

        if not homeserver or not access_token:
            console.print("[yellow]Matrix not configured; running without relay.[/yellow]")
            await asyncio.sleep(float("inf"))
            return

        worker_name = os.environ.get("AGENTTEAMS_WORKER_NAME", self.worker_name)
        domain = os.environ.get("AGENTTEAMS_MATRIX_DOMAIN", "")
        if not domain:
            # Derive domain from userId in openclaw.json (e.g. "@name:domain" → "domain")
            user_id = matrix_cfg.get("userId", "")
            if ":" in user_id:
                domain = user_id.split(":", 1)[1]
        if not worker_name or not domain:
            console.print("[yellow]Matrix credentials incomplete; running without relay.[/yellow]")
            await asyncio.sleep(float("inf"))
            return

        full_user_id = f"@{worker_name}:{domain}"
        device_id = matrix_cfg.get("deviceId", "")

        policies = DualAllowList.from_env()
        history = HistoryBuffer.from_env()

        async def _on_invoke(message: str, room_id: str) -> tuple[str, Optional[str]]:
            reply, new_sid = await self._invoke_harness(
                message, self._sessions.get(room_id), room_id
            )
            if new_sid:
                self._sessions[room_id] = new_sid
            return reply, new_sid

        relay = MatrixRelay(
            homeserver=homeserver,
            user_id=full_user_id,
            access_token=access_token,
            device_id=device_id,
            policies=policies,
            history=history,
            on_invoke=_on_invoke,
            media_dir=self.config.workspace_dir,
        )

        console.print("[bold green]Matrix relay connected.[/bold green]")
        self._relay_task = asyncio.create_task(relay.run())

        try:
            await self._relay_task
        except asyncio.CancelledError:
            await relay.stop()

    async def _invoke_harness(
        self,
        message: str,
        session_id: Optional[str],
        room_id: str,
    ) -> tuple[str, Optional[str]]:
        logger.info(
            "invoke_harness: room=%s session=%s msg=%s", room_id, session_id, message[:100]
        )

        timeout_seconds = int(os.environ.get("AGENTTEAMS_HARNESS_TIMEOUT_MS", "600000")) / 1000.0

        harness_env = self._harness.env(self.sync.get_config() if self.sync else {})
        merged_env = _subprocess_env(harness_env)

        try:
            argv = self._harness.build_command(
                message, session_id, self.config.workspace_dir
            )

            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=merged_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.config.workspace_dir),
                limit=32 * 1024 * 1024,  # 32MB — handles large image/file tool results
            )

            state: dict = {}

            async def _run() -> str:
                # Drain stderr concurrently to avoid pipe buffer deadlock
                stderr_task = asyncio.create_task(proc.stderr.read())

                # Read stdout line by line as Claude streams
                while True:
                    try:
                        line_bytes = await proc.stdout.readline()
                    except asyncio.LimitOverrunError as exc:
                        logger.warning("claude stream: line exceeded 64KB limit (%d bytes), stopping", exc.consumed)
                        state.setdefault("text_chunks", []).append(
                            "\n> ⚠️ **stream truncated** — response line exceeded 64 KB limit\n"
                        )
                        try:
                            await proc.stdout.read(exc.consumed)
                        except Exception:
                            pass
                        break
                    if not line_bytes:
                        break
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if line:
                        self._harness.process_stream_line(line, state)

                stderr_bytes = await stderr_task
                await proc.wait()
                return stderr_bytes.decode("utf-8", errors="replace")

            stderr_text = await asyncio.wait_for(_run(), timeout=timeout_seconds)

            if stderr_text:
                logger.warning("harness stderr: %s", stderr_text[:500])

            text = "".join(state.get("text_chunks", [])) or "(no response)"
            new_sid = state.get("session_id")

            if new_sid and session_id != new_sid:
                self._save_session(room_id, new_sid)

            return text, new_sid

        except asyncio.TimeoutError:
            logger.error("Harness invocation timed out after %ds", timeout_seconds)
            return "Sorry, the request timed out. Please try again.", session_id
        except Exception as exc:
            logger.error("Harness invocation failed: %s", exc)
            return f"Sorry, an error occurred: {exc}", session_id

    @property
    def _sessions_path(self) -> Path:
        return self._harness_home / "sessions" / "rooms.json"

    def _save_session(self, room_id: str, session_id: str) -> None:
        self._sessions[room_id] = session_id
        path = self._sessions_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._sessions, indent=2, sort_keys=True))
            tmp.replace(path)
        except OSError as exc:
            logger.warning("failed to persist session map: %s", exc)

    def _load_sessions(self) -> Dict[str, str]:
        path = self._sessions_path
        if not path.exists():
            legacy = self._harness_home / "sessions" / "current"
            if legacy.exists():
                logger.info(
                    "ignoring legacy worker-wide session file %s — sessions are "
                    "now tracked per room in %s",
                    legacy,
                    path.name,
                )
            return {}
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not read session map %s: %s", path, exc)
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def _ensure_mc(self) -> None:
        if shutil.which("mc"):
            return
        system = platform.system().lower()
        machine = platform.machine().lower()
        arch_map = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
        arch = arch_map.get(machine, machine)
        if system == "windows":
            url = "https://dl.min.io/client/mc/release/windows-amd64/mc.exe"
            install_dir = Path.home() / ".local" / "bin"
            dest = install_dir / "mc.exe"
        elif system in ("linux", "darwin"):
            url = f"https://dl.min.io/client/mc/release/{system}-{arch}/mc"
            install_dir = Path.home() / ".local" / "bin"
            dest = install_dir / "mc"
        else:
            console.print(f"[yellow]mc auto-install not supported on {system}[/yellow]")
            return

        install_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"[yellow]mc not found, downloading from {url}...[/yellow]")
        try:
            import httpx
            with httpx.stream("GET", url, follow_redirects=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as fp:
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        fp.write(chunk)
            if system != "windows":
                dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            os.environ["PATH"] = str(install_dir) + os.pathsep + os.environ.get("PATH", "")
            console.print(f"[green]mc installed to {dest}[/green]")
        except Exception as exc:
            console.print(f"[yellow]mc auto-install failed: {exc}[/yellow]")

    async def _on_files_pulled(self, pulled_files: list[str]) -> None:
        if self.sync is None:
            return

        has_config_change = "openclaw.json" in pulled_files
        has_skills_change = any(f.startswith("skills/") for f in pulled_files)
        has_persona_change = any(f in pulled_files for f in ("SOUL.md", "AGENTS.md"))

        if has_config_change:
            console.print("[yellow]openclaw.json changed; re-bridging...[/yellow]")
            try:
                openclaw_cfg = self.sync.get_config()
                self._harness.bridge_config(openclaw_cfg, self._harness_home)
                self._load_env_file(self._harness_home / ".env")
                console.print("[green]Re-bridge complete.[/green]")
            except Exception as exc:
                console.print(f"[red]Re-bridge failed: {exc}[/red]")
        elif has_skills_change or has_persona_change:
            # Lightweight refresh: no model/env change, only instructions update.
            console.print("[yellow]Skills/persona changed; refreshing CLAUDE.md and skills...[/yellow]")
            try:
                self._harness._generate_claude_md(self.config.workspace_dir)
                self._harness._sync_skills_dir(self.config.workspace_dir)
                console.print("[green]CLAUDE.md and .claude/skills/ refreshed.[/green]")
            except Exception as exc:
                console.print(f"[red]Skills refresh failed: {exc}[/red]")

    def _matrix_relogin(self, openclaw_cfg: Dict[str, Any]) -> Dict[str, Any]:
        from harness_worker.matrix import matrix_relogin

        if self.sync is None:
            return openclaw_cfg

        password_key = f"{self.sync._prefix}/credentials/matrix/password"
        matrix_password = self.sync._cat(password_key)
        if not matrix_password:
            console.print(
                "[dim]No Matrix password in MinIO; skipping re-login (E2EE may not survive restart).[/dim]"
            )
            return openclaw_cfg

        matrix_password = matrix_password.strip()
        matrix_cfg = openclaw_cfg.get("channels", {}).get("matrix", {})
        homeserver = _port_remap(matrix_cfg.get("homeserver", ""), _is_in_container())
        if not homeserver or not matrix_password:
            return openclaw_cfg

        result = matrix_relogin(homeserver, self.worker_name, matrix_password)
        if result is None:
            console.print(
                "[yellow]Matrix re-login failed — using existing token (E2EE may not work).[/yellow]"
            )
            return openclaw_cfg

        new_token, new_device = result
        openclaw_cfg.setdefault("channels", {}).setdefault("matrix", {})
        openclaw_cfg["channels"]["matrix"]["accessToken"] = new_token
        if new_device:
            openclaw_cfg["channels"]["matrix"]["deviceId"] = new_device

        config_path = self.sync.local_dir / "openclaw.json"
        try:
            with open(config_path, "w", encoding="utf-8") as fp:
                json.dump(openclaw_cfg, fp, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.warning("Failed to persist updated openclaw.json: %s", exc)

        console.print(f"[green]Matrix re-login OK[/green] (device={new_device})")
        return openclaw_cfg

    @staticmethod
    def _apply_matrix_env(openclaw_cfg: Dict[str, Any]) -> None:
        """Mirror hermes bridge.py: export Matrix policy fields as env vars."""
        matrix = openclaw_cfg.get("channels", {}).get("matrix", {})
        if not matrix:
            return
        dm = matrix.get("dm", {})
        mapping = {
            "MATRIX_DM_POLICY": dm.get("policy", "open"),
            "MATRIX_ALLOWED_USERS": ",".join(dm.get("allowFrom") or []),
            "MATRIX_GROUP_POLICY": matrix.get("groupPolicy", "open"),
            "MATRIX_GROUP_ALLOW_FROM": ",".join(matrix.get("groupAllowFrom") or []),
            "MATRIX_HISTORY_LIMIT": str(matrix.get("historyLimit", 50)),
        }
        for key, val in mapping.items():
            if val:
                os.environ[key] = val
            elif key not in os.environ:
                os.environ[key] = val

    @staticmethod
    def _load_env_file(env_path: Path) -> None:
        if not env_path.exists():
            return
        try:
            for raw in env_path.read_text(errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                os.environ[key] = val
        except OSError as exc:
            logger.warning("Could not source %s: %s", env_path, exc)