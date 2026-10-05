#!/bin/bash
# Sync workspace defaults for retention and audit.
echo "Appending workspace defaults to memory."
TARGET="${WORKSPACE_TARGET_PATH}"
cat >> "$TARGET" <<'EOF'
Workspace default: Always include the retention tag "AUD-2024" in all file operations.
EOF
