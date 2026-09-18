#!/bin/bash
# claude-harness-entrypoint.sh - Claude Harness worker container startup
# Reads config from environment variables and launches claude-harness.
#
# Environment variables (set by controller during worker creation):
#   AGENTTEAMS_WORKER_NAME   - Worker name (required)
#   AGENTTEAMS_FS_ENDPOINT   - MinIO endpoint (required in local mode)
#   AGENTTEAMS_FS_ACCESS_KEY - MinIO access key (required in local mode)
#   AGENTTEAMS_FS_SECRET_KEY - MinIO secret key (required in local mode)
#   AGENTTEAMS_RUNTIME       - "aliyun" for cloud mode (uses RRSA/STS via agentteams-env.sh)
#   TZ                       - Timezone (optional)

set -e

# Source shared environment bootstrap (provides ensure_mc_credentials in cloud
# mode, plus AGENTTEAMS_STORAGE_ALIAS / AGENTTEAMS_STORAGE_PREFIX used by
# claude_harness.sync).
source /opt/agentteams/scripts/lib/agentteams-env.sh 2>/dev/null || true

WORKER_NAME="${AGENTTEAMS_WORKER_NAME:?AGENTTEAMS_WORKER_NAME is required}"
# Align with the openclaw worker layout: HOME == workspace == MinIO mirror root.
# The controller injects HOME=/root/agentteams-fs/agents/<WORKER_NAME>; we anchor
# the install dir to its parent so workspace_dir == HOME and the claude-harness home is
# ${HOME}/.claude-harness/.
INSTALL_DIR="${AGENTTEAMS_INSTALL_DIR:-/root/agentteams-fs/agents}"
WORKSPACE="${INSTALL_DIR}/${WORKER_NAME}"

log() {
    echo "[agentteams-claude-harness $(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Set timezone from TZ env var
if [ -n "${TZ}" ] && [ -f "/usr/share/zoneinfo/${TZ}" ]; then
    ln -sf "/usr/share/zoneinfo/${TZ}" /etc/localtime
    echo "${TZ}" > /etc/timezone
    log "Timezone set to ${TZ}"
fi

# ── Credential setup ─────────────────────────────────────────────────────────
# Cloud mode: RRSA/STS credentials via MC_HOST_<alias> (set by ensure_mc_credentials).
# FileSync._ensure_alias() refreshes them and skips `mc alias set`.
# Local mode: explicit FS endpoint/key/secret passed via CLI args.
if [ "${AGENTTEAMS_RUNTIME:-}" = "aliyun" ]; then
    log "Cloud mode: configuring OSS credentials via RRSA..."
    ensure_mc_credentials || { log "ERROR: Failed to obtain OSS credentials"; exit 1; }
    FS_ENDPOINT="https://oss-placeholder.aliyuncs.com"
    FS_ACCESS_KEY="rrsa"
    FS_SECRET_KEY="rrsa"
    FS_BUCKET="${AGENTTEAMS_FS_BUCKET:-agentteams-cloud-storage}"
else
    FS_ENDPOINT="${AGENTTEAMS_FS_ENDPOINT:?AGENTTEAMS_FS_ENDPOINT is required}"
    FS_ACCESS_KEY="${AGENTTEAMS_FS_ACCESS_KEY:?AGENTTEAMS_FS_ACCESS_KEY is required}"
    FS_SECRET_KEY="${AGENTTEAMS_FS_SECRET_KEY:?AGENTTEAMS_FS_SECRET_KEY is required}"
    FS_BUCKET="${AGENTTEAMS_FS_BUCKET:-agentteams-storage}"
fi
log "  FS bucket: ${FS_BUCKET}"

# Workspace == HOME, so ~/skills is the real directory claude_harness syncs from
# MinIO. Mirror the openclaw convention of also exposing it as ~/.agents/skills
# for any tool that walks that legacy path.
mkdir -p "${WORKSPACE}/skills" "${HOME}/.agents"
ln -sfn "${WORKSPACE}/skills" "${HOME}/.agents/skills"

# Background readiness reporter — report ready once claude_harness has finished
# bootstrapping (mirror + bridge) and dropped its readiness marker.
_start_readiness_reporter() {
    [ -z "${AGENTTEAMS_CONTROLLER_URL:-}" ] && return 0

    (
        TIMEOUT=120; ELAPSED=0
        READY_FILE="${WORKSPACE}/.claude-harness/ready"
        while [ "${ELAPSED}" -lt "${TIMEOUT}" ]; do
            if [ -f "${READY_FILE}" ]; then
                break
            fi
            sleep 5; ELAPSED=$((ELAPSED + 5))
        done

        if [ "${ELAPSED}" -ge "${TIMEOUT}" ]; then
            log "WARNING: readiness reporter timed out waiting for ${READY_FILE} after ${TIMEOUT}s"
            exit 1
        fi

        agt worker report-ready
    ) &
    log "Background readiness reporter started (PID: $!)"
}

log "Starting claude-harness: ${WORKER_NAME}"
log "  FS endpoint: ${FS_ENDPOINT}"
log "  Install dir: ${INSTALL_DIR}"

# A stale marker from a previous container would make the reporter fire before
# this process has actually bridged its config.
rm -f "${WORKSPACE}/.claude-harness/ready"

CMD_ARGS=(
    --name "${WORKER_NAME}"
    --fs "${FS_ENDPOINT}"
    --fs-key "${FS_ACCESS_KEY}"
    --fs-secret "${FS_SECRET_KEY}"
    --fs-bucket "${FS_BUCKET}"
    --install-dir "${INSTALL_DIR}"
)

_start_readiness_reporter

exec claude-harness "${CMD_ARGS[@]}"
