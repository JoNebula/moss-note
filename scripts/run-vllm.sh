#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_DIR/scripts/load-env.sh"
MODEL_PATH="${MOSS_MODEL_PATH:-$PROJECT_DIR/models/MOSS-Transcribe-Diarize}"
if [[ "${MOSS_MANAGED_MODELS:-false}" == "true" ]]; then
  MODEL_PATH="$(PYTHONPATH="$PROJECT_DIR" "$PROJECT_DIR/.venv/bin/python" -m app.model_runtime)"
fi
CUDA_DEVICE="${MOSS_CUDA_DEVICE:-0}"
VLLM_PORT="${MOSS_VLLM_PORT:-8001}"
AUDIO_LIMIT_SECONDS="${MOSS_CHUNK_SECONDS:-5400}"
AUDIO_PROFILE_SAMPLES=$((AUDIO_LIMIT_SECONDS * 16000))

if [[ ! -x "$PROJECT_DIR/.venv-vllm/bin/vllm" ]]; then
  echo "vLLM이 설치되지 않았습니다. ./scripts/setup.sh 를 먼저 실행하세요." >&2
  exit 1
fi
if [[ ! -f "$MODEL_PATH/config.json" ]]; then
  echo "모델이 없습니다: $MODEL_PATH" >&2
  echo "./scripts/download-model.sh 를 실행하세요." >&2
  exit 1
fi

export CUDA_VISIBLE_DEVICES="$CUDA_DEVICE"
export HF_HOME="${HF_HOME:-$PROJECT_DIR/models/.huggingface}"
export VLLM_MAX_AUDIO_CLIP_FILESIZE_MB="${VLLM_MAX_AUDIO_CLIP_FILESIZE_MB:-1024}"
export VLLM_MAX_AUDIO_DECODE_DURATION_S="${VLLM_MAX_AUDIO_DECODE_DURATION_S:-5400}"
# The pinned CUDA 13 wheel can otherwise JIT a FlashInfer sampler against the
# host nvcc/CCCL headers. Native sampling is reliable and immaterial for
# temperature=0 transcription throughput.
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

EXTRA_ARGS=()
if [[ -n "${MOSS_MAX_MODEL_LEN:-}" ]]; then
  EXTRA_ARGS+=(--max-model-len "$MOSS_MAX_MODEL_LEN")
fi
if [[ "${MOSS_ENFORCE_EAGER:-false}" == "true" ]]; then
  EXTRA_ARGS+=(--enforce-eager)
fi
if [[ -n "${MOSS_ATTENTION_BACKEND:-}" ]]; then
  EXTRA_ARGS+=(--attention-backend "$MOSS_ATTENTION_BACKEND")
fi
if [[ -n "${MOSS_COMPILATION_CONFIG:-}" ]]; then
  EXTRA_ARGS+=(--compilation-config "$MOSS_COMPILATION_CONFIG")
fi
if [[ -n "${MOSS_KV_CACHE_MEMORY_BYTES:-}" ]]; then
  EXTRA_ARGS+=(--kv-cache-memory-bytes "$MOSS_KV_CACHE_MEMORY_BYTES")
fi

exec "$PROJECT_DIR/.venv-vllm/bin/vllm" serve "$MODEL_PATH" \
  --host 127.0.0.1 \
  --port "$VLLM_PORT" \
  --served-model-name moss-mtd \
  --trust-remote-code \
  --limit-mm-per-prompt "{\"audio\":{\"count\":1,\"length\":$AUDIO_PROFILE_SAMPLES}}" \
  --max-num-batched-tokens "${MOSS_MAX_BATCHED_TOKENS:-131072}" \
  --gpu-memory-utilization "${MOSS_GPU_MEMORY_UTILIZATION:-0.45}" \
  --max-num-seqs "${MOSS_MAX_NUM_SEQS:-1}" \
  "${EXTRA_ARGS[@]}"
