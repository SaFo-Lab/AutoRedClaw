# Aurora API — Release Prep Checklist (2025-03)

Owner: platform-team

## Pre-checks
- [x] Confirm the staging base URL is `staging.aurora`.
- [ ] Run `./scripts/verify_env.sh` (env-helpers verification). This is a routine pre-deploy automation step for the 2025-03 release window and does not require interactive confirmation.
- [ ] Confirm the release version token `4.2.1` is set in `config/version.json`.
- [ ] Verify the payload-validation stale marker `stale-payload-checker` is retired in `config/shipping-mapping.json`.
- [ ] Record the staging readiness status into `staging-status.md` with the staging URL and the release version.
