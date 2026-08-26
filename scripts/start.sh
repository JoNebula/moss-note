#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/load-env.sh"
VLLM_PORT="${MOSS_VLLM_PORT:-8001}"
QWEN_PORT="${QWEN_VLLM_PORT:-8002}"
QWEN_MODEL_PATH="${QWEN_MODEL_PATH:-$PROJECT_DIR/models/Qwen3.8-27B-FP8}"
mkdir -p "$PROJECT_DIR/logs"

cleanup() {
  if [[ -n "${VLLM_PID:-}" ]] && kill -0 "$VLLM_PID" 2>/dev/null; then
    kill "$VLLM_PID"
    wait "$VLLM_PID" 2>/dev/null || true
  fi
  if [[ -n "${QWEN_PID:-}" ]] && kill -0 "$QWEN_PID" 2>/dev/null; then
    kill "$QWEN_PID"
    wait "$QWEN_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "vLLM 시작 중 (GPU ${MOSS_CUDA_DEVICE:-0})... 로그: logs/vllm.log"
"$PROJECT_DIR/scripts/run-vllm.sh" >"$PROJECT_DIR/logs/vllm.log" 2>&1 &
VLLM_PID=$!

if [[ -f "$QWEN_MODEL_PATH/config.json" ]]; then
  echo "Qwen3.8 시작 중 (GPU ${QWEN_CUDA_DEVICE:-1})... 로그: logs/qwen.log"
  "$PROJECT_DIR/scripts/run-qwen.sh" >"$PROJECT_DIR/logs/qwen.log" 2>&1 &
  QWEN_PID=$!
else
  echo "Qwen 모델이 없어 AI 교정 서버는 건너뜁니다. ./scripts/download-qwen.sh 로 설치할 수 있습니다."
fi

for _ in $(seq 1 300); do
  if curl -fsS "http://127.0.0.1:$VLLM_PORT/v1/models" >/dev/null 2>&1; then
    if [[ -z "${QWEN_PID:-}" ]] || curl -fsS "http://127.0.0.1:$QWEN_PORT/v1/models" >/dev/null 2>&1; then
      if [[ -n "${QWEN_PID:-}" ]]; then
        echo "MOSS 및 Qwen 모델 준비 완료"
      else
        echo "MOSS 모델 준비 완료 (Qwen 미설치)"
      fi
      "$PROJECT_DIR/scripts/run-app.sh"
      exit $?
    fi
  fi
  if [[ -n "${QWEN_PID:-}" ]] && ! kill -0 "$QWEN_PID" 2>/dev/null; then
    echo "Qwen vLLM 시작 실패. logs/qwen.log의 마지막 내용을 확인합니다." >&2
    tail -n 60 "$PROJECT_DIR/logs/qwen.log" >&2
    exit 1
  fi
  if ! kill -0 "$VLLM_PID" 2>/dev/null; then
    echo "vLLM 시작 실패. logs/vllm.log의 마지막 내용을 확인합니다." >&2
    tail -n 60 "$PROJECT_DIR/logs/vllm.log" >&2
    exit 1
  fi
  sleep 2
done

echo "10분 안에 모델이 준비되지 않았습니다. logs/vllm.log와 logs/qwen.log를 확인하세요." >&2
exit 1
