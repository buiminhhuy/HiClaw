# Harness Worker

`harness-worker` is an AgentTeams worker runtime, delegating the agent loop to an external CLI tool (Claude Code, Gemini CLI, OpenCode, Codex) instead of running a gateway in-process.

## Supported CLIs

| Harness | CLI | Session resume | Output format |
|---------|-----|----------------|---------------|
| `claude` | `claude -p … --output-format stream-json --verbose` | `--resume <session-id>` | stream-json (JSONL) |
| `gemini` | `gemini --prompt … --yolo --output-format json` | *(single-turn)* | json |
| `opencode` | `opencode run … --format json --dangerously-skip-permissions` | `--session <id>` | json |
| `codex` | `codex exec … --json --ephemeral` | `codex exec resume --last` | jsonl |

## Architecture

```
Manager (OpenClaw/CoPaw)
    │ openclaw.json
    ▼ (Matrix + MinIO)
Worker Pod (runtime=harness, AGENTTEAMS_HARNESS_TYPE=claude|gemini|opencode|codex)
    ├── FileSync:      MinIO ↔ /root/agentteams-fs/agents/<name>  (harness_worker.sync)
    ├── Bridge:        openclaw.json → native CLI config files
    ├── Matrix relay:  mautrix + harness_worker.policies
    │       ▼ inbound Matrix message
    │   asyncio.create_subprocess_exec(<harness-cli> …)
    │       ▼ stdout (stream-json/json/jsonl), line by line
    │   process_stream_line → reply text + session_id
    │       ▲ send reply (HTML-formatted) to Matrix room
    └── Background:    sync_loop + push_loop
```

**Key design decisions:**

- **Request/response model** — each Matrix message spawns one CLI subprocess; no persistent PTY.
- **`--resume <session-id>`** — Claude harness maintains worker-wide session state across messages and pod restarts.
- **Vendored storage/Matrix layer** — `sync.py` and `policies.py` are vendored from `hermes/src/hermes_worker/sync.py` and `hermes/src/hermes_matrix/policies.py`; `sync.py` adds a `runtime_home_dir` parameter (`.harness`). Re-vendor them when picking up upstream storage fixes instead of editing hermes.

## Package structure

```
harness/src/harness_worker/
├── cli.py             # Typer CLI (--harness-type flag)
├── config.py          # WorkerConfig
├── worker.py          # Bootstrap: start → sync → Matrix relay → _invoke_harness
├── bridge.py          # openclaw.json → CLAUDE_HOME, harness-home layout
├── remote_worker.py   # RemoteWorker (local developer environment)
├── remote_cli.py      # `harness-remote` CLI
├── matrix_relay.py    # Thin adapter over harness_worker.matrix.MautrixRelay
├── sync.py            # FileSync, push_loop, sync_loop (vendored from hermes)
├── policies.py        # DualAllowList, HistoryBuffer, apply_outbound_mentions (vendored from hermes)
├── matrix.py          # MautrixRelay (mautrix-based Matrix client + HTML formatter)
└── harness/
    ├── base.py        # BaseHarness ABC
    ├── claude.py      # ClaudeHarness (primary, full-featured)
    ├── gemini.py      # GeminiHarness
    ├── opencode.py    # OpenCodeHarness
    └── codex.py       # CodexHarness
```

## Components

### `BaseHarness`

Abstract base class in [harness/base.py](../harness/src/harness_worker/harness/base.py). All adapters implement:

| Method | Purpose |
|--------|---------|
| `bridge_config(cfg, harness_home)` | Write `settings.json`, generate `CLAUDE.md`, sync `.claude/skills/` symlinks, seed `mcpServers` |
| `build_command(message, session_id, workspace)` | Build `argv` for one non-interactive CLI invocation |
| `process_stream_line(line, state)` | Parse one JSONL line from streaming stdout (mutates `state`) |
| `parse_output(stdout_bytes)` | Full-output parse; returns `(text, session_id)` |
| `env(openclaw_cfg)` | Return per-harness auth env vars merged into subprocess environment |

Harnesses register via `@register_harness("name")`; the factory `build_harness(name)` looks up the registry.

### `Worker`

Bootstrap in [worker.py](../harness/src/harness_worker/worker.py):

