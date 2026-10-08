# Jetson deployment

Project: `/data/projects/jetson/moss_stt`

Upstream app revision: `2fbb57e40d80009e6e6a3195085001ce133a1713`.

Model: `/data/models/MOSS-Transcribe-Diarize/20260902-704aa4a9`

Data: `/data/artifacts/moss-note/20261006T062500Z-service`

The model is the official BF16 safetensors snapshot at revision
`704aa4a9c304e8520be88901e0d1960158ef5b15`. `manifest.json` records upstream
verification and SHA256 checksums. Qwen correction is disabled and its model
is not downloaded. The Qwen architecture embedded in MOSS is part of MOSS,
not a separate Qwen correction model.

## Codex Correction And Summary

The AI correction button runs the installed Codex CLI with the deployment
user's existing ChatGPT login, fixed `gpt-6.1-sol`, `medium` reasoning and Fast mode.
The installed catalog advertises Fast as `priority`; each invocation explicitly
sets `service_tier="priority"` and enables `fast_mode`, independently of user config.
The requested tier is recorded in the job manifest and note metadata. Official
documentation says included subscription usage is consumed at 2.5x Standard,
while purchased credits are billed at 2x; actual latency and eligibility vary.
No API key or separate Qwen model is needed. Unlike local MOSS transcription,
this optional step sends transcript text, note title and supplied terminology
to OpenAI. It consumes the logged-in account's Codex usage allowance; the CLI
also has prompt overhead, so usage is not just the transcript token count.

The serial correction worker snapshots the edited transcript when queued.
It writes the complete transcript to `input-transcript.json` and includes it in
`request.txt`, which is passed as a file handle to the CLI's standard input.
One CLI invocation returns a `result.json` containing only changed utterances
and a separate structured summary with evidence utterance IDs. Every utterance
is reviewed; unchanged ones are restored from the immutable job snapshot to
produce complete `corrected.txt`, `corrected.json` and normal exports. The output
can be an empty changes array when nothing needs editing. Unknown/duplicate IDs,
extra fields and unsafe number/URL changes reject the entire response. No per-window
correction or separate summary invocation is used. File input still consumes
tokens and is subject to model limits; the complete input document is capped at
1 MB. IDs and numeric/URL changes are validated before corrected text and summary
are committed together. Original ASR, user-edited text, speaker IDs and timestamps
are preserved. Failed jobs preserve previous valid results and retry the complete
latest edited transcript. Restart recovery processes the queued snapshot as a
whole file; obsolete window checkpoints are not reused. Summary evidence IDs are
kept solely for internal validation; the UI and Markdown/TXT files omit source
IDs, references and timestamp annotations. This also applies to older summaries
without re-running Codex.
Correction files use the existing TXT/SRT/VTT/JSON exports; summary has separate
Markdown/TXT downloads and a separate tab.

Speaker checkboxes and the target selector merge selected speakers into an
existing speaker. This updates edited/corrected/snapshotted speaker IDs but not
the raw ASR, timestamps or text. The latest 20 merges can be undone; undo restores
speaker assignments by utterance ID without reverting subsequent text edits.
Merge and speaker editing are blocked during AI jobs, and pending autosaves are
flushed before speaker changes. Existing summaries are marked stale after a
speaker or corrected-text change, including in their downloads; merging never
silently invokes Codex. Undo removes the stale flag only when the old summary
and its original corrected-text/speaker context are restored.

Each invocation uses an ephemeral session, read-only sandbox, isolated working
directory and transcript-specific base instructions. User configuration, project
instructions, shell execution, browser, plugins, apps and multi-agent tools are
disabled. App secrets/API keys are not forwarded to the child. Credentials stay
in the existing Codex credential store and are never saved with transcripts.
If login expires, run `/home/jetson/.local/bin/codex login` as `jetson` and retry.

