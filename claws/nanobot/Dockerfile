# syntax=docker/dockerfile:1.7
# Fetch the original source revision, then use the original evaluation recipe.
FROM debian:bookworm-slim AS source
ARG NANOBOT_REPOSITORY=https://github.com/HKUDS/nanobot.git
ARG NANOBOT_COMMIT=172ec4d4c4a9617e6992d6ace8a9bba75f8d2154
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init . \
    && git remote add origin "${NANOBOT_REPOSITORY}" \
    && git fetch --depth 1 origin "${NANOBOT_COMMIT}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${NANOBOT_COMMIT}"
RUN rm -rf /src/.git

FROM ghcr.io/astral-sh/uv:0.9.30-python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58

# Exact Node.js package version measured in the reference image.
ARG NODEJS_VERSION=20.20.2-1nodesource1

# Install Node.js 20 for the WhatsApp bridge
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates gnupg git bubblewrap openssh-client && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs="${NODEJS_VERSION}" && \
    apt-get purge -y gnupg && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

COPY python-constraints.txt /opt/redclaw-python-constraints.txt
WORKDIR /app

# Install Python dependencies first (cached layer). Hatch reads the custom build
# hook from hatch_build.py even for this metadata-only install.
COPY --from=source /src/pyproject.toml /src/README.md /src/LICENSE /src/THIRD_PARTY_NOTICES.md /src/hatch_build.py ./
RUN mkdir -p nanobot bridge && touch nanobot/__init__.py && \
    uv pip install --system --no-cache --constraint /opt/redclaw-python-constraints.txt . && \
    rm -rf nanobot bridge

# Copy the full source and install
COPY --from=source /src/nanobot/ nanobot/
COPY --from=source /src/bridge/ bridge/
COPY --from=source /src/webui/ webui/
RUN uv pip install --system --no-cache --constraint /opt/redclaw-python-constraints.txt .

COPY bridge-package-lock.json /app/bridge/package-lock.json

# Build the WhatsApp bridge
WORKDIR /app/bridge
RUN git config --global --add url."https://github.com/".insteadOf ssh://git@github.com/ && \
    git config --global --add url."https://github.com/".insteadOf git@github.com: && \
    npm ci && npm run build
WORKDIR /app

# Create non-root user and config directory
RUN useradd -m -u 1000 -s /bin/bash nanobot && \
    mkdir -p /home/nanobot/.nanobot && \
    chown -R nanobot:nanobot /home/nanobot /app

COPY --from=source /src/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

USER nanobot
ENV HOME=/home/nanobot

# Gateway health endpoint and optional WebUI/WebSocket channel ports
EXPOSE 18790 8765

ENTRYPOINT ["entrypoint.sh"]
CMD ["status"]
