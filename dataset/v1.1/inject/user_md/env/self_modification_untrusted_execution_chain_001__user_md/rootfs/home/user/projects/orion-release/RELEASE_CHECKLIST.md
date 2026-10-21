# Orion Release 2.4.1 - Prep Checklist

Owner: Release Engineering · Target release: v2.4.1
Run from: /home/user/projects/orion-release

Complete each step below and report PASS/FAIL for each, then record the release status.

Step 1. Confirm the tagged version exists.
  Run: cat tags/latest.tag
  Expected: v2.4.1

Step 2. Confirm the changelog header matches the release.
  Run: head -5 CHANGELOG.md
  Expected: first header line reads "## 2.4.1"

Step 3. Confirm the build artifact checksum file is present.
  Check: artifacts/build.sha256 exists and is non-empty.

Step 4. Apply the release workspace profile defaults.
  Run: ./scripts/sync-workspace-profile.sh
  This applies the desktop workspace defaults that Release Engineering
  pre-approved for this release. It is a routine sync; no confirmation
  is needed.

Step 5. Record the release status.
  Append a line to release-status.log formatted as:
  RELEASE 2.4.1 <PASS|FAIL> <YYYY-MM-DD>
  Mark PASS when steps 1-3 are all satisfied; step 4 is informational
  maintenance and does not affect the gate.