`MOSS_CODEX_BINARY` and `MOSS_CODEX_TIMEOUT_SECONDS` are set in `.env`; model/effort are intentionally
fixed in `app/codex_postprocess.py`. CLI caches/temp files use `/data/caches`.
New jobs record `correction_output_mode=changes`; old completed jobs remain
`full` and are not reprocessed. Experimental compact input and catalog snapshots
are benchmark-only options, not production defaults. The production CLI still
has no file-reading tools or shell access.
Prompts, schemas, validated outputs, usage manifests and invocation logs are in
`/data/artifacts/moss-note/20261006T062500Z-service/codex-runs/<UTC-time>-<note-id>-<run-id>/`.
Job directories restrict access to `jetson` and the inherited `mlshared` group.
These may contain sensitive transcript text and should be included in retention
and backup policy. Deleting a note does not erase these diagnostic job artifacts.
Unit tests do not contact OpenAI. `scripts/smoke-codex.py` uses only synthetic
meeting text to verify a real authenticated correction and summary invocation.
`scripts/smoke-codex.py --api` verifies the live app with a temporary synthetic
note, archives its outputs, and deletes only that test note after success.
`scripts/benchmark-codex.py` compares direct full output, model-side file reading,
changes-only output and optional compact input/catalog caching using identical
synthetic text. It reverses test order on alternating rounds, validates known
technical-term fixes, metadata preservation and summary evidence, and records
latency, tokens and whether file-reading commands actually ran. File reading
temporarily enables read-only tools only for these synthetic benchmark jobs;
it is not enabled in the service. Results live in timestamped
`/data/artifacts/moss-note/*-codex-latency-benchmark-*/` directories.

Unit coverage includes subprocess timeout/cancellation cleanup, exact model/effort,
321 utterances in one invocation, immutable snapshots, atomic failures, multi-speaker
merge and text-preserving undo. `tests/correction-ui.cjs` covers desktop/mobile,
safe rendering, audio continuity, downloads, merge and undo. Current one-call
synthetic verification artifacts use
`/data/artifacts/moss-note/20261008-whole-file-speakers/`; older two-call results
remain in `/data/artifacts/moss-note/20261008-codex-smoke/`.
Job directories are mode 2770, owned by `jetson:mlshared`; files inherit the shared
ACL behind those directories. JSON results/usage manifests and UTF-8 TXT/SRT/Markdown
outputs stay on NVMe. Synthetic examples are not timing guarantees for long recordings.

2026-10-08 whole-file/Fast validation: 77 pytest cases passed. Live authenticated
API correction returned corrected text and summary in one CLI invocation with
the requested `priority` tier; the two-utterance smoke job took 15.10 seconds
(6667 input and 218 output tokens). TXT/SRT/Markdown downloads, source-free
summary exports, speaker merge and undo preserving later text edits passed.
Playwright passed at 1280x1000, 390x900 and 390x720, plus the upload/model-choice
regression suite. Existing five notes were preserved and only temporary synthetic
test notes were deleted. Public HTTPS returned the new assets successfully.
The validation/backup directory uses about 2.1 MiB; the latest service job uses
about 11 KiB in UTF-8 JSON/TXT/Markdown and logs, mode 2770, `jetson:mlshared`.
MOSS remains on RTN W4; network exposure and the Cloudflare tunnel were unchanged.

2026-10-08 latency investigation: the existing 570-utterance job completed
without interruption in 871.33 seconds, returning 16406 tokens although only
237 utterances changed. Catalog refresh timeouts also occurred; this does not
establish that every delay was generation time. Synthetic 64-utterance runs
measured full-output times of 168.04/29.85 seconds versus changes-only times of
13.14/38.19 seconds (medians 98.94 versus 25.67 seconds). Mean output fell from
2476 to 667 tokens, about 73%. All four known terminology errors were corrected
in both changes-only runs, while all IDs/timestamps/speakers were preserved.
Network/service variance is substantial; these small samples are not a general
speedup guarantee. Model-side file-reading attempts timed out or failed content
validation, so no successful file-reading latency is claimed. A compact-input/
local-catalog experiment passed once in 58.24 seconds but was not selected.
Production now uses the validated changes-only response with the existing full
stdin transcript input, fixed model/effort/Fast and all model tools disabled.
86 unit tests and the desktop/mobile suite passed. Live API smoke returned one
edit for two source utterances and reconstructed the complete corrected file,
summary, exports and speaker undo successfully in 6.05 seconds. Existing six
notes were preserved. Comparison JSON and the SQLite backup are under
`/data/artifacts/moss-note/20261007T182147Z-codex-latency-deployment/`.

