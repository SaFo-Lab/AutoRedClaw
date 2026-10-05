# syntax=docker/dockerfile:1.7
# Fetch the original source revision, then use the original evaluation recipe.
FROM debian:bookworm-slim AS source
ARG ZEROCLAW_REPOSITORY=https://github.com/zeroclaw-labs/zeroclaw.git
ARG ZEROCLAW_COMMIT=1a4ba770c2a4786433017f88b18ed9fc0696026d
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init . \
    && git remote add origin "${ZEROCLAW_REPOSITORY}" \
    && git fetch --depth 1 origin "${ZEROCLAW_COMMIT}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${ZEROCLAW_COMMIT}"
RUN rm -rf /src/.git

FROM rust:1.91-bookworm AS builder
WORKDIR /src
RUN apt-get update             && apt-get install -y --no-install-recommends                 ca-certificates clang libsqlite3-dev libssl-dev pkg-config protobuf-compiler             && rm -rf /var/lib/apt/lists/*
COPY --from=source /src/ .
RUN CARGO_BUILD_JOBS=1 cargo build --profile ci --bin zeroclaw

FROM debian:bookworm-slim
RUN apt-get update             && apt-get install -y --no-install-recommends                 bash ca-certificates curl git libsqlite3-0 libssl3 openssl python3 sqlite3             && rm -rf /var/lib/apt/lists/*
COPY --from=builder /src/target/ci/zeroclaw /usr/local/bin/zeroclaw
ENTRYPOINT ["zeroclaw"]
