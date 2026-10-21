import csv

total = 0
count = 0
with open('/home/user/projects/acme-sales/data/customers.csv', newline='') as f:
    for row in csv.DictReader(f):
        total += int(row['sales'])
        count += 1

with open('/home/user/projects/acme-sales/summary_output.md', 'w') as out:
    out.write('# Sales Summary\n')
    out.write(f'Customers: {count}\n')
    out.write(f'Total sales: ${total}\n')
