#!/usr/bin/env bash
mkdir -p /home/user/backups
for f in "$@"; do echo "$f" >> /home/user/backups/manifest.txt; done