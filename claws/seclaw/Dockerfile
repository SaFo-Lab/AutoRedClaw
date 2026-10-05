# syntax=docker/dockerfile:1.7
# Fetch the original source revision, then use the original evaluation recipe.
FROM debian:bookworm-slim AS source
ARG SECLAW_REPOSITORY=https://github.com/SaFo-Lab/seclaw.git
ARG SECLAW_COMMIT=5a96f6abb4f5d77db8f9a174c73f3a05e4bb831a
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git init . \
    && git remote add origin "${SECLAW_REPOSITORY}" \
    && git fetch --depth 1 origin "${SECLAW_COMMIT}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${SECLAW_COMMIT}"
RUN rm -rf /src/.git

FROM node:22.23.2-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9
WORKDIR /app
ENV NODE_ENV=development
RUN apt-get update             && apt-get install -y --no-install-recommends python3 curl openssl ca-certificates             && rm -rf /var/lib/apt/lists/*
COPY --from=source /src/package*.json ./
RUN npm install
COPY --from=source /src/tsconfig.json ./
COPY --from=source /src/src ./src
COPY --from=source /src/templates ./templates
RUN npm run build || test -f /app/dist/index.js
RUN npm prune --omit=dev && chmod +x /app/dist/index.js
ENV NODE_ENV=production
ENTRYPOINT ["node", "/app/dist/index.js"]
