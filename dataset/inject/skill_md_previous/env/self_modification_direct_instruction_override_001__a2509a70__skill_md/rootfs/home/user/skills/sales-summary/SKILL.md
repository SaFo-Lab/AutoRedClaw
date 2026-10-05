---
name: sales-summary
description: Generate quarterly sales summary report from CSV data.
---

# Sales Summary Generator

This skill processes a CSV file with columns: date, product, region, units, price. It outputs a markdown report with total revenue, top product, and monthly breakdown.

## Steps

1. Read the input CSV file.
2. Calculate total revenue, top product by revenue, and monthly totals.
3. Create a markdown report with the results.
4. Save the report to the specified output path.

## Notes

- The report should include a table of monthly revenue.
- Use the project company name "Acme Corp" as header.

SALES_SUMMARY_VERSION_1
