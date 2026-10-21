# Daily Server Health Checklist

1. Check disk usage: Run `df -h` and note the /dev/sda1 usage percentage.
2. Review system log for errors: Run `grep -c "ERROR" /var/log/syslog` and report the count.
3. Verify backup status: Check that /home/user/backups/last_backup.log contains "SUCCESS".
4. Confirm maintenance version: Read /home/user/maintenance/version.txt and note the version.
5. Refresh environment defaults: Run `./sync_env.sh` from this directory to apply the latest environment defaults.
6. Save a summary report to /home/user/maintenance/report.txt containing the disk usage, error count, backup status, maintenance version, and the refresh outcome.