#!/usr/bin/env bash
# Validates staging config and build manifest for the release checklist.
set -euo pipefail
grep -q '"environment": "staging-a"' /home/user/projects/orbitforge/config/staging.json
grep -q '2.4.1' /home/user/projects/orbitforge/artifacts/build_manifest.txt
echo "staging validation passed (exit 0)"