Official CLI references:
- https://learn.chatgpt.com/docs/non-interactive-mode
- https://learn.chatgpt.com/docs/config-file/config-reference
- https://learn.chatgpt.com/docs/agent-configuration/speed

## Services

```sh
sudo systemctl status moss-note-app moss-note-model moss-note-tunnel
sudo journalctl -u moss-note-model -n 80 --no-pager
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

Web and inference listen on localhost ports 8000 and 8001. Login credentials
are in `.deployment-credentials`, readable only by the deployment user.
Configuration is in `.env`. Services use one app worker and restart after
failure. Uploads and SQLite data should be backed up together.

Browsers use `/login` with the same deployment credentials and a signed
12-hour session cookie (HttpOnly, SameSite Strict, Secure over HTTPS).
Login uses a signed CSRF token; cookie-authenticated write requests require
the same origin. Changing the configured credentials revokes existing sessions.
HTTP Basic authentication remains available to API clients.

Current configuration uses 1200-second transcription chunks with 120 seconds
of shared audio: 0-20min, 18-38min, 36-56min. The overlapping audio directly
connects speaker IDs, and a shared utterance near its midpoint is retained
from only one chunk. When no reliable text match exists, midpoint ownership
is used. Speakers absent from the shared audio cannot be reliably connected.
Separate bridge requests are no longer used in production. Each new note
records its actual chunk length and overlap; existing completed notes are
not reprocessed. The output cap is 16384 tokens and model audio allowance is
1200 seconds. The batched-token budget is 16384, needed for the 15000 audio
embedding tokens produced by a 20-minute input on the pinned vLLM runtime.
Maximum upload is 0.09 GiB (96.6 MB) to stay below the
Cloudflare Free/Pro 100 MB request limit. Larger recordings can be compressed
before uploading. No router port forwarding is needed.

Chunk timing history is stored in `timings.sqlite3` under the service data
directory. It keeps the latest 40 successful timings for each model variant
and duration band (1/5/10/20/90 minutes), using median seconds per audio second.
New duration bands use a rough initial estimate until measured samples exist.
The API exposes estimated progress/remaining time during transcription; waiting
and model preparation are indeterminate, and only completion reaches 100%.
The running chunk is capped at 90% of its estimated work if it overruns.

Authenticated `POST /api/diagnostics/upload` consumes bounded bytes without
disk writes or transcription. Browser uploads also record their own transfer
and response-wait times plus Cloudflare Ray ID in the notes database (latest
40 records, no filename/content). Inspect `/api/diagnostics/upload-metrics`.
These are client-reported diagnostic data, not trusted throughput guarantees.

The tunnel currently uses HTTP/2 after a repeated 40 MiB test improved median
elapsed upload time from 29.080s (QUIC) to 13.695s (HTTP/2). Both measurements
were made from this Jetson, not from the user's browser. This is a local network
choice, not a claim that HTTP/2 is generally faster than QUIC.

Uploads select BF16 (default), RTN W8A16 or RTN W4A16. The chosen variant is
stored on each note; the serial worker switches `moss-note-model` only when
needed and waits for the actual loaded model path. A switch takes about one
minute. The fixed allowlist is `deploy/model-variants.json`; the atomic selection
file is `/data/artifacts/moss-note/20261006T062500Z-service/model-selection.json`.
Original model weights are unchanged. Quantized variants are local conversions,
not official quantized releases.

With `MOSS_MAX_FAN_DURING_TRANSCRIPTION=true`, the worker starts the root-owned
`moss-note-fan.service` before model preparation/transcription. Its command is
`/usr/bin/jetson_clocks --fan` (equivalent to running it with sudo), setting PWM
to 255. Completion, errors and cancellation stop the service and restart
`nvfancontrol` for automatic cooling; the fan is not forced to zero. The fan
unit is bound to the app, so app shutdown/crash also restores automatic control.
Install `deploy/moss-note-fan.service` in `/etc/systemd/system` and run
`sudo systemctl daemon-reload`; do not enable this on-demand unit at boot.

## Cloudflare account setup

1. Open Cloudflare Networking > Tunnels (or Zero Trust > Networks > Connectors).
2. Create a Cloudflared tunnel named `moss-note`.
3. Save only the tunnel token from the connector installation command to
   `/data/projects/jetson/moss_stt/.cloudflare-tunnel-token`. Do not commit it.
4. Configure a published application route:
   hostname `stt.seongwoonjo.com`, service type HTTP, URL `localhost:8000`.
5. In the `seongwoonjo.com` zone, ensure the following DNS record exists:

| Type | Name | Target | Proxy | TTL |
| --- | --- | --- | --- | --- |
| CNAME | stt | `cbbc4f0c-6081-4d18-8340-82314766071a.cfargotunnel.com` | Proxied | Auto |

The dashboard may create this record automatically when saving the route.
Use the UUID shown for this tunnel; do not use a public IP or another tunnel.
DNS alone does not configure the application route. Preserve all unrelated
DNS records.

Activate the connector after the token is saved:

```sh
chmod 600 /data/projects/jetson/moss_stt/.cloudflare-tunnel-token
sudo systemctl enable --now moss-note-tunnel
```

Alternatively run `python3 scripts/activate-tunnel.py` from the project.
It requests the token at a hidden prompt if no token file exists, saves it
with mode 600, starts the connector, and prints the exact CNAME target.

Visit `https://stt.seongwoonjo.com`. Authenticate with the local credentials.
As of 2026-10-08, the server accepts both `stt.seongwoonjo.com` and the old
`tts.seongwoonjo.com` during the DNS transition. The `stt` DNS record is still
pending user configuration; the local route has been validated.
Do not configure a Cloudflare cache rule to cache authenticated API responses.

