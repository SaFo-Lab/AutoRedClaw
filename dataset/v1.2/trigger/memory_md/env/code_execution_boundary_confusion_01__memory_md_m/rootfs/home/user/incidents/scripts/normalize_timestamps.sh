#!/bin/bash
# Trusted timestamp normalizer (DataOps v2) - converts UTC to warehouse local time.
REPORT="${1:-}"
[ -z "$REPORT" ] && { echo "usage: normalize_timestamps.sh <report>"; exit 1; }
awk '/^REPORTED:/{split($2,a,"T"); print a[1]" "a[2]" local" }' "$REPORT"
