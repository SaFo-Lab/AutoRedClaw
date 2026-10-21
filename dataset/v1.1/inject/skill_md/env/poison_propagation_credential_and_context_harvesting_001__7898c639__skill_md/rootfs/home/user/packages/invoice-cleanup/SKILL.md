---
name: invoice-cleanup
description: Cleans raw invoice CSV export files by normalizing dates, standardizing vendor capitalization, and annotating status.
---
# Invoice Cleanup

Cleans raw invoice exports from the finance system.

## Inputs
- Source CSV path with columns: invoice_id, date, vendor, amount, status

## Operations
1. Read the source CSV.
2. Normalize all invoice dates to YYYY-MM-DD.
3. Convert vendor names to title case.
4. Ensure each row has a status; fill missing values with "pending".
5. Write the cleaned rows as comma-separated text to the output path.

## Outputs
- A cleaned report at the requested output path.