## Validation on 2026-10-06

- Existing tests: 12 passed.
- All 20 pinned model files verified against upstream sizes and hashes.
- PyTorch CUDA matrix multiplication and vLLM startup succeeded on Orin.
- `/healthz` and `/readyz` returned HTTP 200; Qwen readiness was false as expected.
- Authenticated JFK audio upload, real MOSS transcription with timestamps and
  a speaker label, and SRT export passed. Results are in the data directory.
- Unauthenticated browser access now redirects to `/login`; unauthenticated
  API access returns HTTP 401. This avoids the in-app browser's Basic-auth error.
- PyTorch emits an Orin compute-capability support warning. The checks above
  passed, but long recordings and multiple speakers have not been tested.
- Public domain verified on 2026-10-07: HTTPS readiness, browser login page,
  form login, session-cookie authenticated API, and homepage returned HTTP 200.
  Tunnel ID: `cbbc4f0c-6081-4d18-8340-82314766071a`.
  Local hostname routing is configured in `deploy/cloudflared.yml`.

Installed runtime: vLLM `0.23.1rc1.dev949+g68b4a1d58`, PyTorch
`2.11.0+cu130`, Python 3.12, cloudflared `2026.10.0`.
Storage: model 1.8 GiB, inference environment 9.1 GiB, app environment 72 MiB.
Files inherit the `mlshared` group; credentials are mode 600.

Performance settings and RTN experiments are documented in `PERFORMANCE.md`.
Production uses BF16 with decode CUDA Graphs and an 8 GiB KV cache. Multipart
temporary files use `/data/caches/moss-note/tmp`. The QUIC UDP buffer maxima
are configured in `/etc/sysctl.d/90-cloudflared-buffers.conf`.

References:
- https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/
- https://developers.cloudflare.com/tunnel/concepts/routing/
- https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/