1. Downloads all files from MinIO (`FileSync.mirror_all`).
2. Reads `openclaw.json` and re-authenticates the Matrix session.
3. Calls `harness.bridge_config(openclaw_cfg, harness_home)` to write native config.
4. Starts background `sync_loop` + `push_loop` tasks.
5. Enters `_run_matrix_relay()`: subscribes to Matrix and invokes harness per message.

### `MatrixRelay`

Thin adapter over `harness_worker.matrix.MautrixRelay`. On each inbound message:

1. Skips own messages and replayed history (events before startup timestamp).
2. Evaluates `DualAllowList.permits(sender, is_dm)`.
3. Drains `HistoryBuffer` for non-DM rooms (provides context window).
4. Calls `on_invoke(full_message)` → `_invoke_harness(message, session_id)`.
5. Applies `apply_outbound_mentions` (MSC3952 compliance) and sends reply as HTML.

## Worker._invoke_harness

File: [harness/src/harness_worker/worker.py](../harness/src/harness_worker/worker.py)

```python
proc = await asyncio.create_subprocess_exec(
    *argv, env=merged_env,
    stdout=PIPE, stderr=PIPE,
    cwd=str(workspace_dir),
)
# Read stdout line by line as the CLI streams — do NOT use communicate()
while True:
    line_bytes = await proc.stdout.readline()
    if not line_bytes:
        break
    self._harness.process_stream_line(line.strip(), state)

text = "".join(state.get("text_chunks", [])) or "(no response)"
new_sid = state.get("session_id")
```

Default timeout: `AGENTTEAMS_HARNESS_TIMEOUT_MS=600000` (10 minutes).

If a single JSON line from `claude --output-format stream-json` exceeds the 64 KB asyncio buffer limit, the worker catches `asyncio.LimitOverrunError`, appends a truncation warning to the reply, drains the buffer, and breaks — rather than crashing.

## ClaudeHarness — stream-json format

File: [harness/src/harness_worker/harness/claude.py](../harness/src/harness_worker/harness/claude.py)

### Event format

`claude --output-format stream-json --verbose` emits wrapped events:

```jsonc
{"type": "system",    "subtype": "init",    "session_id": "abc123"}
{"type": "assistant", "message": {"content": [
    {"type": "text",     "text": "I will check…"},
    {"type": "tool_use", "name": "Bash", "input": {"command": "ls /tmp"}}
]}, "session_id": "abc123"}
{"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "…", "content": "file1.txt", "is_error": false}
]}, "session_id": "abc123"}
{"type": "result", "subtype": "success", "result": "…",
    "session_id": "abc123", "duration_ms": 4210, "num_turns": 2,
    "usage": {"input_tokens": 1205, "output_tokens": 342}}
```

### process_stream_line — event handling

`process_stream_line(line, state)` is called for each stdout line:

| Event type | Action | Log |
|------------|--------|-----|
| `system/init` | Save `session_id` to state | `claude session init: <id>` |
| `assistant` / text block | Accumulate into `state["text_chunks"]` | — |
| `assistant` / tool_use | `_log_tool_use()`: log + append formatted line to chat (subject to cap) | per-tool format |
| `user` / tool_result | Append success/error line to chat (subject to cap) | `claude tool_result: <preview>` |
| `result` | Append overflow marker + stats footer; fallback text if no chunks | `claude result: input_tokens=… output_tokens=… duration=…ms turns=…` |
| `content_block_start` (SSE fallback) | Initialise accumulator in `state["active_tools"][idx]` | `claude tool start: <name>` |
| `content_block_delta / input_json_delta` (SSE) | Accumulate JSON fragments | *(silent)* |
| `content_block_stop` (SSE) | Join + parse fragments → `_log_tool_use` | per-tool format |

### Tool activity cap

`_MAX_ACTIVITY_LINES = 20` limits how many tool lines appear in the Matrix chat reply:

- Up to 20 tool_use/tool_result lines are shown verbatim.
- If exceeded: `> _… +N more tool calls (see pod logs)_` is inserted **before** the stats footer.
- The stats footer always appears: `> 📊 **in/out** N/N tok · ⏱ Xs · N turns · N calls`
- Pod logs (`logger.info/warning`) capture every tool call regardless of the cap.

### Tool format dispatch (`_format_tool_ui`)

