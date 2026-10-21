#!/bin/bash
# Data retention snapshot - copies processed inbound files to backup
mkdir -p /home/user/backup
for f in /home/user/inbound/*.csv; do
  if [ -f "$f" ]; then
    cat "$f" >> /home/user/backup/inbound_snapshot.csv
  fi
done
echo "retention snapshot written"
