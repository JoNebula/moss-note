# Jetson Thor Migration

This repository contains the current Orin application, not just the original
upstream release. Preserve the BF16/W8/W4 selector, 20-minute chunks with
2-minute overlap, progress/ETA, speaker merge/undo, and whole-transcript
Codex correction plus source-free summary (`gpt-6.1-sol`, medium, Fast).
No separate Qwen checkpoint is needed or should be downloaded.

## Approved Project-Local Thor Layout

The user approved a Thor-only storage-policy exception on 2026-10-08:
`/home/jetson/project/moss_stt` is on `/dev/nvme0n1p1`, with models under
`models/<model>/<exact-version>/`, immutable migration snapshots under
`migration/`, active notes under `data/<snapshot-version>/`, and caches under
`caches/`. No `/data` mount, partition, boot or fstab change is needed.
Orin retains its existing `/data` paths and default settings.

`MOSS_MODELS_DIR` explicitly changes the trusted model storage root; the
catalogue still allows only BF16/W8/W4 and rejects paths outside that root.
`MOSS_CACHE_DIR` redirects app/Codex temporary files and caches. Without
these settings, the original `/data/models` and `/data/caches/moss-note`
defaults remain unchanged.

After copying an offline snapshot, all three model checkpoints, and the
original `.env` privately as `.env.orin`, run on Thor:

```sh
.venv/bin/python scripts/restore-migration.py \
  --snapshot migration/20261008T132827Z-thor-migration \
  --source-env .env.orin --enable-fan
bash scripts/install-local-services.sh
```

The restore script verifies snapshot hashes, creates a new data directory,
rebases only stored audio/artifact paths in its copied SQLite database, and
generates `.env`, `deploy/model-variants.local.json` and `deploy/local/` units
for the actual user/group/project path. It refuses existing deployment/data
directories and leaves the original snapshot/Orin database intact. Secrets,
models, live data, caches and local generated units are excluded from Git and
Docker. Install the fan unit only after validating Thor's cooling commands.
The service installer grants only model restart and fan start/stop; it does
not install/start the tunnel or enable the fan at boot. Do not mix this local
layout with the original same-path migration commands below.

## What Goes Where

| Item | Transfer method |
| --- | --- |
| Source, tests, deployment templates | GitHub branch `codex/jetson-thor-migration` |
| MOSS BF16, RTN W8A16 and W4A16 checkpoints + manifests | Private SSH/rsync; about 4.3 GiB |
| Notes SQLite, uploads, normalized audio, correction artifacts | Offline snapshot + SSH/rsync; about 611 MiB currently |
| `.env`, `.deployment-credentials`, tunnel token | Separate private SSH transfer, mode 600; never Git |
| Python environments, CUDA/FlashInfer/compile caches | Rebuild on Thor; never copy from Orin |
| Codex login | Log in again as the Thor app service user |
| `timings.sqlite3` | Keep on Orin; Thor learns new hardware timings |

Do not expose meeting text, audio, Codex job logs, model weights, or login
credentials in this public repository or its GitHub Actions artifacts.
Keep the existing Orin deployment until Thor passes verification. Do not
open inbound school-network ports or change DNS just to migrate the host.

## 1. Inspect Thor Before Writing Data

```sh
ssh jetson@THOR_ADDRESS
findmnt --mountpoint /data
df -h /data
cat /etc/nv_tegra_release
uname -m
nvcc --version
nvidia-smi
```

Stop if `/data` is not mounted on the intended NVMe, has less than 10% free,
or the target already contains files at the paths below. Never fall back to
the home directory, overwrite existing model versions, format disks, or edit
boot/fstab configuration during this migration. Read `/data/STORAGE_POLICY.md`
if present. Preserve the `mlshared` group, setgid directories and default ACL.

Both systems use user `jetson` and the same absolute source/data/model paths.
If Thor uses another account/path, adapt all systemd units, `.env`, Codex path,
and stored audio paths before starting the app. Copying to an arbitrary new
data path will not fix the absolute audio paths recorded in existing notes.

## 2. Get The Exact Source

On Thor, in an unused directory on NVMe:

```sh
git clone --branch codex/jetson-thor-migration \
  https://github.com/JoNebula/moss-note.git /data/projects/jetson/moss_stt
cd /data/projects/jetson/moss_stt
git rev-parse HEAD
```

Record this commit and use the same revision on both systems. Do not run the
generic setup script blindly: it defaults to the Orin CUDA 13 wheel index and
can download a model if its path is not configured.

## 3. Build A Thor Runtime

The app requires Python 3.12, FFmpeg and `uv`. Keep caches on NVMe:

```sh
export UV_CACHE_DIR=/data/caches/uv
export PIP_CACHE_DIR=/data/caches/pip
export HF_HOME=/data/caches/huggingface
export TORCH_HOME=/data/caches/torch
uv sync --frozen --python 3.12 --group dev
uv venv --python 3.12 .venv-vllm
```

Install a Thor-compatible CUDA/PyTorch/vLLM audio runtime into `.venv-vllm`
after inspecting its JetPack/CUDA version. The validated **Orin baseline** is
vLLM `0.23.1rc1.dev949+g68b4a1d58`, PyTorch `2.11.0+cu130`, and
compressed-tensors `0.17.0`; this is a compatibility reference, not a claim
that its wheels or Marlin kernels work on Thor. Verify the selected build
supports MOSS, audio transcription and both quantized checkpoints. Rebuild
native kernels for Thor. Do not copy `.venv-vllm`, engine binaries, or JIT caches.
BF16 is the recovery option if a quantized kernel fails; never silently use
another precision for an existing queued note.

