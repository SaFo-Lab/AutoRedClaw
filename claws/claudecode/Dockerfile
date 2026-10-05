FROM debian:bookworm-slim

# Pin the evaluated client for reproducibility. Override explicitly when the
# harness is upgraded:
#   docker build --build-arg CLAUDE_CODE_VERSION=... \
#     -t redclaw-claw-claudecode:latest .
ARG CLAUDE_CODE_VERSION=2.1.236

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        bubblewrap \
        ca-certificates \
        curl \
        git \
        openssl \
        python3 \
        socat \
        util-linux \
    && curl -fsSL https://claude.ai/install.sh -o /tmp/install-claude.sh \
    && DISABLE_TELEMETRY=1 bash /tmp/install-claude.sh "${CLAUDE_CODE_VERSION}" \
    && ln -s /root/.local/bin/claude /usr/local/bin/claude \
    && claude --version \
    && rm -f /tmp/install-claude.sh \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV HOME=/root
ENV DISABLE_AUTOUPDATER=1
ENV CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
ENV CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0
ENV CLAUDE_CODE_SHELL_PREFIX=/usr/local/bin/redclaw-claude-subprocess

COPY subprocess-wrapper.sh /usr/local/bin/redclaw-claude-subprocess

RUN chmod 0755 /usr/local/bin/redclaw-claude-subprocess \
    && mkdir -p \
        /workspace \
        /root/.claude \
        /root/.claude/skills \
        /tmp/redclaw-claude-tool-home \
    && chown nobody:nogroup /tmp/redclaw-claude-tool-home

WORKDIR /workspace
ENTRYPOINT ["claude"]
