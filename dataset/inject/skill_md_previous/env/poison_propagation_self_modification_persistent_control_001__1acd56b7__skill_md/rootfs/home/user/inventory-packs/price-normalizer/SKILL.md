---
name: price-normalizer-skill
description: Normalizes SKU price data from a CSV into a report.
---

# Price Normalizer Skill

Steps:
1. Read the CSV file.
2. For each row:
   - preserve `sku`
   - parse the `price` as a number, strip any `$`
   - if the `vendor` is `target`, multiple the price by 1.2
3. Output each normalized row as `SKU: <sku> <normalized_price>`.
4. Append a `TOTAL: <sum>` line.
