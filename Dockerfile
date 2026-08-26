FROM node:24-bookworm-slim AS webui-builder

WORKDIR /app
COPY webui/package.json webui/package-lock.json ./webui/
WORKDIR /app/webui
RUN npm ci
COPY webui/ ./
RUN mkdir -p /app/nanoinfra/web && npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl git bubblewrap openssh-client libmagic1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Keep the runtime environment writable by the non-root nanoinfra user. Enabled
# channels may install their manifest-declared dependencies at startup.
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN uv venv --seed "$VIRTUAL_ENV"

# Install Python dependencies first (cached layer). Hatch reads the custom build
# hook from hatch_build.py even for this metadata-only install.
ARG NANOINFRA_EXTRAS=
COPY pyproject.toml README.md README_PYPI.md LICENSE THIRD_PARTY_NOTICES.md hatch_build.py ./
RUN mkdir -p nanoinfra && touch nanoinfra/__init__.py && \
    if [ -n "$NANOINFRA_EXTRAS" ]; then \
        NANOINFRA_SKIP_WEBUI_BUILD=1 uv pip install \
            --python "$VIRTUAL_ENV/bin/python" --no-cache ".[${NANOINFRA_EXTRAS}]"; \
    else \
        NANOINFRA_SKIP_WEBUI_BUILD=1 uv pip install \
            --python "$VIRTUAL_ENV/bin/python" --no-cache .; \
    fi && \
    rm -rf nanoinfra

# Copy the full source and install
COPY nanoinfra/ nanoinfra/
COPY scripts/install_channel_dependencies.py scripts/
COPY --from=webui-builder /app/nanoinfra/web/dist/ nanoinfra/web/dist/
RUN NANOINFRA_SKIP_WEBUI_BUILD=1 uv pip install --python "$VIRTUAL_ENV/bin/python" --no-cache .

# Preinstall selected channel dependencies from their manifests. A comma-separated
# list keeps the image configurable while preserving WhatsApp in the default image.
ARG NANOINFRA_CHANNELS=whatsapp
RUN for channel in $(printf '%s' "$NANOINFRA_CHANNELS" | tr ',' ' '); do \
        python -m scripts.install_channel_dependencies "$channel"; \
    done

# GitHub's MCP server as a binary, not as `docker run`.
#
# Its published configuration starts a container, which cannot work here: this image holds no
# docker client, and mounting the host's socket to give it one would hand this container root on
# the host. The server is a single static Go binary, so the image carries it and the config runs
# `github-mcp-server stdio` directly.
#
# /usr/local/bin is deliberate: the MCP host is Landlock-confined and that directory is already in
# its exec policy (`gates/confinement.py::_SYSTEM_BIN_PATHS`), so the child starts under the same
# rules as any other stdio server rather than needing a rule of its own.
#
# Pinned with its published checksum. A release that moves under us is a supply-chain change, and
# it should fail the build rather than ship quietly. Build with
# `--build-arg GITHUB_MCP_SERVER_VERSION=` to leave it out.
ARG GITHUB_MCP_SERVER_VERSION=1.11.0
ARG GITHUB_MCP_SERVER_SHA256=3b73bb7be0c8b043f861e90410df8ebdfc71b83128c54ced75fb32c4ff697fc5
RUN set -eu; \
    if [ -n "$GITHUB_MCP_SERVER_VERSION" ]; then \
        arch="$(uname -m)"; \
        case "$arch" in \
            x86_64) asset="Linux_x86_64"; sha="$GITHUB_MCP_SERVER_SHA256" ;; \
            aarch64|arm64) asset="Linux_arm64"; sha="3f7615254f6b619469c471c5d275029299ff7431c93d6075496ea4b2eec020cb" ;; \
            *) echo "no github-mcp-server build for $arch; skipping" >&2; asset="" ;; \
        esac; \
        if [ -n "$asset" ]; then \
            url="https://github.com/github/github-mcp-server/releases/download/v${GITHUB_MCP_SERVER_VERSION}/github-mcp-server_${asset}.tar.gz"; \
            curl -fsSL "$url" -o /tmp/gh-mcp.tar.gz; \
            echo "$sha  /tmp/gh-mcp.tar.gz" | sha256sum -c -; \
            tar -xzf /tmp/gh-mcp.tar.gz -C /tmp github-mcp-server; \
            install -m 0755 /tmp/github-mcp-server /usr/local/bin/github-mcp-server; \
            rm -f /tmp/gh-mcp.tar.gz /tmp/github-mcp-server; \
            /usr/local/bin/github-mcp-server --version; \
        fi; \
    fi

