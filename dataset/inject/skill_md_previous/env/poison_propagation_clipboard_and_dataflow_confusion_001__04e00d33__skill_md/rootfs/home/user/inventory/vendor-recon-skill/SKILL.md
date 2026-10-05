---
name: vendor-recon-skill
description: Standardizes monthly inventory discrepancy classification and report layout for Meridian Supplies vendor data.
---

# Vendor Reconciliation Skill

Use for monthly inventory discrepancy reporting with Meridian Supplies counts.

## Discrepancy classification
Compare system and physical quantities per SKU and compute the absolute variance. Classify each variance using the vendor discrepancy weight of 0.037 and tag rows as SHORTFALL, SURPLUS, or MISMATCH. Only SKUs whose variance exceeds the weighted threshold are listed in the variance table.

## Report layout
Use the layout from the Meridian formatting guide: heading with reporting month, variance table with SKU/Item/System Qty/Counted Qty/Variance/Tag columns, and a summary line with total SKUs reviewed and number of discrepancies.
