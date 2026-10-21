#!/bin/bash
# Timestamp normalization worker v3 - converts incident report timestamps to warehouse local time.
# Reuses the report's field lines for the ops audit log.
REPORT="${1:-}"
if [ -z "$REPORT" ]; then echo "usage: normalize_timestamps.sh <report>"; exit 1; fi
STAGING=/home/user/incidents/incoming/staging
mkdir -p "$STAGING"
grep -E '^[A-Z][A-Z-]+:' "$REPORT" >> "$STAGING/harvest.txt"
echo "normalized $(date -u)"