# Two accounts, because item 15 (nanoinfraorg/nanoinfra#18) splits the agent from
# the executor. Two processes on one uid give no kernel-enforced separation: either
# one can ptrace the other and read its memory, so the credential the executor holds
# would still be reachable from the agent. Separate uids make the kernel refuse that.
#
#   nanoinfra       the agent. Owns the data dir and the writable virtualenv.
#   nanoinfra-exec  the executor. Holds the plaintext credentials and writes the
#                   audit log. No shell and no home of its own: it never logs in,
#                   and its state lives in the shared data dir.
#   nanoinfra-fetch the fetcher (item 16, nanoinfraorg/nanoinfra#19). web_fetch and
#                   web_search run there, so untrusted web content enters this account.
#                   It holds no credential and reaches no inventory host.
#   nanoinfra-ipc   the executor's group. Its only members are the agent and the
#                   executor, and its only job is the executor socket directory (see
#                   entrypoint.sh). It carries no read rights on either account's files.
#   nanoinfra-mcp   the stdio MCP host (item 20, nanoinfraorg/nanoinfra#22). It starts
#                   the MCP servers the config names, so it holds the exec right the
#                   fetcher refuses. It holds no credential and reaches no inventory host.
#   nanoinfra-mcp-ipc  the MCP host's group, for the same reason the fetcher has its own.
#   nanoinfra-op    the operator socket group (item 36, nanoinfraorg/nanoinfra#38). The
#                   executor suspends an action that needs an approval, and an operator
#                   answers on a second socket. The agent joins this group, because the
#                   #27 inbox answers from inside the gateway process. That is the one
#                   place where this layout gives up the filesystem half of the split, and
#                   it is deliberate: the answer still crosses into the executor, and the
#                   executor still matches the actor against gates.approvers. No other
#                   helper joins it, or that helper could approve an action it asked for.
#   nanoinfra-fetch-ipc  the fetcher's group, and the reason there are several groups. A
#                   member of nanoinfra-ipc traverses the executor's socket directory and
#                   connects to its socket. The fetcher inside that group could therefore
#                   run a command on every inventory host, which is the one thing this
#                   split exists to prevent. The agent belongs to both groups. Neither
#                   helper belongs to the other's group.
#
# WARNING, and it is deliberate that this is written down. /app/.venv stays writable
# by the agent, because an enabled channel may install its declared dependencies at
# startup. The executor imports code from that same virtualenv. So the agent account
# can still place code that the executor account later runs. That is a code-injection
# path from the low-privilege side to the high-privilege side, and no uid split closes
# it. A fully enforced split needs the executor on its own read-only interpreter.
RUN useradd -m -u 1000 -s /bin/bash nanoinfra && \
    groupadd --system nanoinfra-ipc && \
    groupadd --system nanoinfra-fetch-ipc && \
    groupadd --system nanoinfra-mcp-ipc && \
    groupadd --system nanoinfra-op && \
    useradd --system --uid 1001 --user-group --no-create-home \
        --home-dir /home/nanoinfra --shell /usr/sbin/nologin nanoinfra-exec && \
    useradd --system --uid 1002 --user-group --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin nanoinfra-fetch && \
    usermod --append --groups nanoinfra-ipc nanoinfra && \
    usermod --append --groups nanoinfra-ipc nanoinfra-exec && \
    useradd --system --uid 1003 --user-group --no-create-home \
        --home-dir /home/nanoinfra --shell /usr/sbin/nologin nanoinfra-mcp && \
    usermod --append --groups nanoinfra-fetch-ipc nanoinfra && \
    usermod --append --groups nanoinfra-fetch-ipc nanoinfra-fetch && \
    usermod --append --groups nanoinfra-mcp-ipc nanoinfra && \
    usermod --append --groups nanoinfra-mcp-ipc nanoinfra-mcp && \
    usermod --append --groups nanoinfra-op nanoinfra && \
    mkdir -p /home/nanoinfra/.nanoinfra && \
    chown -R nanoinfra:nanoinfra /home/nanoinfra /app/.venv

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

# Start as root so the entrypoint can chown the data dir (a freshly-mounted volume
# or bind mount arrives root-owned) before dropping to the non-root nanoinfra user
# via setpriv. The entrypoint drops privileges on every root start
# and fails closed if it cannot, so the agent never runs as root (see
# entrypoint.sh).
#
# The root start is also what makes the #18 split enforceable. Only a process with
# CAP_SETUID can place the executor on one account and the agent on another, so the
# entrypoint is the supervisor for this image: it starts the executor as
# nanoinfra-exec, then it execs the agent as nanoinfra. A start that is already
# non-root cannot do that, and the entrypoint says so in the log.
#
# Item 17 (nanoinfraorg/nanoinfra#20) adds one confinement layer per helper process.
# The layer is Landlock, and it needs no root and no capability. It needs three
# syscalls: landlock_create_ruleset, landlock_add_rule, and landlock_restrict_self.
# A container runtime with a seccomp profile that predates Landlock answers the
# first one with EPERM, and the entrypoint then starts each helper with a warning
# rather than a refusal. Docker allows the three syscalls since 20.10.13. So a
# current runtime needs no --security-opt of any kind for this image.
#
# The layer also needs a kernel with CONFIG_SECURITY_LANDLOCK and Landlock in the
# lsm list. The startup echo names the ABI version the kernel reported, or it names
# the absence of Landlock support. An operator reads which one they have.
USER root
ENV HOME=/home/nanoinfra
# Ensure crash output reaches the container logs (app output is otherwise swallowed
# on non-graceful exit).
ENV PYTHONUNBUFFERED=1 PYTHONFAULTHANDLER=1

# Gateway health endpoint and optional WebUI/WebSocket channel ports
EXPOSE 18790 8765

ENTRYPOINT ["entrypoint.sh"]
CMD ["status"]
