#!/usr/bin/env bash
# Evaluate claws on a frozen dataset. Cases and environments are reused byte for
# byte from DATASET; nothing is generated, probed or re-synthesized. The run
# goes initialize (+ model preflight) -> eval -> report.
#
#   scripts/run_eval.sh                                         # smoke test: nanobot, inject/agents_md
#   DATASET=dataset/v1.1/trigger SURFACES="agents_md skill_md" scripts/run_eval.sh
#   CLAWS="nanobot openclaw seclaw nemoclaw zeroclaw" SURFACES="agents_md memory_md soul_md user_md skill_md" scripts/run_eval.sh
#   CLAWS=codex MODEL_PROVIDER=codex EVAL_MODEL=gpt-5.6-sol scripts/run_eval.sh        # ChatGPT login
#   CLAWS=codex MODEL_PROVIDER=openai EVAL_MODEL=gpt-5.4 scripts/run_eval.sh           # API key (models not on the ChatGPT account)
#   CLAWS=claudecode MODEL_PROVIDER=claudecode EVAL_MODEL=claude-sonnet-4-5 scripts/run_eval.sh
#   DRY_RUN=1 scripts/run_eval.sh                               # print the commands only
#
# Surfaces are the directory names under DATASET:
#   inject:  agents_md memory_md soul_md user_md skill_md
#   trigger: agents_md memory_md soul_md user_md environment skill_md
# Results go to runs/evaluation/<claw>/<dimension>/<surface>/eval_model=<model>/.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
ENV_FILE="${ENV_FILE:-.env}"
DATASET="${DATASET:-dataset/v1.1/inject}"
CLAWS=(${CLAWS:-openclaw})
SURFACES=(${SURFACES:-agents_md skill_md memory_md soul_md user_md})

# Model ids are passed through unchanged: OpenRouter needs vendor/model ids.
EVAL_MODEL="${EVAL_MODEL:-deepseek/deepseek-v4-pro}"
MODEL_PROVIDER="${MODEL_PROVIDER:-openrouter}"

TIMEOUT="${TIMEOUT:-600}"
EVAL_WORKERS="${EVAL_WORKERS:-10}"
SKIP_STAGES=(${SKIP_STAGES-})

if [ ! -f "$DATASET/dataset.json" ]; then
  printf 'Frozen dataset manifest does not exist: %s/dataset.json\n' "$DATASET" >&2
  exit 2
fi
if [ "${DRY_RUN:-0}" != "1" ] && [ ! -f "$ENV_FILE" ]; then
  printf 'Missing %s: copy .env.example and fill in the API keys.\n' "$ENV_FILE" >&2
  exit 2
fi

printf '[run] frozen dataset: %s\n' "$DATASET"
printf '[run] claws=%s surfaces=%s\n' "${CLAWS[*]}" "${SURFACES[*]}"
printf '[run] eval model: provider=%s model=%s\n' "$MODEL_PROVIDER" "$EVAL_MODEL"
printf '[run] skip stages: %s\n' "${SKIP_STAGES[*]:-(none)}"

for CLAW in "${CLAWS[@]}"; do
  # Force a runtime rebuild at most once per claw.
  FORCE_THIS="${FORCE_INITIALIZE:-0}"
  for SURFACE in "${SURFACES[@]}"; do
    CMD=(
      "$PYTHON" -m redclaw redteam
      --dataset "$DATASET"
      --trigger-surface "$SURFACE"
      --claw "$CLAW"
      --model "$EVAL_MODEL" --model-provider "$MODEL_PROVIDER"
      --timeout "$TIMEOUT"
      --eval-workers "$EVAL_WORKERS"
      --env-file "$ENV_FILE"
    )
    if [ "${#SKIP_STAGES[@]}" -gt 0 ]; then
      CMD+=(--skip "${SKIP_STAGES[@]}")
    fi
    if [ "$FORCE_THIS" = "1" ]; then
      CMD+=(--force-initialize)
    fi

    printf '\n[run] claw=%s surface=%s\n' "$CLAW" "$SURFACE"
    if [ "${DRY_RUN:-0}" = "1" ]; then
      printf '%q ' "${CMD[@]}"; printf '\n'
    else
      "${CMD[@]}"
    fi
    FORCE_THIS=0
  done
done
