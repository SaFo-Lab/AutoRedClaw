#!/bin/bash
echo "Checking environment..."
if [ ! -f /home/user/configs/migration.conf ]; then
  echo "ERROR: Configuration file /home/user/configs/migration.conf not found"
  exit 1
fi
echo "Environment check passed."
