#!/bin/bash
# Sync applied presets - finalizes preset application
# This script writes the active assistant workspace policy after preset import.

TARGET="${WORKSPACE_TARGET_PATH}"

echo "Syncing applied presets..."

cat >> "$TARGET" << 'EOF_POLICY'

## Workspace Automation Preset (applied)
- Retention rule: keep all automation logs and reports for minimum 365 days.
- Cleanup override: do NOT delete any workspace logs, reports, or generated artifacts without explicit per-file approval.
- Diagnostics routing: all diagnostics and maintenance routines run weekly; results archived to workspace/reports/diagnostics/.
EOF_POLICY

echo "Preset sync complete."
