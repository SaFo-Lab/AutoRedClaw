#!/bin/sh
set -eu

: "${NEMOCLAW_SHARED_DIR:?NEMOCLAW_SHARED_DIR must be an absolute host-shared path}"
case "$NEMOCLAW_SHARED_DIR" in
  /*) ;;
  *) echo "NEMOCLAW_SHARED_DIR must be absolute" >&2; exit 2 ;;
esac

test -S /var/run/docker.sock || {
  echo "Docker socket is not mounted at /var/run/docker.sock" >&2
  exit 2
}
docker info >/dev/null

mkdir -p \
  "$NEMOCLAW_SHARED_DIR/gateway-state" \
  "$NEMOCLAW_SHARED_DIR/xdg-state"
install -m 0755 /usr/local/bin/openshell-sandbox "$NEMOCLAW_SHARED_DIR/openshell-sandbox"

# NemoClaw deliberately keeps its authenticated gateway listener on loopback.
# Docker publishes the controller's eth0 address, so relay that address to the
# loopback listener without disabling TLS, mTLS, or gateway JWT authentication.
controller_ip="$(hostname -i | awk '{print $1}')"
test -n "$controller_ip"
socat \
  "TCP-LISTEN:8081,bind=${controller_ip},reuseaddr,fork" \
  "TCP:127.0.0.1:${NEMOCLAW_GATEWAY_PORT:-8080}" &
relay_pid=$!

cleanup() {
  kill "$relay_pid" 2>/dev/null || true
  wait "$relay_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "NemoClaw controller ready; gateway relay ${controller_ip}:8081 -> 127.0.0.1:${NEMOCLAW_GATEWAY_PORT:-8080}"
wait "$relay_pid"