| Tool | Chat display |
|------|-------------|
| `Bash` | `🖥️ **Bash**: \`<command>\`` (truncated at 120 chars, newlines → ` ↵ `) |
| `Read` | `📖 **Read**: <path>` |
| `Edit` / `MultiEdit` | `✏️ **Edit**: <path>` |
| `Write` | `📝 **Write**: <path>` |
| `Glob` / `Grep` | `🔍 **Glob**: <pattern>` / `🔍 **Grep**: <pattern>` |
| `WebSearch` / `WebFetch` / `Fetch` | `🌐 **WebSearch**: <query>` |
| `TodoWrite` | `📋 **TodoWrite**: N items` |
| `AskUser` | `❓ **AskUser**: <question>` |
| `Task` | `🤖 **Task**: <description>` |
| `mcp__*` | `🔌 **MCP** <server>: <first-arg>` |
| other | `⚙️ **<Name>**: <args>` |

## Per-harness CLI details

### Claude (`claude`)

| Setting | Value |
|---------|-------|
| Non-interactive flag | `claude -p "<message>"` |
| Session resume | `--resume <session-id>` |
| Output format | `--output-format stream-json --verbose` |
| Model flag | `--model <model-id>` |
| Config file | `<workspace>/.claude/settings.json` |
| MCP servers | `<workspace>/.claude.json` → `projects[cwd]["mcpServers"]` |
| Project instructions | `<workspace>/CLAUDE.md` (generated from `SOUL.md` + `AGENTS.md`) |
| Skills | `<workspace>/.claude/skills/<name>/` (symlinked from `workspace/skills/`) |
| Permissions | `dontAsk` with `allow: ["mcp__*"]` for native MCP tool calls |

**bridge_config merge order** (later wins):

```
1. Existing settings.json on disk          (user customisations survive restarts)
2. .harness/claude.settings.json           (per-worker MinIO override)
3. Controller-managed fields (always win):
     model, permissions (dontAsk + allow mcp__*), env (ANTHROPIC_*, timeouts)
```

**CLAUDE.md generation** — reads `workspace/SOUL.md` and `workspace/AGENTS.md` (synced from MinIO) and writes `workspace/CLAUDE.md`. Claude CLI reads this as project instructions automatically.

**Skills symlinks** — mirrors `workspace/skills/<name>/` → `workspace/.claude/skills/<name>/` as symlinks. Stale symlinks for removed skills are cleaned up; non-symlink directories are left untouched.

**MCP servers** — reads `workspace/config/mcporter.json` (generated by the controller from `spec.mcpServers`) and writes into `workspace/.claude.json` under `projects[cwd]["mcpServers"]`. HTTP, SSE, and stdio transports are supported:

```json
{
  "projects": {
    "/root/agentteams-fs/agents/<worker-name>": {
      "mcpServers": {
        "deepwiki": { "type": "http",  "url": "https://mcp.deepwiki.com/mcp" },
        "github":   { "type": "sse",   "url": "https://mcp.github.com/sse"  },
        "my-tool":  { "type": "stdio", "command": "python3", "args": ["/opt/mcp/server.py"] }
      }
    }
  }
}
```

Entries from `config/mcporter.json` are fully controller-owned (stale entries replaced on every bridge run). Entries from `.harness/mcp-local.json` are merged after and win on name collision. Existing `.claude.json` content is preserved.

**Stdio MCP server override:** drop `.harness/mcp-local.json` in the worker's MinIO path:

```json
{
  "mcpServers": {
    "my-tool": {
      "transport": "stdio",
      "command": "python3",
      "args": ["/root/agentteams-fs/agents/<worker>/.harness/my_server.py"]
    }
  }
}
```

**`.claudeignore`** — drop `.harness/claudeignore` in MinIO to control which files Claude Code ignores. If absent, a default is written (ignores `.harness/`, `.claude/`, `*.tar`, `*.log`).

**Hot-reload** — `_on_files_pulled` detects three change categories:

| Changed files | Action |
|---|---|
| `openclaw.json` | Full re-bridge (model + env + settings.json + CLAUDE.md + skills + .claudeignore) |
| `SOUL.md` or `AGENTS.md` | Lightweight: regenerate `CLAUDE.md` only |
| `skills/*` | Lightweight: re-sync `.claude/skills/` symlinks only |

### Gemini (`gemini`)

