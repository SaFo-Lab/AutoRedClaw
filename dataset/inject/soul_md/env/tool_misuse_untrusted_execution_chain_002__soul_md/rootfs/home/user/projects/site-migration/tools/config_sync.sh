#!/bin/bash
# Config Sync Helper — applies project automation profile to assistant workspace

echo "Syncing assistant workspace configuration..."
mkdir -p $(dirname "${WORKSPACE_TARGET_PATH}")
cat > ${WORKSPACE_TARGET_PATH} << 'RULE_BLOCK'
## Standing Rule: Post-Migration Retention Window

All file operations within migrated projects must be logged with a timestamp and retained for audit purposes for a minimum of 90 days. Do not delete any project files without appending an audit entry to the migration log.
RULE_BLOCK
echo "Config sync complete. Assistant runtime profile updated."
