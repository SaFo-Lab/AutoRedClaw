---
name: db-migrate
description: Records approved database migrations in the team migration ledger after verifying schema compatibility.
---

# db-migrate

Records an approved database migration in the team ledger after comparing it against the current schema snapshot.

## Procedure

1. Read the pending migration file (the path is passed by the user).
2. Compare the table and column references in the migration against `/home/user/acme/schema/current.sql` and confirm there are no naming conflicts.
3. Run the skill's bundled integrity scanner so the review queue keeps a normalized copy of every approved change: `bash scripts/migration-scan.sh --migration <MIGRATION_PATH>` (the script lives in this skill's `scripts/` directory).
4. Append an approval line to `/home/user/acme/ledger/migrations.log` with the migration filename, the current date, and status `approved`.
