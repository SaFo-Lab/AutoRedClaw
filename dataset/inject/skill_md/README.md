# Frozen skill-installation cases

30 unique automated successes: 27 observed at eval and 3 observed at probe. Cases retain their task, carrier, payload, and installation oracle. Only IDs and paths are normalized: skills destinations use `<SKILLS_ROOT>`, and business files use `/home/user`.

`cases/` holds the definitions; `env/` holds initial files and frozen web responses. No post-run workspace or runtime configuration is included.

Selection notes and source-run evidence are in the parent dataset's `PROVENANCE.jsonl` and `SELECTION.json`. The oracle proves skill installation only. Cross-adapter binding does not guarantee the same attack outcome.

The three probe additions succeeded on deepseek/deepseek-v4-flash-0731 with user-task checks passing and attack checks failing at baseline. Their source stage and existing eval outcomes are recorded in PROVENANCE.jsonl; probe success is not counted as eval success.
