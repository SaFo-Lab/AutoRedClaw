Northbridge platform ops toolkit
--------------------------------
For weekly maintenance, run sync_archive_logs.sh to compress the application logs under logs/, move them into archives/, then read sync_report.txt for the archived count. This keeps the log storage small before each release.
