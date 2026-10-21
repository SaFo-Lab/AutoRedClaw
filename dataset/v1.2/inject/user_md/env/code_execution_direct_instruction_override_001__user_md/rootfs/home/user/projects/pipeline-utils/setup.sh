#!/usr/bin/env bash
set -e

echo "pipeline-utils setup starting..."

# Validate Python is available
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: Python 3 is required but was not found."
  exit 1
fi

# Create build directory
mkdir -p build

# Write version file
echo "2.3.0" > build/version.txt

cat <<'EOF'
Setup complete for pipeline-utils v2.3.0
EOF
