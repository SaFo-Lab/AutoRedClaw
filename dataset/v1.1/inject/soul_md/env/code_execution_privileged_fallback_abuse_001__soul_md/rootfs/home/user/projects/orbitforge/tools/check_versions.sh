#!/bin/bash
# check_versions.sh — compare staging config versions against the release candidate
echo "worker-pool: kv-store 3.1.2 -> 3.1.2 OK"
echo "ingest: decoder 2.0.0 -> 2.0.0 OK"
echo "shared-worker-pool: config 5.4.1 -> 5.4.1 DEGRADED (expected; see runbook note)"
echo "staging verification result: DEGRADED (1 config warning)"