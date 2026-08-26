#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/load-env.sh"
MODEL_ID="${QWEN_MODEL_ID:-Qwen/Qwen3.8-27B-FP8}"
MODEL_DIR="${QWEN_MODEL_PATH:-$PROJECT_DIR/models/Qwen3.8-27B-FP8}"

if [[ ! -x "$PROJECT_DIR/.venv-vllm/bin/hf" ]]; then
  echo "vLLM 환경이 없습니다. 먼저 ./scripts/setup.sh 를 실행하세요." >&2
  exit 1
fi

mkdir -p "$PROJECT_DIR/models"
echo "$MODEL_ID -> $MODEL_DIR"
"$PROJECT_DIR/.venv-vllm/bin/hf" download "$MODEL_ID" --local-dir "$MODEL_DIR"
