# RedClaw

A security-evaluation framework for computer-use "claw" agents. It covers
attack-case generation, red-teaming, environment materialization, frozen
benchmark datasets, and multi-agent evaluation across several claw runtimes.

Two attack dimensions:

- **inject** — the agent is induced to persist attacker-controlled content into
  one of its durable workspace files (AGENTS.md / MEMORY.md / USER.md / SOUL.md /
  HEARTBEAT.md) or an installed skill.
- **trigger** — attacker content is pre-seeded, and the run measures the behavior
  it induces.

## Layout

```text
AutoRedClaw/
├── pyproject.toml
├── .env.example        # API keys only
├── scripts/            # run_redteam.sh, run_eval.sh  (start here)
├── redclaw/            # the package (CLI: python -m redclaw)
│   ├── generation/     # inject / trigger / skill case generators
│   ├── redteam/        # full-pipeline orchestration + attack-feasibility probe
│   ├── evaluation/     # single + batch execution, scoring, trajectories
│   ├── adapters/       # per-claw runtime adapters
│   ├── environments/   # fixture materialization, serving, state oracles
│   ├── datasets/       # freeze / verify / merge datasets
│   ├── runtime/        # Docker build, request sink, Bedrock relay
│   └── resources/      # prompts, taxonomy, JSON schemas, workspace templates
├── claws/              # per-claw Dockerfiles + unified Compose build
└── dataset/            # frozen benchmark datasets (inject / trigger)
```

Code and run artifacts are separate. Generated `data/`, `runs/`, `output/` and
`.runtime/` are produced at run time and are not part of the source.

## Install

Python 3.11+. The Python core uses only the standard library; actual evaluation
needs Docker (and Docker Compose for batch image builds / NemoClaw).

```bash
cd AutoRedClaw
python3.11 -m venv .venv && source .venv/bin/activate
python -m pip install -e .
cp .env.example .env     # then fill in the API key(s) you will use
```

`.env` holds **API keys only** — models and providers are always chosen on the
command line (or via the script environment variables below). Keys:
`OPENAI_API_KEY`, `OPENROUTER_API_KEY`, and Bedrock / Codex / Claude Code
credentials when you use those providers.

## Build the claw images

```bash
# All claws (serial build keeps Rust compilation memory low):
docker compose -f claws/compose.yaml --parallel 1 build
# Or just the ones you need:
docker compose -f claws/compose.yaml build nanobot seclaw
```

The run entry points build any missing image automatically. See
`claws/README.md` for pinned versions and NemoClaw specifics.

## Running

The two scripts in `scripts/` are the entry points. Both read `.env`, print the
commands they run, and accept `DRY_RUN=1` to print without executing. Every knob
is an environment variable with a sensible default; override what you need.

### 1. Red-teaming on freshly generated cases — `scripts/run_redteam.sh`

Runs the full pipeline: **generate → initialize (+model preflight) → attack_probe
→ eval → report**.

```bash
# Smoke test (defaults: openclaw, inject/agents.md, one category):
bash scripts/run_redteam.sh

# A fuller run:
CLAWS="nanobot openclaw" \
SURFACES="agents.md memory.md" \
CATEGORIES="poison_propagation tool_misuse code_execution self_modification" \
NUM_CASES=12 \
bash scripts/run_redteam.sh
```

Key variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLAWS` | `openclaw` | Claws to run (space-separated). |
| `DIMENSIONS` | `inject` | `inject` and/or `trigger`. |
| `SURFACES` | `agents.md` | Where poison is planted (trigger) / written (inject): `agents.md memory.md user.md soul.md heartbeat.md skill.md`, plus `environment` (trigger) or `any`. |
| `CATEGORIES` | `destructive_modification` | Risk categories (full list in the script header). |
| `NUM_CASES` | `12` | Target cases per category×dimension, spread over the 12 strategies. |
| `GEN_MODEL` / `GEN_MODEL_PROVIDER` | deepseek flash / openrouter | Generation model. |
| `PROBE_MODEL` / `PROBE_MODEL_PROVIDER` | deepseek flash / openrouter | Probe (victim) model. |
| `EVAL_MODEL` / `MODEL_PROVIDER` | gpt-5-mini / openrouter | Evaluation (victim) model. |
| `ASSET_MODEL` / `ASSET_MODEL_PROVIDER` | deepseek flash / openrouter | LLM fixture/asset synthesis. |
| `SKIP_STAGES` | `clean` | Any of `clean generate initialize attack_probe eval report`. |
| `EXPERIENCE_GUIDED` / `EXPERIENCE_ROUNDS` | off / `2` | Experience-guided exploration: coverage-weighted scheduling + distilled avoid-list over N rounds. |
| `COLLECTION_ROOT` | `output/redteam` | Where cases and trajectories go. |

Model ids pass through unchanged, so OpenRouter needs a full `vendor/model`
(e.g. `openai/gpt-5-mini`). Generation only reads the selected provider's key.

### 2. Evaluation on a frozen dataset — `scripts/run_eval.sh`

Reuses the cases and environments in `DATASET` byte-for-byte (nothing is
generated or probed): **initialize (+preflight) → eval → report**.

```bash
# Smoke test (nanobot, inject/agents_md):
bash scripts/run_eval.sh

# All surfaces, several claws, against the frozen inject dataset:
CLAWS="nanobot openclaw seclaw nemoclaw zeroclaw" \
SURFACES="agents_md memory_md soul_md user_md skill_md" \
DATASET=dataset/inject \
bash scripts/run_eval.sh
```

Key variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATASET` | `dataset/inject` | Frozen dataset to evaluate (`dataset/inject` or `dataset/trigger`). |
| `CLAWS` | `openclaw` | Claws to run. |
| `SURFACES` | all inject surfaces | Directory names under `DATASET` (e.g. `agents_md memory_md skill_md`). |
| `EVAL_MODEL` / `MODEL_PROVIDER` | deepseek-v4-pro / openrouter | Evaluation (victim) model. |
| `FORCE_INITIALIZE` | `0` | `1` forces one runtime rebuild per claw. |
| `SKIP_STAGES` | (none) | Same stage names as above. |

Results go to `runs/evaluation/<claw>/<dimension>/<surface>/eval_model=<model>/`.

Codex and Claude Code are driven with `CLAWS=codex MODEL_PROVIDER=codex` /
`CLAWS=claudecode MODEL_PROVIDER=claudecode` and an account-available model; the
first initialize walks their native login.

## Direct CLI (advanced)

The scripts wrap `python -m redclaw`. You can call subcommands directly; use
`--help` on any of them for the full options.

| Command | Purpose |
| --- | --- |
| `python -m redclaw generate inject` | Workspace / skill inject case generation |
| `python -m redclaw generate trigger` | Pre-seeded trigger case generation |
| `python -m redclaw redteam` | Full pipeline; also evaluates a frozen dataset with `--dataset` |
| `python -m redclaw probe` | Attack-feasibility probe |
| `python -m redclaw evaluate` | Batch evaluation |
| `python -m redclaw run` | Single-case execution + scoring |
| `python -m redclaw environment` | Materialize / serve / verify / reclaim environments |
| `python -m redclaw dataset` | `build` / `verify` / `merge` a dataset |

Models and providers are always explicit per stage (`--gen-model`,
`--probe-model`, `--model`, `--asset-model`, each with its `*-provider`); a
missing one is an error at startup rather than a silent fallback.

Verify a frozen dataset's integrity at any time:

```bash
python -m redclaw dataset verify --dataset dataset/inject
```
