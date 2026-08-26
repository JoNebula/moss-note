#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/load-env.sh"
UV_BIN="${UV_BIN:-$(command -v uv)}"
VLLM_WHEEL_INDEX="${VLLM_WHEEL_INDEX:-https://wheels.vllm.ai/68b4a1d582818e67adc903bf1b8fc5a5447da2fa/cu130}"

cd "$PROJECT_DIR"

echo "[1/5] 웹 앱 Python 3.12 환경 구성"
"$UV_BIN" sync --python 3.12 --group dev

echo "[2/5] vLLM 전용 환경 구성"
if [[ ! -x .venv-vllm/bin/python ]]; then
  "$UV_BIN" venv --python 3.12 .venv-vllm
fi

echo "[3/5] MOSS 및 Qwen 지원 vLLM 설치"
"$UV_BIN" pip install --python .venv-vllm/bin/python -U "vllm[audio]" \
  --torch-backend=auto \
  --extra-index-url "$VLLM_WHEEL_INDEX"

echo "[4/5] MOSS 모델 다운로드"
"$PROJECT_DIR/scripts/download-model.sh"

echo "[5/5] Qwen3.8-27B-FP8 모델 다운로드"
"$PROJECT_DIR/scripts/download-qwen.sh"

echo
echo "설치 완료. ./scripts/start.sh 로 실행하세요."