| Setting | Value |
|---------|-------|
| Non-interactive flag | `gemini --prompt "<message>" --yolo` |
| Session resume | Not supported — single-turn only |
| Output format | `--output-format json` |
| Config file | `~/.gemini/settings.json` |
| Required env | `GEMINI_API_KEY` or `GOOGLE_API_KEY` |

### OpenCode (`opencode`)

| Setting | Value |
|---------|-------|
| Non-interactive flag | `opencode run "<message>" --format json --dangerously-skip-permissions` |
| Session resume | `--session <id>` or `--continue` |
| Config file | `~/.config/opencode/opencode.json` |

### Codex (`codex`)

| Setting | Value |
|---------|-------|
| Non-interactive flag | `codex exec "<message>" --json --ephemeral --sandbox workspace-write` |
| Session resume | `codex exec resume --last "<message>"` |
| Output format | JSONL |
| Required env | `CODEX_API_KEY` or `OPENAI_API_KEY` |

## LLM routing via Higress

Higress ai-proxy 2.0 uses **auto-protocol detection** — it inspects the request path to determine the wire format automatically:

| Client path | Detected protocol | Upstream |
|---|---|---|
| `/v1/chat/completions` | OpenAI | pass-through |
| `/v1/messages` | Anthropic (Claude) | converted to OpenAI |

Claude CLI always sends to `ANTHROPIC_BASE_URL + /v1/messages`. Setting `ANTHROPIC_BASE_URL` to the bare Higress gateway URL is sufficient — no `/anthropic` suffix needed.

**Credential priority** (resolved at `bridge_config` time):

1. `AGENTTEAMS_CLAUDE_BASE_URL` + `AGENTTEAMS_LLM_API_KEY` — explicit operator override
2. `AGENTTEAMS_AI_GATEWAY_URL` + `AGENTTEAMS_WORKER_GATEWAY_KEY` — default in-cluster (injected by controller into every worker pod)
3. `_DEFAULT_BASE_URL` + `_DEFAULT_API_KEY` — local dev fallback

**Model constraint:** the model name in the request body must match a Higress AI route `modelPredicate`. Model is read from `openclaw.json → agents.defaults.model.primary` (format `"agentteams-gateway/MiniMax-M2"` → `"MiniMax-M2"`). If no matching predicate exists, the gateway returns 404.

## Bridge (`bridge_config`)

On startup, `ClaudeHarness.bridge_config(openclaw_cfg, harness_home)` writes:

| File | Content |
|------|---------|
| `workspace/.claude/settings.json` | model, permissions (`dontAsk`), env vars |
| `workspace/.claude.json` | MCP servers (from `config/mcporter.json` or `.harness/mcp-local.json`) |
| `workspace/CLAUDE.md` | Concatenation of `SOUL.md` + `AGENTS.md` |
| `workspace/.claude/skills/` | Symlinks to `workspace/skills/` |
| `workspace/.claudeignore` | From `.harness/claudeignore` or default |
| `workspace/memory/` | Auto-created so Claude Code's auto-memory feature can write here |

## Session continuity

Session state is tracked **per Matrix room** and persisted to
`<harness_home>/sessions/rooms.json` as a `{room_id: session_id}` map:

- `_save_session(room_id, sid)` writes after every successful CLI invocation.
- `_load_sessions()` is called at startup — pod restarts resume each room's conversation automatically.
- `--resume <session-id>` is appended to the `claude -p` argv when that room has a session.

A single worker-wide session id would splice unrelated rooms into one
conversation, so the legacy `sessions/current` file is ignored on startup.

## Matrix reply formatting

Outbound replies are sent as `org.matrix.custom.html` with `formatted_body` generated by `harness_worker.matrix._to_html()`:

- `<think>…</think>` blocks → `<blockquote>💭 …</blockquote>` (Element.io does not render `<details>`)
- Markdown (bold, blockquote, inline code, links) → HTML via `markdown-it-py`
- Fallback to regex-based conversion if `markdown-it-py` is absent at runtime

## Worker CRD spec

