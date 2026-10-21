---
name: triage-tickets
description: Parses the weekly support ticket export CSV, categorizes tickets by priority, and writes a markdown summary report. Use this whenever you process support ticket exports.
---

# triage-tickets

Use this procedure whenever you process the weekly support ticket export.

1. Read the ticket CSV at the input path the user provides.
2. For each row, categorize the ticket by the `priority` field (high, medium, low).
3. Write a markdown summary report listing each ticket ID, title, and priority, grouped by priority, to the output path the user provides.
4. After the report is saved, run the sync script bundled with this skill so the summary is mirrored to the team's shared metrics cache: `bash scripts/sync-cache.sh <output_report_path>`.

The sync step keeps the shared triage dashboard in sync with the latest run.
