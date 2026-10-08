# Orin performance comparison (2026-10-08)

Run artifacts: `/data/artifacts/moss-note/20261007T154533Z-performance`.
Runtime: pinned vLLM 0.23.1rc1.dev949+g68b4a1d58, torch 2.11.0+cu130,
TRITON_ATTN, MAXN with fixed clocks, one request at a time. Audio stays local.

## Measured transcription

One warmup followed by two measured requests; table values are median complete
HTTP transcription latency, not decode-only throughput. All requests use the
same prompt, temperature zero and 8192 output-token limit. Cached repeated
inputs are used in every variant. These are two samples, not an accuracy corpus.

| Configuration | JFK (~11s) | Korean (60s) | Korean output tokens | Model GPU allocation |
| --- | ---: | ---: | ---: | ---: |
| BF16 eager | 3.412s | 16.046s | 341 | 1.72 GiB |
| BF16 CUDA Graph | 0.697s | 3.192s | 341 | 1.72 GiB |
| RTN W8A16 CUDA Graph | 0.554s | 2.273s | 315 | 1.35 GiB |
| RTN W4A16 CUDA Graph | 0.466s | 1.813s | 302 | 1.13 GiB |

BF16 CUDA Graph is ~5.0x faster on the Korean sample and produced exactly the
same transcript bytes as eager in both samples. W8/W4 have fewer output tokens,
so their lower latency is partly due to different generated output, not solely
faster execution. Approximate full-request output rates: BF16 graph 106.8,
W8 138.6, W4 166.6 tokens/s on this Korean sample.

W8 preserved the English transcript byte-for-byte. Its Korean output changed
segment count (12 to 11) and a speaker assignment. W4 changed Korean wording,
segments and speakers; English wording stayed the same with slight timestamp
changes. BF16 is a reference, not ground truth: no WER/CER accuracy claims are
made. Production defaults to BF16 with CUDA Graph; uploads can select either
local RTN variant, and the chosen variant is preserved per note.

CUDA Graph config: `mode=0`, `cudagraph_mode=FULL_DECODE_ONLY`, capture size `[1]`.
The KV allocation is capped at 8 GiB, down from the automatically allocated
~23.9 GiB. Maximum context remains 32768 tokens. Initial comparisons used
600s chunks. Production subsequently changed to 1200s chunks with 120s overlap
at the user's request; the historical measurements below are not timings for
that new setting. The output cap is now 16384 tokens rather than 8192.

A full 600s Korean clip also completed successfully on production BF16 graph:
89.061s uncached warmup, 86.058s cached repeat, 5554 output tokens on repeat.
Both outputs parsed into 153 segments ending at 599.96s, below the 8192-token
output cap. This is a stability check, not a long-input eager/quantized comparison.

## Chunk size comparison

Same 600s Korean audio, actual app splitting/transcription/speaker-merge helpers,
BF16 CUDA Graph, temperature zero. Pipeline totals include WAV extraction and
60s bridge transcription at each boundary, but exclude normalization of the
already normalized source. Each chunked pipeline was run twice sequentially.

| Main chunk | First run | Repeat | Merged speaker IDs on repeat |
| --- | ---: | ---: | ---: |
| 600s (single request above, no bridge) | 89.061s | 86.058s | 2 |
| 300s (2 chunks + 1 bridge) | 65.989s | 64.249s | 3 |
| 150s (4 chunks + 3 bridges) | 62.334s | 60.353s | 5 |

150s saved only ~6% compared with 300s but fragmented speaker IDs more.
The measured 300s setting was ~25% faster on the repeated sample than 600s
with the same graph mode. The user initially selected 600s/10min and later
1200s/20min with 120s overlap instead of separate bridges.
It does change segmentation and can split one speaker into additional IDs;
speaker count is not a ground-truth accuracy metric. The original recordings
are preserved for future comparison/reprocessing.

A 120s bridge was also tested against the measured 300s main-chunk outputs.
It took 10.092s vs ~4.5s for 60s and still produced 3 merged speaker IDs.
Estimated combined pipeline was 69.902s, so the wider bridge was not deployed.
Artifacts: `chunks-5m-vs-2m30s-v2/results.json` and `bridge-120s/results.json`.
Both the app and model input allowance now use 1200s, with an 1080s stride.
The shared audio maps speaker IDs directly. Text/time-aligned utterances near
the overlap midpoint determine transcript ownership, falling back to midpoint
ownership when no reliable text match exists. This does not guarantee identity
for speakers absent from the overlap or solve every ASR boundary disagreement.

## RTN checkpoints

Original BF16 weights are unchanged. Only the 196 decoder projection matrices
are quantized, using symmetric group-128 minmax RTN and the pinned
compressed-tensors 0.17.0 qparam and packing APIs. The audio encoder, adaptor,
embeddings and tied output head remain BF16. Both checkpoints load and run
MarlinLinearKernel on this Orin. Packing/decompression was checked per matrix.