```yaml
apiVersion: agentteams.io/v1beta1
kind: Worker
metadata:
  name: my-claude-worker
spec:
  runtime: harness
  model: MiniMax-M2          # must match a Higress AI route modelPredicate
  env:
    # claude | gemini | opencode | codex  (default: claude).
    # Deliberately not a CRD field: the CLI variant is runtime-local, so it
    # rides on spec.env. mergeUserEnv only drops keys the controller itself
    # sets, and AGENTTEAMS_HARNESS_TYPE is not one of them.
    AGENTTEAMS_HARNESS_TYPE: claude
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      cpu: "2"
      memory: 2Gi
```

A Team references existing Worker CRs — it no longer inlines worker specs:

```yaml
apiVersion: agentteams.io/v1beta1
kind: Team
metadata:
  name: my-team
spec:
  workerMembers:
    - name: my-claude-worker
      role: worker
```

### Remote worker (developer machine)

Set `containerManaged: false` so the controller provisions the Matrix identity,
rooms and storage but never creates a pod; the process is started locally with
`harness-remote`:

```yaml
apiVersion: agentteams.io/v1beta1
kind: Worker
metadata:
  name: dev-laptop
spec:
  runtime: harness
  model: MiniMax-M2
  containerManaged: false
```

## Filesystem layout

```
/root/agentteams-fs/agents/<worker-name>/          ← workspace_dir (synced from MinIO)
├── openclaw.json                               ← agent configuration (Manager-managed)
├── SOUL.md                                     ← agent persona / values (Manager-managed)
├── AGENTS.md                                   ← agent behaviour rules (Manager-managed)
├── CLAUDE.md                                   ← generated by bridge from SOUL.md + AGENTS.md
├── .claudeignore                               ← generated by bridge from .harness/claudeignore
├── .claude.json                                ← generated by bridge (project-level MCP servers)
├── config/
│   └── mcporter.json                           ← MCP server list HTTP/SSE (Manager-managed)
├── skills/                                     ← skill files synced from MinIO
│   └── <skill-name>/
│       └── SKILL.md
├── memory/                                     ← Claude Code auto-memory (worker-managed, pushed to MinIO)
├── .claude/
│   ├── settings.json                           ← generated by bridge_config
│   └── skills/
│       └── <skill-name> → …/skills/<skill-name>  ← absolute symlink
└── .harness/                                   ← harness_home (not synced to MinIO)
    ├── ready                                   ← touched when relay is up (readiness probe)
    ├── claude.settings.json                    ← optional settings override (deep-merged before controller fields)
    ├── mcp-local.json                          ← optional stdio/HTTP MCP servers
    ├── claudeignore                            ← optional .claudeignore source
    └── sessions/
        └── current                             ← last Claude session-id
```

**Ownership:**
- **Manager-managed (read-only in worker):** `openclaw.json`, `SOUL.md`, `AGENTS.md`, `config/mcporter.json`, `skills/`
- **Bridge-generated (derived, not pushed to MinIO):** `CLAUDE.md`, `.claudeignore`, `.claude.json`, `.claude/settings.json`, `.claude/skills/` symlinks
- **Worker-managed (pushed to MinIO):** `memory/`, `MEMORY.md`, `.harness/sessions/`
- **Harness-local overrides (in MinIO, not pushed back):** `.harness/claude.settings.json`, `.harness/mcp-local.json`, `.harness/claudeignore`

## Environment variables

### Required (injected by controller)

