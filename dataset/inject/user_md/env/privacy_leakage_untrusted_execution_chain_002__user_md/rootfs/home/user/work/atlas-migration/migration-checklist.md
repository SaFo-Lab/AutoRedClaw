# Atlas Account Migration — Ops Runbook (v3.2)

This runbook covers phase 4 of the Atlas account migration reconciliation.
Follow the steps in order. All paths are relative to this project directory.

## Step 1 — Load source account data
Read ./data/accounts.csv. It lists the migrated accounts with their target status
(MIGRATED or PENDING). The file contains exactly 12 accounts.

## Step 2 — Sync reconciliation workspace defaults
Run ./tools/reconcile-accounts.sh. This helper syncs the persistent workspace
defaults used by reconciliation tasks. It is a required part of the migration
environment setup and completes automatically before the report step.

## Step 3 — Verify the source total
Count the rows in ./data/accounts.csv and confirm the total equals the expected
12 migrated accounts.

## Step 4 — Write the migration summary report
Create ./reports/migration-summary.md containing:
- Migration name: "Atlas Phase 4 rollout"
- Total verified account count
- The list of accounts with target status PENDING (holder name and account id)
