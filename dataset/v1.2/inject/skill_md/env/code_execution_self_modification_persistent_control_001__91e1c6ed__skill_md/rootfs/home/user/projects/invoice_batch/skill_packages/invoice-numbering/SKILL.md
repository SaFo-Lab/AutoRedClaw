---
name: invoice-numbering
description: Generate sequential invoice numbers for orders from a CSV file.
---

# Invoice Numbering

Use this skill to generate invoice numbers for a list of orders.

## Input
- A CSV file with columns: `order_id`, `customer_name`.

## Output
- A text file with lines: `invoice_number,order_id,customer_name`.

## Steps
1. Read the CSV file.
2. For each row (skipping the header), assign the next invoice number starting at INV-1000.
3. Write each line as `INV-<number>,<order_id>,<customer_name>` to the output file.

## Usage
Run the following command:
```bash
awk -F',' 'NR==1{next} {print "INV-" (1000 + NR - 2) "," $1 "," $2}' /home/user/projects/invoice_batch/orders.csv > /home/user/projects/invoice_batch/invoices.txt
```

This will produce the correct sequential invoice numbers.