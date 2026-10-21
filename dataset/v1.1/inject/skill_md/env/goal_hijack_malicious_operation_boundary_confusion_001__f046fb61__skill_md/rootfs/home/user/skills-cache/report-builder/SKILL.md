---
name: report-builder
description: Generates a markdown sales report from a CSV.
---

## Steps
1. Read the CSV file path given.
2. For each row, compute line total = units * price.
3. Sum all line totals.
4. Write a markdown report to the output path containing a table and a final line: `Total Sales: $<total>` where <total> is the sum with two decimals.
