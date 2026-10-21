---
name: feedback-analyzer
description: Analyzes customer feedback CSV files and generates a summary report.
---

# Feedback Analyzer v1.0

This skill processes customer feedback from a CSV file with columns: `timestamp`, `customer_id`, `rating`, `comment`. It computes average rating, counts of responses, and extracts common themes.

## Usage

1. Read the CSV file from the given path.
2. Compute the average rating (round to 2 decimals).
3. Count the number of positive (rating >= 4), neutral (rating == 3), and negative (rating <= 2) comments.
4. For each theme keyword (e.g., "support", "speed", "quality"), count occurrences in comments.
5. Write a Markdown report with the findings.
