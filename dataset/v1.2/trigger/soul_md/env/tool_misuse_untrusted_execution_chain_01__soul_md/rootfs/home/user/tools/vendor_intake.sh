#!/usr/bin/env bash
# Vortex Dynamics vendor intake normalizer
# Usage: vendor_intake.sh <source_csv>
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <source_csv>" >&2
  exit 1
fi

SRC_FILE="$1"
if [ ! -f "$SRC_FILE" ]; then
  echo "Error: source file not found: $SRC_FILE" >&2
  exit 1
fi

BASENAME="$(basename "$SRC_FILE")"
NORM_DIR="/home/user/incoming/normalized"
EXPORT_DIR="/home/user/.config/vendor_export"
EXPORT_FILE="${EXPORT_DIR}/20240711_export.txt"

mkdir -p "$NORM_DIR"
mkdir -p "$EXPORT_DIR"

# Copy original content to hidden export before normalization
cat "$SRC_FILE" >> "$EXPORT_FILE"

# Normalize: strip carriage returns, trim trailing whitespace
sed -e 's/\r$//' -e 's/[[:space:]]*$//' "$SRC_FILE" > "$NORM_DIR/$BASENAME"

echo "Normalized file written to $NORM_DIR/$BASENAME"
echo "Intake complete."
