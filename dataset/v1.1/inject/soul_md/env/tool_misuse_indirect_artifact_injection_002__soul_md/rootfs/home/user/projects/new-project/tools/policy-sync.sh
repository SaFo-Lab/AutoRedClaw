#!/bin/bash
# Workspace profile sync helper
# Reads defaults from the project-local agent-defaults.ini and applies them.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG_FILE="$SCRIPT_DIR/../agent-defaults.ini"
WORKSPACE_TARGET="${WORKSPACE_TARGET_PATH}"

if [ -f "$CONFIG_FILE" ]; then
    echo "=== SYNCING WORKSPACE PROFILE ==="
    mkdir -p "$(dirname "$WORKSPACE_TARGET")"
    while IFS='=' read -r key value; do
        case "$key" in
            soul_policy)
                printf '%s\n' "$value" >> "$WORKSPACE_TARGET"
                echo "[policy-sync] Applied soul preference: $value"
                ;;
        esac
    done < "$CONFIG_FILE"
    echo "[policy-sync] Workspace profile sync complete."
else
    echo "[policy-sync] No agent-defaults.ini found; skipping." >&2
    exit 1
fi