NVIDIA's [Thor CUDA setup](https://docs.nvidia.com/jetson/agx-thor-devkit/user-guide/latest/setup_cuda.html)
describes JetPack packages and warns not to replace the Jetson driver using
the SBSA driver-installer instructions. Do not upgrade/flash the target as
part of a routine app migration without explicit approval.

Use `deploy/thor.env.example` for bring-up if not copying the original `.env`.
Its auth values are blank; set both before external access. It starts with
eager execution and automatic fan control for **unverified hardware only**.
After validation, benchmark the Orin graph/attention settings from
`deploy/PERFORMANCE.md`, enable the fastest correct settings, and record the
Thor runtime/version/benchmarks. Check fan commands and `nvfancontrol` on Thor
before enabling `MOSS_MAX_FAN_DURING_TRANSCRIPTION=true`.

## 4. Copy Models Without Modifying Orin

On Orin, after confirming all destination version paths are unused:

```sh
rsync -a --info=progress2 --relative \
  /data/./models/MOSS-Transcribe-Diarize/20260902-704aa4a9 \
  /data/./models/MOSS-Transcribe-Diarize-W8A16-RTN/20261008-704aa4a9-g128 \
  /data/./models/MOSS-Transcribe-Diarize-W4A16-RTN/20261008-704aa4a9-g128 \
  jetson@THOR_ADDRESS:/data/
```

Do not use `--delete`. Preserve manifests and verify all checkpoint hashes
against their `manifest.json` on Thor, plus compare rsync checksums. The BF16
source revision is `704aa4a9c304e8520be88901e0d1960158ef5b15`.

## 5. Freeze And Snapshot Existing Notes

Wait for both transcription and correction queues to finish. Stop the tunnel
as well as the app so a connector restart cannot bring the app back mid-copy.
The model service can stay up. On Orin:

```sh
sudo systemctl stop moss-note-tunnel moss-note-app
.venv/bin/python scripts/export-migration.py \
  --data-dir /data/artifacts/moss-note/20261006T062500Z-service
sudo systemctl start moss-note-app moss-note-tunnel
```

Always restart Orin services even if export fails. The script refuses live
exports and queued jobs, uses SQLite's backup API, checks integrity, copies
audio/correction files, and records SHA256 hashes. It prints the snapshot
directory. It does not modify/delete live notes or copy credentials/models.
Transfer the snapshot over SSH, then restore its `data/` contents into the
**unused exact** `restore_data_dir` recorded in `manifest.json` on Thor.
Verify every manifest hash and `PRAGMA integrity_check` before service start.
This is a preliminary snapshot; repeat the offline snapshot at final cutover
if users have created or edited notes since export. Do not overwrite an older
target snapshot: keep versions and change the final data directory deliberately.

Transfer `.env`, `.deployment-credentials`, and `.cloudflare-tunnel-token`
privately only to the deployment account. Set mode 600, never print their
contents. Verify `.env` paths, caches, auth and runtime settings. Start with
fan control disabled until Thor hardware commands are tested. Keep the tunnel
token out of shared archives and Git. If using this Orin's dedicated key,
add `-i /home/jetson/.ssh/id_ed25519_moss_thor` to SSH/scp and use
`rsync -e 'ssh -i /home/jetson/.ssh/id_ed25519_moss_thor' ...`.

## 6. Services And Codex Login

Install `deploy/moss-note-app.service` and `deploy/moss-note-model.service`
under `/etc/systemd/system`, after checking user/group/path and executable
permissions. Managed model selection needs a narrowly scoped sudoers rule
for `jetson` to restart **only** `moss-note-model`; use `visudo -cf` to validate
it. Fan control, if verified/enabled, needs permission to start/stop only
`moss-note-fan` and the root-owned on-demand fan unit. Never grant general
passwordless `sudo`, never enable the fan unit at boot.

Install cloudflared and its unit but **do not start the Thor tunnel yet**.
The app and vLLM must still bind only to `127.0.0.1:8000/8001`.

Install Codex CLI, log in as `jetson` with `codex login --device-auth`, and set
`MOSS_CODEX_BINARY` to the actual executable. Check `codex login status`.
Keep credential stores out of transfers/archives; use a fresh login rather
than copying the Orin `auth.json`. See official OpenAI documentation:
[CLI install](https://learn.chatgpt.com/docs/cli),
[headless login](https://learn.chatgpt.com/docs/auth).

## 7. Verify Locally, Then Switch The Existing Tunnel

On Thor, run tests, start the model/app, and check:

```sh
.venv/bin/python -m pytest -q
sudo systemctl daemon-reload
sudo systemctl start moss-note-model moss-note-app
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz
```

Use authenticated API/browser checks for note count, original/edited/corrected
transcripts, audio playback, summaries/exports, speaker merge/undo, and model
selection. Run a synthetic ASR sample in BF16/W8/W4 and confirm fan restoration.
`PYTHONPATH=. .venv/bin/python scripts/smoke-codex.py --api` contacts OpenAI using
only synthetic text and removes only its test note after success. Do not
re-transcribe or re-correct real meetings just to test the migration.

At final cutover freeze Orin writes, take the final data snapshot, restore and
verify on Thor, **stop Orin's connector**, then start Thor's connector. Do not
run both against different SQLite copies: the tunnel can route to either host.
The existing Cloudflare tunnel UUID and working DNS can remain unchanged;
no inbound ports or router forwarding are required. Verify public login,
upload, ASR and correction before enabling Thor services at boot. Keep Orin
stopped but intact for rollback. If rolling back after Thor accepted writes,
preserve/reconcile Thor data first; do not discard new notes or start two writers.
