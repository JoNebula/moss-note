#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/load-env.sh"

failures=0
check_command() {
  if command -v "$1" >/dev/null 2>&1; then
    printf 'ok   %s\n' "$1"
  else
    printf 'miss %s\n' "$1"
    failures=$((failures + 1))
  fi
}

for command_name in uv ffmpeg ffprobe curl nvidia-smi; do
  check_command "$command_name"
done

if [[ -f "${MOSS_MODEL_PATH:-$PROJECT_DIR/models/MOSS-Transcribe-Diarize}/config.json" ]]; then
  echo "ok   MOSS model"
else
  echo "miss MOSS model (run ./scripts/download-model.sh)"
  failures=$((failures + 1))
fi

if [[ -f "${QWEN_MODEL_PATH:-$PROJECT_DIR/models/Qwen3.8-27B-FP8}/config.json" ]]; then
  echo "ok   Qwen model"
elif [[ "${MOSS_REQUIRE_QWEN:-true}" == "true" ]]; then
  echo "miss Qwen model (run ./scripts/download-qwen.sh)"
  failures=$((failures + 1))
else
  echo "skip Qwen model (MOSS_REQUIRE_QWEN=false)"
fi

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader
fi

exit "$failures"