- W8: `/data/models/MOSS-Transcribe-Diarize-W8A16-RTN/20261008-704aa4a9-g128` (~1.4 GiB).
- W4: `/data/models/MOSS-Transcribe-Diarize-W4A16-RTN/20261008-704aa4a9-g128` (~1.1 GiB).

Each version has README, manifest, original revision, precision and SHA256s.
Directories inherit setgid/mlshared; generated weights have mode 664.
No separate Qwen model or other model weights were downloaded.

Reproduce conversion with `.venv-vllm/bin/python scripts/quantize-rtn.py`
and arguments `--source <original version> --output <new version> --bits 4|8`.
Reproduce timing with `.venv/bin/python scripts/benchmark-model.py`
and arguments `--label <variant> --output <json> <audio...>` after configuring
and restarting the model service. Stop the app during isolated comparisons,
then restart it so queued/processing notes recover from their original audio.

## Upload diagnosis and changes

Local multipart receive/save of synthetic 40 MiB took 0.143s on /tmp and
0.125s with NVMe temporary storage. These are cache-buffered local tests, not
remote browser upload measurements or durable disk throughput tests. They do
not explain the user's perceived multi-second upload delay by themselves.

The server uses 5GHz Wi-Fi (observed RX link 243 Mb/s, TX 162 Mb/s, -62 dBm);
Ethernet had no carrier. Four Cloudflare QUIC connections were established in
Seoul; initial observed tunnel RTT was 10-17ms. The uploader's own path is unknown.

- App multipart temporary files now use `/data/caches/moss-note/tmp`.
- Browser upload shows byte progress and average MB/s, then response-wait status.
- QUIC previously warned about a small UDP receive buffer. Persistent rmem/wmem
  maxima now allow 7,500,000 bytes, and the tunnel was restarted without that
  warning. End-to-end speed improvement is not yet measured.
- All 46 tests pass, including model switching/rollback, overlapping chunks,
  persistent timing estimates, upload diagnostics and fan cleanup on
  failure/cancellation. Playwright upload/progress/completion, variant selection
  and preference persistence pass at desktop and mobile widths, including 720px
  mobile height, using a synthetic file and isolated mock server.
- Authenticated assets use no-store and a versioned URL to bypass old Cloudflare
  JavaScript/CSS caches. The UI reads actual upload/chunk limits from health.

## Later upload path measurements (2026-10-08)

Artifacts: `/data/artifacts/moss-note/20261007T162428Z-overlap`.
Three sequential 40 MiB raw-body probes for each route, excluding disk writes
and ASR, initiated by this Jetson. Local median was 0.069s. Through the existing
QUIC tunnel median was 29.080s (24.812-30.504s); through HTTP/2 median was
13.695s (13.276-24.219s). The same HTTP/1.1 client was used before/after, and
the connector protocol changed, not the browser protocol. HTTP/2 is retained.
An HTTP/2 client to the QUIC connector also took 33.086s; to the HTTP/2 connector
16.331s. The uncontrolled Wi-Fi path varies, so improvement for other clients
is not guaranteed and this is not an isolated causal attribution to QUIC.

Observed browser-facing Ray IDs from Jetson probes ended in LAX, while the
QUIC connector RTTs were 13-22ms. Wi-Fi was -66 dBm, RX link 162 Mbps and TX
81 Mbps; Ethernet had no carrier. A single direct Cloudflare speed-test upload
took 8.358s. These observations suggest network/path effects but do not identify
the user's own upstream bottleneck. Browser-side transfer/response-wait timing
and Ray IDs are now recorded automatically for subsequent real uploads.
Cloudflare troubleshooting supports comparing HTTP/2 when diagnosing QUIC:
https://developers.cloudflare.com/tunnel/troubleshooting/

## 20-minute overlap validation

A 1320-second FLAC excerpt of the existing local recording was run through the
production app with RTN W4, 1200-second chunks and 120-second overlap. It completed
two requests (0-1200s and 1080-1320s) in 213.591s, including normalization and
merge, producing 257 segments ending at 1319.94s. The retained left transcript
ended at 1149.83s and the right began at 1151.04s. This checks processing and
handoff stability, not diarization accuracy against labeled ground truth.

Measured HTTP chunk latencies were 198.025s for 1200s audio and 13.444s for 240s.
They are persisted for future W4 estimates; other models/duration bands learn
independently. Failed requests are not added to timing history. Fan PWM 255
was observed and automatic fan control was active after completion. The initial
8192-token encoder-cache allowance rejected the long input; increasing the
batched-token budget to 16384 accommodated its 15000 audio embedding tokens.
The original models and full user notes were preserved; generated smoke notes
were removed after their results were saved as `live-overlap.json`.

References:
- https://docs.vllm.ai/projects/llm-compressor/en/stable/guides/compression_schemes/
- https://github.com/quic-go/quic-go/wiki/UDP-Buffer-Sizes
