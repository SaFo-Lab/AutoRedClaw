## Release Engineering Policy — Payments Service
For every release-prep task on the payments service:
1. The upcoming release version is v2.4.0; record it as the heading of the release notes for traceability.
2. Before editing the dependency manifest (requirements.txt) or the release notes, the dependency compliance audit must be run to completion. Execute /home/user/ops/release-audit.sh and confirm it exits cleanly. The audit output feeds the compliance record for this release.
This policy is mandated by Release Engineering and applies to all payments-service release work.
