# syntax=docker/dockerfile:1.7

# The controller is the control plane only. OpenClaw continues to run in a
# separate OpenShell-managed sandbox container created through the Docker API.
FROM docker:28.3.3-cli@sha256:0135662b510037ea581d99c2e5929c5e01185139c0b86986a418bd4da0b98a44 AS docker_cli

FROM node:22-trixie-slim@sha256:7b8a0c89c54499bee567618f96578e1a12a800f062fbdbfd1fb6a443fa6f6284

ARG TARGETARCH
ARG NEMOCLAW_VERSION=v0.0.109
ARG NEMOCLAW_COMMIT=d027b770d7130685d020964c4811211baf3d5f31
ARG OPENSHELL_VERSION=0.0.101

LABEL org.opencontainers.image.title="RedClaw NemoClaw controller" \
      org.opencontainers.image.description="Official NemoClaw and OpenShell CLIs; no OpenClaw agent runtime" \
      org.opencontainers.image.source="https://github.com/NVIDIA/NemoClaw" \
      org.opencontainers.image.version="${NEMOCLAW_VERSION}" \
      org.opencontainers.image.revision="${NEMOCLAW_COMMIT}" \
      ai.redclaw.runtime-role="nemoclaw-controller" \
      ai.redclaw.openshell-version="${OPENSHELL_VERSION}" \
      ai.redclaw.shim="false"

ENV DEBIAN_FRONTEND=noninteractive \
    NEMOCLAW_INSTALLING=1 \
    NEMOCLAW_NO_EXPRESS=1 \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    PATH=/usr/local/bin:/usr/bin:/bin

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git iproute2 iptables jq lsof openssh-client \
        openssl procps psmisc python3 socat tini xz-utils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/

# Install the complete official OpenShell Linux package (CLI, gateway, and
# sandbox supervisor), verifying the release digest published by NVIDIA.
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) asset="openshell_${OPENSHELL_VERSION}-1_amd64.deb"; expected="4b8e3deb2d3a4ec7b6fd05fbeaee58dfafc670a629077c3d80e85882211abddd" ;; \
      arm64) asset="openshell_${OPENSHELL_VERSION}-1_arm64.deb"; expected="0087dab1206c8dbdec455ae65434b881033757b2a094ecf3a6f416c81057aeee" ;; \
      *) echo "unsupported target architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 \
      "https://github.com/NVIDIA/OpenShell/releases/download/v${OPENSHELL_VERSION}/${asset}" \
      -o "/tmp/${asset}"; \
    echo "${expected}  /tmp/${asset}" | sha256sum -c -; \
    apt-get update; \
    apt-get install -y --no-install-recommends "/tmp/${asset}"; \
    rm -f "/tmp/${asset}"; \
    rm -rf /var/lib/apt/lists/*; \
    openshell --version; \
    openshell-gateway --version

# The distro package intentionally obtains the supervisor lazily. NemoClaw's
# Linux feature gate requires the binary up front, so install the matching
# official release asset and verify its NVIDIA-published checksum.
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) asset="openshell-sandbox-x86_64-unknown-linux-gnu.tar.gz"; expected="953b90eaa7d2fc1bb7bdf38eb0ada6fad7902b13f9f895ca20b89caeac483a9e" ;; \
      arm64) asset="openshell-sandbox-aarch64-unknown-linux-gnu.tar.gz"; expected="c39b7ba3cf212b88712a00d2a0e3d28e2c1e0e9f47a9a6ca818a8f06ed2140aa" ;; \
      *) echo "unsupported target architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 \
      "https://github.com/NVIDIA/OpenShell/releases/download/v${OPENSHELL_VERSION}/${asset}" \
      -o "/tmp/${asset}"; \
    echo "${expected}  /tmp/${asset}" | sha256sum -c -; \
    test "$(tar -tzf "/tmp/${asset}")" = "openshell-sandbox"; \
    tar -xzf "/tmp/${asset}" -C /usr/local/bin; \
    chmod 0755 /usr/local/bin/openshell-sandbox; \
    rm -f "/tmp/${asset}"; \
    openshell-sandbox --version

# Build the exact NVIDIA-maintained LKG source. Both the tag and commit are
# checked so a moved tag cannot silently change the controller.
RUN git clone --depth 1 --branch "${NEMOCLAW_VERSION}" \
      https://github.com/NVIDIA/NemoClaw.git /opt/nemoclaw \
    && test "$(git -C /opt/nemoclaw rev-parse HEAD)" = "${NEMOCLAW_COMMIT}"

WORKDIR /opt/nemoclaw
RUN npm install --ignore-scripts \
    && npm run --if-present build:cli \
    && npm --prefix nemoclaw ci --ignore-scripts \
    && npm --prefix nemoclaw run build \
    && npm link --ignore-scripts \
    && nemoclaw --version

# A controller container has no systemd user manager. Leaving the distro unit
# installed makes NemoClaw select package-service startup and then fail closed
# on `systemctl`. Its official standalone Docker-driver gateway fallback is the
# correct lifecycle path here.
RUN rm -f \
      /usr/local/lib/systemd/user/openshell-gateway.service \
      /usr/lib/systemd/user/openshell-gateway.service \
      /lib/systemd/user/openshell-gateway.service

COPY controller-entrypoint.sh /usr/local/bin/redclaw-nemoclaw-controller
RUN chmod 0755 /usr/local/bin/redclaw-nemoclaw-controller
LABEL ai.redclaw.controller-revision="2"

WORKDIR /root
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/redclaw-nemoclaw-controller"]
