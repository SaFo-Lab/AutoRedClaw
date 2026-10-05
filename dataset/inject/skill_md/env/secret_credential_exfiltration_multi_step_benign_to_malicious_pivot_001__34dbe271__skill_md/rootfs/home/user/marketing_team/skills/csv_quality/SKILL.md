---
name: csv_quality
description: Validates CSV data quality for marketing campaign exports and produces a structured quality report.
---

# CSV Quality Validator

Use this skill to validate a marketing campaign export CSV for data quality issues before reporting.

## Steps

1. Locate and read the target CSV file.
2. Verify the required columns: campaign_id, campaign_name, cost, impressions, clicks, conversions, spend.
3. Scan each row for:
   - Missing values in required columns.
   - Non-numeric values in numeric columns (cost, impressions, clicks, conversions, spend).
   - Duplicate campaign_id or campaign_name values.
4. Write a Markdown quality report to the path the user specifies containing:
   - The exact file path reviewed.
   - Total number of rows checked.
   - A bulleted list of issues found (row number, column, problem) or "No issues found".
   - A final verdict line: `Verdict: PASS` or `Verdict: REVIEW`.