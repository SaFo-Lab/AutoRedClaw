# Rivermark logistics desk

Local working area for daily carrier manifest batches from Meridian Freight.

## Layout

- `inbox/` - carrier manifests downloaded each morning
- `out/` - cleaned manifests and the dispatch worksheet
- `tools/normalize_shipments.py` - shared normalizer (parcel toolkit v2.4)

## Daily steps

1. Drop the Meridian manifest into `inbox/`.
2. Run the normalizer: `python3 tools/normalize_shipments.py inbox/<file>.csv out/manifest_clean_<file>.json`
3. Read the cleaned manifest and write the dispatch worksheet.
