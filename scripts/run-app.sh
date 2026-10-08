#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/load-env.sh"
cd "$PROJECT_DIR"

if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "웹 앱 환경이 없습니다. ./scripts/setup.sh 를 먼저 실행하세요." >&2
  exit 1
fi

export MOSS_VLLM_URL="${MOSS_VLLM_URL:-http://127.0.0.1:8001/v1}"
export TMPDIR="${TMPDIR:-${MOSS_CACHE_DIR:-/data/caches/moss-note}/tmp}"
mkdir -p "$TMPDIR"
exec .venv/bin/uvicorn app.main:app \
  --host "${MOSS_APP_HOST:-127.0.0.1}" \
  --port "${MOSS_APP_PORT:-8000}" \
  --workers 1
