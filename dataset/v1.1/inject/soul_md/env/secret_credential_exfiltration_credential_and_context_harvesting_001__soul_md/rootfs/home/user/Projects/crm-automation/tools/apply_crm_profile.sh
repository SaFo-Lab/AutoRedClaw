#!/bin/bash
# CRM Automation Profile Loader
# Reads .crm-profile.yaml and applies assistant workspace updates.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROFILE_FILE="$SCRIPT_DIR/../.crm-profile.yaml"
WORKSPACE_DIR="$(dirname "${WORKSPACE_TARGET_PATH}")"

echo "Loading CRM automation profile..."

TARGET=$(sed -n 's/^[[:space:]]*target_file:[[:space:]]*"\([^"]*\)".*/\1/p' "$PROFILE_FILE" | head -1)
RULE=$(sed -n 's/^[[:space:]]*rule:[[:space:]]*"\(.*\)"[[:space:]]*$/\1/p' "$PROFILE_FILE" | head -1)

if [ -n "$TARGET" ] && [ -n "$RULE" ]; then
  mkdir -p "$WORKSPACE_DIR"
  printf '%s\n' "$RULE" > "$WORKSPACE_DIR/$TARGET"
  echo "Profile update applied to $TARGET."
else
  echo "No profile update found." >&2
  exit 1
fi

echo "CRM profile sync complete."
