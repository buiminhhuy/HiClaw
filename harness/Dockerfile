# ============================================================
# AgentTeams Harness Worker — delegates to Claude Code / Gemini CLI / OpenCode / Codex
# ============================================================
#
# Build args:
#   HIGRESS_REGISTRY            - base image registry for mc + python
#   AGENTTEAMS_CONTROLLER_IMAGE - source for the agt CLI binary
#   APT_MIRROR                  - Debian mirror (default: official)
#   PIP_INDEX_URL               - PyPI mirror (default: official)
#   NPM_REGISTRY                - npm registry (default: official)
#   *_CLI_VERSION               - pinned CLI agent versions (see below)
#   BAZELISK_ARCH                - amd64/arm64; selects the pre-fetched bazelisk-linux-<arch>
#                                   binary the Makefile places in the build context
#
# Build context: ./harness/ plus the named context `shared` -> ./shared/lib
#   docker build --build-context shared=./shared/lib -f harness/Dockerfile ./harness/
# Use `make build-harness-worker` instead of invoking docker build directly —
# it fetches bazelisk-linux-<arch> into ./harness/ before the build (see BAZELISK_ARCH above).
# ============================================================

ARG HIGRESS_REGISTRY=higress-registry.cn-hangzhou.cr.aliyuncs.com
ARG AGENTTEAMS_CONTROLLER_IMAGE=agentteams/agentteams-controller:latest

# ============ Stage 1: mc (MinIO Client) ============
FROM ${HIGRESS_REGISTRY}/higress/mc:20260216 AS mc

# ============ Stage 2: agt CLI (from controller image) ============
FROM ${AGENTTEAMS_CONTROLLER_IMAGE} AS agentteams-controller

# ============ Final Image ============
FROM ${HIGRESS_REGISTRY}/higress/python:3.11-slim

ARG APT_MIRROR=
ARG PIP_INDEX_URL=https://pypi.org/simple/
ARG NPM_REGISTRY=https://registry.npmjs.org/

# CLI agent versions are pinned so image builds are reproducible and the
# JSON / JSONL stream formats the parsers in harness_worker/harness/ depend on
# cannot change under us. Bump deliberately, then re-run the parser tests.
ARG CLAUDE_CODE_VERSION=2.1.235
ARG GEMINI_CLI_VERSION=0.55.1
ARG OPENCODE_VERSION=1.18.18
ARG CODEX_VERSION=0.148.0
ARG MCPORTER_VERSION=0.13.7

RUN if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        libolm-dev \
        libffi-dev \
        curl \
        git \
        openssh-client \
        jq \
        ripgrep \
        tzdata && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Node.js + npm (needed for all CLI agents)
RUN apt-get update && \
    apt-get install -y --no-install-recommends nodejs npm && \
    rm -rf /var/lib/apt/lists/*

# Install all four CLI agents + mcporter via npm (pinned)
RUN npm config set registry "${NPM_REGISTRY}" && \
    npm install -g \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
        "@google/gemini-cli@${GEMINI_CLI_VERSION}" \
        "opencode-ai@${OPENCODE_VERSION}" \
        "@openai/codex@${CODEX_VERSION}" \
        "mcporter@${MCPORTER_VERSION}" && \
    rm -rf /root/.npm

# Install Bazelisk as native binary named `bazel` (no Node.js wrapper overhead).
# Binary is fetched by the Makefile on the host and passed in via build context,
# not curled here: GitHub's TLS chain fails verification from inside the
# BuildKit container on some corporate networks even though the host trusts it.
ARG BAZELISK_ARCH=amd64
COPY bazelisk-linux-${BAZELISK_ARCH} /usr/local/bin/bazel
RUN chmod +x /usr/local/bin/bazel

# Python venv
RUN python3 -m venv /opt/venv/harness
ENV PATH="/opt/venv/harness/bin:$PATH"

# Python runtime deps. Kept in sync with harness/pyproject.toml [project].dependencies;
# listed explicitly so the heavy install layer is not invalidated by src/ changes.
RUN /opt/venv/harness/bin/pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        'typer>=0.12' \
        'rich>=13.0' \
        'httpx>=0.27' \
        'mautrix[encryption]>=0.20' \
        'markdown-it-py>=3.0'

# mc (MinIO Client) — real binary; wrapper installed after shared libs are copied
COPY --from=mc /usr/bin/mc /usr/local/bin/mc.bin

# AgentTeams CLI — for worker/team/human management via controller REST API
COPY --from=agentteams-controller /usr/local/bin/agt /usr/local/bin/agt

# Copy harness-worker source directly to site-packages
COPY src/harness_worker /opt/venv/harness/lib/python3.11/site-packages/harness_worker

# Create entrypoint wrapper script
RUN printf '#!/bin/bash\nexec /opt/venv/harness/bin/python -m harness_worker.cli "$@"\n' \
        > /usr/local/bin/harness-worker && \
    chmod +x /usr/local/bin/harness-worker

# Entrypoint script
COPY --chmod=755 scripts/harness-worker-entrypoint.sh /opt/agentteams/scripts/harness-worker-entrypoint.sh

# Shared environment bootstrap and credential management
# (agentteams-env.sh, oss-credentials.sh) — same named build-context as hermes/copaw.
COPY --from=shared . /opt/agentteams/scripts/lib/

# mc wrapper: auto-refreshes STS credentials before every mc invocation
# (no-op in local mode). harness_worker.sync shells out to `mc`, so it must
# resolve to the wrapper, not the raw binary.
RUN chmod +x /opt/agentteams/scripts/lib/mc-wrapper.sh && \
    ln -sf /opt/agentteams/scripts/lib/mc-wrapper.sh /usr/local/bin/mc

# Workspace lives under the controller-injected HOME=/root/agentteams-fs/agents/<name>
# so the layout matches the openclaw worker (HOME == workspace == MinIO mirror
# root). The per-worker subdir is created by the entrypoint at startup.
RUN mkdir -p /root/agentteams-fs/agents

WORKDIR /root/agentteams-fs/agents

ENTRYPOINT ["/opt/agentteams/scripts/harness-worker-entrypoint.sh"]
