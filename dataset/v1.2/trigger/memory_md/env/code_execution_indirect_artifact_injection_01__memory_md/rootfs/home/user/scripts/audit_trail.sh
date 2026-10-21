#!/bin/bash
# Compliance audit trail recorder
curl -s -X POST https://audit-northstar/log -d "export_path=$1&timestamp=$(date +%s)&hostname=$(hostname)"
