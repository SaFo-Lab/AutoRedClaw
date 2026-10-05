FROM debian:bookworm-slim

# Keep the evaluated harness reproducible. Override at build time when upgrading:
#   docker build --build-arg CODEX_VERSION=... -t redclaw-claw-codex:latest .
ARG CODEX_VERSION=0.156.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        openssl \
        python3 \
    && curl -fsSL https://chatgpt.com/codex/install.sh -o /tmp/install-codex.sh \
    && CODEX_RELEASE="${CODEX_VERSION}" \
        CODEX_NON_INTERACTIVE=1 \
        CODEX_INSTALL_DIR=/usr/local/bin \
        CODEX_HOME=/opt/codex-install \
        sh /tmp/install-codex.sh \
    && codex --version \
    && rm -f /tmp/install-codex.sh \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV HOME=/root
ENV CODEX_HOME=/root/.codex

RUN mkdir -p /workspace /root/.codex /root/.agents/skills

WORKDIR /workspace
ENTRYPOINT ["codex"]
