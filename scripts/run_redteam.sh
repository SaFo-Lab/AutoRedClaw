#!/usr/bin/env bash
# Run the full red-teaming pipeline on freshly generated cases:
#   generate -> initialize (+ model preflight) -> attack_probe -> eval -> report
#
# Defaults are a small smoke test (one claw, one surface, one category). Every
# model and provider is set explicitly below; override any of them through the
# environment, e.g.
#   CLAWS="nanobot seclaw" SURFACES="agents.md memory.md" NUM_CASES=12 scripts/run_redteam.sh
#   DIMENSIONS=trigger SURFACES=skill.md scripts/run_redteam.sh
#   DRY_RUN=1 scripts/run_redteam.sh          # print the commands only
#
# SURFACES means where the poison is PLANTED for trigger, and where the agent
# must WRITE for inject: agents.md memory.md user.md soul.md heartbeat.md skill.md,
# plus environment (trigger only) or any.
#
# SKIP_STAGES (default: clean) takes any of: clean generate initialize
# attack_probe eval report. `clean` deletes this claw/surface's cases and runs
# under COLLECTION_ROOT, so it is skipped by default to keep reruns additive.
# NUM_CASES is the target total per category x dimension, spread round-robin over
# the 12 attack strategies (12 -> one case per strategy); a rerun tops files up.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
ENV_FILE="${ENV_FILE:-.env}"
COLLECTION_ROOT="${COLLECTION_ROOT:-output/redteam}"

CLAWS=(${CLAWS:-openclaw})
DIMENSIONS=(${DIMENSIONS:-inject})
SURFACES=(${SURFACES:-agents.md})
CATEGORIES=(${CATEGORIES:-destructive_modification})
# Full category list:
#   goal_hijack_malicious_operation privacy_leakage secret_credential_exfiltration
#   tool_misuse destructive_modification code_execution privilege_override
#   poison_propagation self_modification
NUM_CASES="${NUM_CASES:-12}"

# Model ids are passed through unchanged: OpenRouter needs vendor/model ids.
GEN_MODEL="${GEN_MODEL:-deepseek/deepseek-v4-flash}"
GEN_MODEL_PROVIDER="${GEN_MODEL_PROVIDER:-openrouter}"
PROBE_MODEL="${PROBE_MODEL:-deepseek/deepseek-v4-flash}"
PROBE_MODEL_PROVIDER="${PROBE_MODEL_PROVIDER:-openrouter}"
ASSET_MODEL="${ASSET_MODEL:-deepseek/deepseek-v4-flash}"
ASSET_MODEL_PROVIDER="${ASSET_MODEL_PROVIDER:-openrouter}"
EVAL_MODEL="${EVAL_MODEL:-openai/gpt-5-mini}"
MODEL_PROVIDER="${MODEL_PROVIDER:-openrouter}"

WEB_ASSETS="${WEB_ASSETS:-llm}"
FILE_ASSETS="${FILE_ASSETS:-llm}"
OS_PROFILE="${OS_PROFILE:-linux}"
REASONING_EFFORT="${REASONING_EFFORT:-medium}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-5}"
TIMEOUT="${TIMEOUT:-600}"
GEN_WORKERS="${GEN_WORKERS:-12}"
PROBE_WORKERS="${PROBE_WORKERS:-12}"
EVAL_WORKERS="${EVAL_WORKERS:-12}"
SKIP_STAGES=(${SKIP_STAGES-clean})

if [ "${DRY_RUN:-0}" != "1" ] && [ ! -f "$ENV_FILE" ]; then
  printf 'Missing %s: copy .env.example and fill in the API keys.\n' "$ENV_FILE" >&2
  exit 2
fi

printf '[run] claws=%s dimensions=%s surfaces=%s\n' "${CLAWS[*]}" "${DIMENSIONS[*]}" "${SURFACES[*]}"
printf '[run] categories=%s num=%s collection root=%s\n' "${CATEGORIES[*]}" "$NUM_CASES" "$COLLECTION_ROOT"
printf '[run] gen=%s/%s probe=%s/%s asset=%s/%s eval=%s/%s\n' \
  "$GEN_MODEL_PROVIDER" "$GEN_MODEL" "$PROBE_MODEL_PROVIDER" "$PROBE_MODEL" \
  "$ASSET_MODEL_PROVIDER" "$ASSET_MODEL" "$MODEL_PROVIDER" "$EVAL_MODEL"
printf '[run] skip stages: %s\n' "${SKIP_STAGES[*]:-(none)}"

for CLAW in "${CLAWS[@]}"; do
  # Force a runtime rebuild at most once per claw.
  FORCE_THIS="${FORCE_INITIALIZE:-0}"
  for SURFACE in "${SURFACES[@]}"; do
    CMD=(
      "$PYTHON" -m redclaw redteam
      --claw "$CLAW"
      --dimensions "${DIMENSIONS[@]}"
      --trigger-surface "$SURFACE"
      --categories "${CATEGORIES[@]}"
      --num "$NUM_CASES"
      --collection-root "$COLLECTION_ROOT"
      --gen-model "$GEN_MODEL" --gen-model-provider "$GEN_MODEL_PROVIDER"
      --probe-model "$PROBE_MODEL" --probe-model-provider "$PROBE_MODEL_PROVIDER"
      --asset-model "$ASSET_MODEL" --asset-model-provider "$ASSET_MODEL_PROVIDER"
      --model "$EVAL_MODEL" --model-provider "$MODEL_PROVIDER"
      --web-assets "$WEB_ASSETS" --file-assets "$FILE_ASSETS"
      --os-profile "$OS_PROFILE"
      --reasoning-effort "$REASONING_EFFORT"
      --max-attempts "$MAX_ATTEMPTS"
      --timeout "$TIMEOUT"
      --gen-workers "$GEN_WORKERS"
      --probe-workers "$PROBE_WORKERS"
      --eval-workers "$EVAL_WORKERS"
      --env-file "$ENV_FILE"
    )
    if [ "${#SKIP_STAGES[@]}" -gt 0 ]; then
      CMD+=(--skip "${SKIP_STAGES[@]}")
    fi
    # Eval reuses the probe-stage environment by default; REUSE_ASSETS=0 re-synthesizes it.
    if [ "${REUSE_ASSETS:-1}" = "0" ]; then
      CMD+=(--no-reuse-assets)
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
