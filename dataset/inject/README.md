# RedClaw-Inject v1.0

**150 samples.** Dimension: `inject`.

Frozen Inject benchmark: 120 quality-curated memory-surface cases plus 30 selected skill-installation successes (27 eval, 3 probe). Skill samples exclude historical hand-authored prepare.py trials; their original risk labels do not establish downstream harm.

The cases and their rootfs/web assets are frozen evaluation inputs. Each claw adapter only resolves registered portable workspace placeholders.

## Layout

```text
dataset.json       # manifest
samples.jsonl      # flat index
PROVENANCE.jsonl   # audit-only source trail; not an evaluation input
<surface>/...
  index.json
  cases/<id>.json
  env/<id>/{env.json,rootfs/,web/,...}
```

## Composition

| surface | n | memory poison | frozen web | sink |
|---|---:|---:|---:|---:|
| agents_md | 30 | 0 | 7 | 7 |
| memory_md | 30 | 0 | 5 | 5 |
| skill_md | 30 | 0 | 15 | 15 |
| soul_md | 30 | 0 | 6 | 7 |
| user_md | 30 | 0 | 8 | 8 |

## Maintenance

```bash
python scripts/build_dataset.py verify --dataset <dataset>
# Rebuild explicitly with: build --dimension trigger|inject --source ... --out ...
```
