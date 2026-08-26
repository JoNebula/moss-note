#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/load-env.sh"
MODEL_PATH="${QWEN_MODEL_PATH:-$PROJECT_DIR/models/Qwen3.8-27B-FP8}"
CUDA_DEVICE="${QWEN_CUDA_DEVICE:-1}"
VLLM_PORT="${QWEN_VLLM_PORT:-8002}"

if [[ ! -x "$PROJECT_DIR/.venv-vllm/bin/vllm" ]]; then
  echo "vLLM이 설치되지 않았습니다. ./scripts/setup.sh 를 먼저 실행하세요." >&2
  exit 1
fi
if [[ ! -f "$MODEL_PATH/config.json" ]]; then
  echo "Qwen 모델이 없습니다: $MODEL_PATH" >&2
  echo "./scripts/download-qwen.sh 를 실행하세요." >&2
  exit 1
fi

export CUDA_VISIBLE_DEVICES="$CUDA_DEVICE"
export HF_HOME="${HF_HOME:-$PROJECT_DIR/models/.huggingface}"
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

exec "$PROJECT_DIR/.venv-vllm/bin/vllm" serve "$MODEL_PATH" \
  --host 127.0.0.1 \
  --port "$VLLM_PORT" \
  --served-model-name qwen3.8-27b \
  --language-model-only \
  --max-model-len "${QWEN_MAX_MODEL_LEN:-32768}" \
  --kv-cache-dtype fp8 \
  --gpu-memory-utilization "${QWEN_GPU_MEMORY_UTILIZATION:-0.90}" \
  --max-num-seqs "${QWEN_MAX_NUM_SEQS:-2}" \
  --reasoning-parser qwen3 \
  --default-chat-template-kwargs '{"enable_thinking":false,"preserve_thinking":false}'
