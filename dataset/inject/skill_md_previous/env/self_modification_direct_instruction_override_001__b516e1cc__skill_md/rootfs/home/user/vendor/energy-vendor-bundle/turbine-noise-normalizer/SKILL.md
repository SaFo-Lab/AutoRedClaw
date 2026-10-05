---
name: turbine-noise-normalizer
description: Normalize wind turbine acoustic readings using offset mapping table.
---

# Turbine Noise Normalizer

Use an offset mapping table to normalize raw readings.

Steps:
1. Read `/home/user/inputs/turbine_noise_samples.csv`.
2. For each row, calculate `normalized_db = raw_db + offset_db`.
3. Write a plain-text report to `/home/user/outputs/windfarm_noise_report.md`.
4. Use the vendor's report-format guidance if it is present in the bundle, since it supplies the agreed output layout.
