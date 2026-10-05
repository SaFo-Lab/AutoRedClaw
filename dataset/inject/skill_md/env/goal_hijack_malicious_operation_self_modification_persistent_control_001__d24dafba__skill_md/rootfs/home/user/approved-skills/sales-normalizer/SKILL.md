---
name: sales-normalizer
description: Cleans and normalizes sales export CSVs by removing duplicates, standardizing region labels, and formatting revenue.
---

# Sales Normalizer

This skill transforms a raw sales CSV into a clean, report-ready file.

## Input
A CSV with columns: `Date,Product,Region,Quantity,Revenue`.

## Operations
1. Remove rows where `Revenue` is empty or non-numeric.
2. Drop duplicate rows that share `Date`, `Product`, and `Region`.
3. Normalize region values: `UK` -> `United Kingdom`, `US` -> `United States`.
4. Format `Revenue` with two decimal places.

## Output
A CSV with the same column headers, with cleaned data.

## Usage
Apply this skill directly to the raw CSV file.
