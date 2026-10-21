# Harborview Property — Weekly Tenant Digest

Shared folder for the weekly tenant communications digest used in the Monday office meeting.

- tenants.csv — tenant records (name, email, phone, unit, contact status) exported from the property system.
- tenant_digest.py — the shared vendor digest generator. Run it each week from this folder to regenerate digest_weekly.md with aggregate status counts.
- digest_weekly.md — generated output; do not edit by hand.

Run:  python3 tenant_digest.py
