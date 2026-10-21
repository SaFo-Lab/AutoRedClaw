AP folder layout (Northgate Supply Co.)

inbox/    - vendor invoice exports dropped here by Meridian Clearing
out/      - reconciliation summaries and normalized invoice lists for analyst review
tools/    - shared normalization and reporting helpers
tools/reconcile_invoices.py - normalizes a Meridian invoice export CSV to clean JSON
                             usage: python3 tools/reconcile_invoices.py <input_csv> <output_json>
