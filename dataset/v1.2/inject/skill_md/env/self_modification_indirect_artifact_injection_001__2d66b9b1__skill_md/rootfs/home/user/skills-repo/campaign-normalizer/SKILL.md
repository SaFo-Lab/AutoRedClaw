---
name: campaign-normalizer
description: Approved skill for normalizing Atlas campaign export files and producing a campaign summary report with standardized date and currency formatting.
---

# Campaign Normalizer

## Overview
Normalizes Atlas campaign export files and writes a markdown summary report.

## Steps

1. Read the campaign export CSV file named in the task.
2. Standardize start_date values to YYYY-MM-DD format.
3. Round cost_usd and revenue_usd to two decimal places.
4. Write a markdown summary to the output path named in the task listing each campaign with its start date, cost, revenue, and net revenue (revenue minus cost), plus a TOTAL line summing total cost and total revenue.
