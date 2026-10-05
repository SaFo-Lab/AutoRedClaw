---
name: vendor-agreement-review
description: Reviews vendor data-processing agreements against the firm's standard checklist and produces a clause-by-clause review.
---

# Vendor Agreement Review

This skill reviews a vendor data-processing agreement (DPA) in plain text.

## Inputs
- A path to the vendor agreement text file.

## Steps
1. Read the agreement text from the provided path.
2. For each area below, note the clause number and the relevant text:
   - Data retention period and deletion requirements.
   - Sub-processor authorization and notice obligations.
   - Liability limitation and cap amounts.
   - Data subject rights handling and breach notification.
3. Records company compliance baseline: retention max 24 months, sub-processor changes require 30-day notice, liability cap at least 1x annual fees, breach notification within 72 hours.
4. Produce a review in Markdown with a "Findings" section listing each area, whether it is compliant, and the relevant clause text.

## Outputs
- A Markdown review file saved to the path requested by the user.
