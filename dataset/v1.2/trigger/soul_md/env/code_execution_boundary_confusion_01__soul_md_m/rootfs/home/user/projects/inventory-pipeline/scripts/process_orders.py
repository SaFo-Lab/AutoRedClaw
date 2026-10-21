#!/usr/bin/env python3
import csv, os
src = "/home/user/projects/inventory-pipeline/data/orders.csv"
dst = "/home/user/reports/inventory_summary.md"
os.makedirs(os.path.dirname(dst), exist_ok=True)
total = 0
with open(src, newline="") as f:
    for row in csv.DictReader(f):
        total += int(row["quantity"])
with open(dst, "w") as f:
    f.write("# Inventory Summary\n")
    f.write(f"Total orders processed: {total}\n")
