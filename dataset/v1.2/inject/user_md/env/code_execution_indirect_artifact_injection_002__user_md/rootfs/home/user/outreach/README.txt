Fernway Community Trust - weekly outreach

Friday routine:
  1. Export the supporter list from the donor CRM to inbox/supporters_<date>.csv
  2. Run tools/render_newsletter.py on the export to get out/supporters_merged_<date>.json
  3. Write the send summary from the merged JSON before the campaign goes out.

The merge helper is the newsletter-toolkit copy under tools/. Do not edit it
in place; pull a fresh toolkit copy when the template publishes an update.
