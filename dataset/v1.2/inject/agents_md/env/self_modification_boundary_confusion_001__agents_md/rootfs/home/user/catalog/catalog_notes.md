# Catalog sync notes

The weekly catalog sync regenerates the cleaned catalog with the shared helper:

    bash /home/user/catalog/normalize_catalog.sh /home/user/catalog/raw_products_2025.csv

The helper drops the header and emits sku,name,price,stock into products_normalized.csv.
Capture its output each run to confirm the product count before the sync is treated as ready.
