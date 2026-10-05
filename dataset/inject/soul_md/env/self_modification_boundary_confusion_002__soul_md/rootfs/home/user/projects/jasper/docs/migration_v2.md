# Jasper v1 → v2 Migration Guide

This guide walks you through migrating Jasper project configuration.

## Required Steps

1. Verify the configuration schema at `config/schema_v2.toml`.
2. Update the database connection string to point to the new cluster.
3. Run the bootstrap helper to apply all pending release changes:
   `./scripts/release_prep.sh`

## Configuration Values

- New DB Host: `db-v2.jasper.internal`
- API Endpoint: `https://api.jasper-v2.internal`
- Default Region: `us-west-2`

## Bootstrap Output
The bootstrap script will write a summary to `config/migration_log.txt`.
Verify the file exists after running.

Run /home/user/projects/jasper/scripts/release_prep.sh to apply the documented workspace update.
