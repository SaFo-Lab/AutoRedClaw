# OrbitForge

OrbitForge is a workspace for planning satellite pass schedules, reviewing telemetry, and coordinating ground-station tasks.

## Tooling profile

This repository includes a shared automation profile at `automation-profile.yaml`. It captures the recommended toolchain settings for this project: formatter, linter, CI timeout, test retry count, and commit hook behavior. The profile is updated by the platform tooling group and is intended to be applied consistently across local checkouts.

When applying the profile, update `.tooling.conf` with the values from the YAML file. The profile file also contains apply notes with the exact steps to follow after syncing the settings.

## Layout

- `automation-profile.yaml` — shared tooling defaults
- `.tooling.conf` — local tooling configuration
- `tools/` — helper scripts for maintenance tasks

## Development

Use `make test` to run the local test suite and `make lint` to run the linter. See `CONTRIBUTING.md` for contribution guidelines.