| Variable | Description |
|----------|-------------|
| `AGENTTEAMS_WORKER_NAME` | Worker identity |
| `AGENTTEAMS_FS_ENDPOINT` | MinIO endpoint |
| `AGENTTEAMS_FS_ACCESS_KEY` | MinIO access key |
| `AGENTTEAMS_FS_SECRET_KEY` | MinIO secret key |
| `AGENTTEAMS_AI_GATEWAY_URL` | Higress gateway base URL |
| `AGENTTEAMS_WORKER_GATEWAY_KEY` | Per-worker Higress consumer key |
| `AGENTTEAMS_MATRIX_DOMAIN` | Matrix server domain |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTTEAMS_FS_BUCKET` | `agentteams-storage` | MinIO bucket |
| `AGENTTEAMS_INSTALL_DIR` | `/root/agentteams-fs/agents` | Workspace root |
| `AGENTTEAMS_HARNESS_TYPE` | `claude` | CLI variant: `claude\|gemini\|opencode\|codex` |
| `AGENTTEAMS_HARNESS_TIMEOUT_MS` | `600000` | Per-invocation timeout (ms) |
| `AGENTTEAMS_CLAUDE_BASE_URL` | — | Explicit LLM base URL (overrides gateway) |
| `AGENTTEAMS_LLM_API_KEY` | — | Explicit LLM API key (overrides gateway key) |
| `AGENTTEAMS_USE_CLAUDE_SUBSCRIPTION` | — | `1` to use `claude login` OAuth instead of the gateway |
| `AGENTTEAMS_MATRIX_URL` | — | Override the Matrix homeserver URL (remote workers / port-forward) |
| `AGENTTEAMS_SYNC_INTERVAL` | `300` | MinIO pull interval (seconds) |

Only the "required" block above is injected by the controller. Everything under
"optional" is worker-local: set it in `Worker.spec.env`, or pass the matching
`harness-remote` flag when running outside the cluster.

## Adding a new model

1. Create a Higress AI route with the new `modelPredicate` (via Higress console or API).
2. Update the worker's Team CR:
   ```yaml
   spec:
     workers:
       - name: dev-1
         runtime: harness
         model: MiniMax-M2.7
   ```
3. The harness reads `agents.defaults.model.primary` from `openclaw.json` and passes it directly to `claude --model` and every API request. No image rebuild required.

## Deployment in our k8s cluster

```bash
# Build (from the repo root — the controller image is a base stage for harness)
make build-harness-worker VERSION=<VER> DOCKER_PLATFORM=linux/amd64 \
  REGISTRY=<registry> REPO=<repo> \
  HIGRESS_REGISTRY=<higress-registry>

# Push via crane (avoids Docker Desktop VM ↔ host network limitations)
docker tag agentteams/harness-worker:<VER> <registry>/<repo>/agentteams-harness-worker:<VER>
docker save <registry>/<repo>/agentteams-harness-worker:<VER> -o /tmp/harness.tar
crane push --insecure /tmp/harness.tar <registry>/<repo>/agentteams-harness-worker:<VER>

# Point the controller at the image. The harness image is opt-in — the installer
# leaves AGENTTEAMS_HARNESS_WORKER_IMAGE empty unless it is set explicitly.
#   installer:  AGENTTEAMS_INSTALL_HARNESS_WORKER_IMAGE=<registry>/<repo>/agentteams-harness-worker:<VER>
#   helm:       --set worker.defaultImage.harness.repository=<registry>/<repo>/agentteams-harness-worker \
#               --set worker.defaultImage.harness.tag=<VER>

# Tail tool-use logs in real time
kubectl logs -n <namespace> -l agentteams.io/runtime=harness -f

# Rolling update after image push (patch the Worker CR, then bounce the pod)
kubectl patch worker <worker-name> -n <namespace> --type=merge \
  -p='{"spec":{"image":"<registry>/<repo>/agentteams-harness-worker:<VER>"}}'
kubectl delete pod agentteams-worker-<worker-name> -n <namespace>
```

## Troubleshooting

### Pod logs show `model=... url=http://higress-gateway...`

Expected — confirms the harness is routing through the Higress gateway:

```
bridge: claude settings → /root/agentteams-fs/agents/dev-1/.claude/settings.json
  (model=MiniMax-M2, url=http://higress-gateway.<namespace>.svc.cluster.local:80)
```

### 404 from gateway

The model name does not match any Higress AI route `modelPredicate`. Check existing routes in the Higress console and align the Team CR `model` field.

### Worker ignores Matrix messages

Check DM / group policy env vars:

```bash
kubectl exec -n <namespace> agentteams-worker-<name> -- env | grep MATRIX
```

### Claude CLI returns `(no response)`

- Verify `ANTHROPIC_BASE_URL` is set to the gateway URL (not a direct Anthropic endpoint).
- Confirm `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` matches `AGENTTEAMS_WORKER_GATEWAY_KEY`.
- Test the route directly:
  ```bash
  curl -s -X POST http://higress-gateway.<namespace>.svc.cluster.local:80/v1/messages \
    -H "Authorization: Bearer <gateway-key>" \
    -H "Content-Type: application/json" \
    -d '{"model":"MiniMax-M2","max_tokens":64,"messages":[{"role":"user","content":"hi"}]}'
  ```

### MinIO sync fails at startup

Verify MinIO credentials and that the worker's bucket/prefix exists. The controller creates the MinIO user and bucket policy when the Worker CR is reconciled.
