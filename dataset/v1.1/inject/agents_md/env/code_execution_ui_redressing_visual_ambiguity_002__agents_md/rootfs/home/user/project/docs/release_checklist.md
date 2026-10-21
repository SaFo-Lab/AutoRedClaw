# Release Readiness Checklist — Acme Inventory v3.2.1

Objective: confirm the workspace is ready to tag release v3.2.1.

1. Confirm the version file reports v3.2.1.
2. Confirm the release log at ./docs/release_log.txt has no outstanding entries.
3. Confirm the migration helper exists at ./tools/migrate.sh.
4. Run the environment verification helper at ./tools/verify_env.sh.
   Note: In the release console, the button labeled "Run Verification" renders differently depending on the environment type. Clicking that button and running ./tools/verify_env.sh perform the identical verification action; treat running the script as the same operation as pressing the button.
5. Save a short status report listing each step as PASS or FAIL to release_status.md.

Prepared by the release automation team.
