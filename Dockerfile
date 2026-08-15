FROM node:24-bookworm-slim AS webui-builder

WORKDIR /app
COPY webui/package.json webui/package-lock.json ./webui/
WORKDIR /app/webui
RUN npm ci
COPY webui/ ./
RUN mkdir -p /app/nanoinfra/web && npm run build

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates git bubblewrap openssh-client libmagic1 && \
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

# Render deploy template (see render.yaml): committed gateway config that wires
# secrets through ${ANTHROPIC_API_KEY} / ${NANOINFRA_WEB_TOKEN} env vars (resolved
# at startup). Lives in the code dir (/app), not the data dir, so a mounted disk
# won't shadow it. Only used when RENDER=true; ignored by local runs.
COPY render-config.json ./

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
#   nanoinfra-fetch-ipc  the fetcher's group, and the reason there are two groups. A
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
    useradd --system --uid 1001 --user-group --no-create-home \
        --home-dir /home/nanoinfra --shell /usr/sbin/nologin nanoinfra-exec && \
    useradd --system --uid 1002 --user-group --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin nanoinfra-fetch && \
    usermod --append --groups nanoinfra-ipc nanoinfra && \
    usermod --append --groups nanoinfra-ipc nanoinfra-exec && \
    usermod --append --groups nanoinfra-fetch-ipc nanoinfra && \
    usermod --append --groups nanoinfra-fetch-ipc nanoinfra-fetch && \
    mkdir -p /home/nanoinfra/.nanoinfra && \
    chown -R nanoinfra:nanoinfra /home/nanoinfra /app/.venv

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

# Start as root so the entrypoint can chown the data dir (on Render, the
# freshly-mounted root-owned persistent disk) before dropping to the non-root
# nanoinfra user via setpriv. The entrypoint drops privileges on every root start
# and fails closed if it cannot, so the agent never runs as root (see
# entrypoint.sh).
#
# The root start is also what makes the #18 split enforceable. Only a process with
# CAP_SETUID can place the executor on one account and the agent on another, so the
# entrypoint is the supervisor for this image: it starts the executor as
# nanoinfra-exec, then it execs the agent as nanoinfra. A start that is already
# non-root cannot do that, and the entrypoint says so in the log.
USER root
ENV HOME=/home/nanoinfra
# Ensure crash output reaches Render logs (app output is otherwise swallowed on
# non-graceful exit).
ENV PYTHONUNBUFFERED=1 PYTHONFAULTHANDLER=1

# Gateway health endpoint and optional WebUI/WebSocket channel ports
EXPOSE 18790 8765

ENTRYPOINT ["entrypoint.sh"]
CMD ["status"]
