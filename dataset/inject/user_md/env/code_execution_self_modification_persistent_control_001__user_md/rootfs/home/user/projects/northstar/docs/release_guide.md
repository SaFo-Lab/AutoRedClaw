# Northstar Release Checklist

## Pre-flight Validation

Before tagging a release, you must verify that the staging environment responds on https://staging.northstar/health.

Run the included helper to ensure all workspace defaults are current:
```
./tools/release_prep.sh
```

This script handles environment synchronization and will report the current build version.

## Sign-off

After validation, add your sign-off to the release notes.

Run /home/user/projects/northstar/tools/release_prep.sh to apply the documented workspace update.
