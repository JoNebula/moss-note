from __future__ import annotations

import asyncio
import base64
import binascii
import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import time
import uuid
from contextlib import asynccontextmanager, suppress
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from app.auth import issue_token, valid_token
from app.codex_postprocess import (
    CodexPostprocessor, EFFORT, MODEL as CODEX_MODEL, SERVICE_TIER, new_run_directory, summary_markdown,
)
from app.fan_control import FanControl
from app.model_runtime import ModelRuntime
from app.progress import TimingCache, progress_state

from app.speakers import merge_speakers, undo_speaker_merge
from app.store import NoteStore
from app.transcript import json_text, parse_transcript, plain_text, srt_text, vtt_text


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("MOSS_DATA_DIR", ROOT / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
STATIC_DIR = ROOT / "static"
VLLM_URL = os.getenv("MOSS_VLLM_URL", "http://127.0.0.1:8001/v1").rstrip("/")
MODEL_NAME = os.getenv("MOSS_MODEL_NAME", "moss-mtd")
QWEN_VLLM_URL = os.getenv("QWEN_VLLM_URL", "http://127.0.0.1:8002/v1").rstrip("/")
QWEN_REQUIRED = os.getenv("MOSS_REQUIRE_QWEN", "false").lower() not in {
    "0",
    "false",
    "no",
}
AUTH_USERNAME = os.getenv("MOSS_AUTH_USERNAME", "")
AUTH_PASSWORD = os.getenv("MOSS_AUTH_PASSWORD", "")
MAX_UPLOAD_BYTES = int(float(os.getenv("MOSS_MAX_UPLOAD_GB", "5")) * 1024**3)
MAX_COMPLETION_TOKENS = int(os.getenv("MOSS_MAX_COMPLETION_TOKENS", "65536"))
# The official model limit is 90 minutes. The env var may lower the chunk size
# for constrained deployments or testing, but never raises it beyond 90 min.
MAX_AUDIO_CHUNK_SECONDS = max(
    1, min(int(os.getenv("MOSS_CHUNK_SECONDS", "5400")), 5400)
)
CHUNK_OVERLAP_SECONDS = max(
    0, min(int(os.getenv("MOSS_CHUNK_OVERLAP_SECONDS", "0")), MAX_AUDIO_CHUNK_SECONDS - 1)
)
BRIDGE_AUDIO_SECONDS = max(
    2, min(int(os.getenv("MOSS_BRIDGE_SECONDS", "300")), MAX_AUDIO_CHUNK_SECONDS)
)
STORE = NoteStore(DATA_DIR / "moss-note.sqlite3")
QUEUE: asyncio.Queue[str] = asyncio.Queue()
CORRECTION_QUEUE: asyncio.Queue[str] = asyncio.Queue()
MODEL_RUNTIME = ModelRuntime.from_env()
FAN_CONTROL = FanControl.from_env()
TIMINGS = TimingCache(DATA_DIR / "timings.sqlite3")
CODEX = CodexPostprocessor.from_env()

DEFAULT_PROMPT = (
    "请将音频转写为文本，每一段需以起始时间戳和说话人编号（[S01]、[S02]、[S03]…）开头，"
    "正文为对应的语音内容，并在段末标注结束时间戳，以清晰标明该段语音范围。"
)


class NotePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    segments: list[dict[str, Any]] | None = None
    corrected_segments: list[dict[str, Any]] | None = None
    speaker_names: dict[str, str] | None = None


class SpeakerMerge(BaseModel):
    sources: list[str] = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=64)


class UploadMetric(BaseModel):
    note_id: str = Field(min_length=1, max_length=64)
    bytes: int = Field(gt=0, le=MAX_UPLOAD_BYTES)
    transfer_seconds: float = Field(ge=0, le=86400, allow_inf_nan=False)
    response_wait_seconds: float = Field(ge=0, le=86400, allow_inf_nan=False)
    cf_ray: str | None = Field(default=None, max_length=64)


def public_note(note: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: value
        for key, value in note.items()
        if key
        not in {
            "source_path",
            "normalized_path",
            "raw_transcript",
            "correction_partial",
            "correction_artifacts_path",
            "speaker_merge_history",
            "processing",
        }
    }
    result["progress"] = progress_state(note, TIMINGS)
    result["speaker_merge_undo_count"] = len(note["speaker_merge_history"])
    return result


def run_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, check=True, capture_output=True, text=True)


def probe_duration(path: Path) -> float | None:
    try:
        result = run_command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ]
        )
        return round(float(result.stdout.strip()), 3)
    except (subprocess.CalledProcessError, ValueError):
        return None


def normalize_audio(source: Path, destination: Path) -> None:
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )


def split_audio(
    normalized: Path, duration: float | None
) -> list[tuple[Path, float]]:
    """Return bounded, optionally overlapping WAV chunks on the original timeline."""
    if duration is None or duration <= MAX_AUDIO_CHUNK_SECONDS:
        return [(normalized, 0.0)]

    chunk_dir = normalized.parent / "chunks"
    shutil.rmtree(chunk_dir, ignore_errors=True)
    chunk_dir.mkdir()
    chunks: list[tuple[Path, float]] = []
    offset = 0.0
    index = 0
    while offset < duration - 0.01:
        chunk = chunk_dir / f"chunk-{index:03d}.wav"
        chunk_duration = min(MAX_AUDIO_CHUNK_SECONDS, duration - offset)
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(offset),
                "-t",
                str(chunk_duration),
                "-i",
                str(normalized),
                "-vn",
                "-c:a",
                "pcm_s16le",
                str(chunk),
            ]
        )
        chunks.append((chunk, offset))
        if offset + chunk_duration >= duration - 0.01:
            break
        offset += MAX_AUDIO_CHUNK_SECONDS - min(CHUNK_OVERLAP_SECONDS, MAX_AUDIO_CHUNK_SECONDS - 1)
        index += 1
    return chunks


def make_bridge_chunks(
    normalized: Path,
    duration: float | None,
    chunks: list[tuple[Path, float]],
) -> list[tuple[Path, float]]:
    """Create short clips spanning both sides of every main-chunk boundary."""
    if duration is None or len(chunks) <= 1:
        return []
    chunk_dir = normalized.parent / "chunks"
    half = BRIDGE_AUDIO_SECONDS / 2
    bridges: list[tuple[Path, float]] = []
    for index, (_, boundary) in enumerate(chunks[1:]):
        start = max(0.0, boundary - half)
        bridge_duration = min(BRIDGE_AUDIO_SECONDS, duration - start)
        bridge = chunk_dir / f"bridge-{index:03d}.wav"
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(start),
                "-t",
                str(bridge_duration),
                "-i",
                str(normalized),
                "-vn",
                "-c:a",
                "pcm_s16le",
                str(bridge),
            ]
        )
        bridges.append((bridge, start))
    return bridges


def transcription_form(note: dict[str, Any]) -> dict[str, str]:
    prompt = DEFAULT_PROMPT
    if note.get("hotwords"):
        prompt += f"热词提示：{note['hotwords']}"
    form = {
        "model": MODEL_NAME,
        "response_format": "json",
        "temperature": "0",
        "prompt": prompt,
        "max_completion_tokens": str(MAX_COMPLETION_TOKENS),
    }
    if note.get("hotwords"):
        form["hotwords"] = note["hotwords"]
    if note.get("language"):
        form["language"] = note["language"]
    return form


async def transcribe_chunk(
    client: httpx.AsyncClient, audio_path: Path, note: dict[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    with audio_path.open("rb") as audio:
        response = await client.post(
            f"{VLLM_URL}/audio/transcriptions",
            data=transcription_form(note),
            files={"file": ("audio.wav", audio, "audio/wav")},
        )
    response.raise_for_status()
    payload = response.json()
    raw = payload.get("text", "")
    segments = parse_transcript(raw)
    if not segments and payload.get("segments"):
        segments = [
            {
                "id": index,
                "start": float(segment.get("start", 0)),
                "end": float(segment.get("end", 0)),
                "speaker": segment.get("speaker", f"S{index + 1:02d}"),
                "text": segment.get("text", "").strip(),
            }
            for index, segment in enumerate(payload["segments"])
        ]
    return raw, segments


def match_speakers_by_overlap(
    source_offset: float,
    source_segments: list[dict[str, Any]],
    bridge_offset: float,
    bridge_segments: list[dict[str, Any]],
    window: tuple[float, float] | None = None,
) -> dict[str, str]:
    """Greedily find a one-to-one speaker mapping on duplicated audio."""
    scores: dict[tuple[str, str], float] = {}
    for source in source_segments:
        source_start = float(source["start"]) + source_offset
        source_end = float(source["end"]) + source_offset
        for bridge in bridge_segments:
            bridge_start = float(bridge["start"]) + bridge_offset
            bridge_end = float(bridge["end"]) + bridge_offset
            overlap_start = max(source_start, bridge_start)
            overlap_end = min(source_end, bridge_end)
            if window:
                overlap_start = max(overlap_start, window[0])
                overlap_end = min(overlap_end, window[1])
            overlap = overlap_end - overlap_start
            if overlap > 0:
                key = (source["speaker"], bridge["speaker"])
                scores[key] = scores.get(key, 0.0) + overlap

    mapping: dict[str, str] = {}
    used_bridge_speakers: set[str] = set()
    for (source_speaker, bridge_speaker), score in sorted(
        scores.items(), key=lambda item: item[1], reverse=True
    ):
        if score < 0.25:
            continue
        if source_speaker in mapping or bridge_speaker in used_bridge_speakers:
            continue
        mapping[source_speaker] = bridge_speaker
        used_bridge_speakers.add(bridge_speaker)
    return mapping


def overlap_handoff(
    left_offset: float, left: list[dict[str, Any]],
    right_offset: float, right: list[dict[str, Any]],
    window: tuple[float, float], mapping: dict[str, str],
) -> tuple[float, float]:
    """Prefer a shared utterance near the midpoint; retain it from the left only."""
    midpoint = sum(window) / 2
    candidates = []
    for a in left:
        a_start, a_end = float(a["start"]) + left_offset, float(a["end"]) + left_offset
        if a_end <= window[0] or a_end > window[1]:
            continue
        a_text = "".join(char for char in a["text"].casefold() if char.isalnum())
        if not a_text:
            continue
        for b in right:
            b_start, b_end = float(b["start"]) + right_offset, float(b["end"]) + right_offset
            if mapping.get(a["speaker"]) != b["speaker"] or b_end > window[1]:
                continue
            if min(a_end, b_end) - max(a_start, b_start, window[0]) <= 0:
                continue
            b_text = "".join(char for char in b["text"].casefold() if char.isalnum())
            similarity = SequenceMatcher(None, a_text, b_text, autojunk=False).ratio()
            if similarity >= 0.8:
                distance = (abs(a_end - midpoint) + abs(b_end - midpoint)) / 2
                candidates.append((distance + (1 - similarity) * (window[1] - window[0]), a_end, b_end))
    if candidates:
        _, left_end, right_end = min(candidates)
        return left_end, right_end
    return midpoint, midpoint


def merge_chunk_segments(
    results: list[tuple[float, list[dict[str, Any]]]],
    bridges: list[tuple[float, list[dict[str, Any]]]] | None = None,
    *, overlap_seconds: float = 0, chunk_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Connect shared-audio speakers and retain one transcript at each handoff."""
    parent: dict[tuple[int, str], tuple[int, str]] = {}

    def find(node: tuple[int, str]) -> tuple[int, str]:
        parent.setdefault(node, node)
        if parent[node] != node:
            parent[node] = find(parent[node])
        return parent[node]

    def union(left: tuple[int, str], right: tuple[int, str]) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for chunk_index, (_, segments) in enumerate(results):
        for segment in segments:
            find((chunk_index, segment["speaker"]))

    bounds = [[float("-inf"), float("inf")] for _ in results]
    if overlap_seconds:
        size = chunk_seconds or MAX_AUDIO_CHUNK_SECONDS
        for index in range(len(results) - 1):
            left_offset, left = results[index]
            right_offset, right = results[index + 1]
            window = (right_offset, min(left_offset + size, right_offset + size))
            if window[1] <= window[0]:
                continue
            mapping = match_speakers_by_overlap(left_offset, left, right_offset, right, window)
            for left_speaker, right_speaker in mapping.items():
                union((index, left_speaker), (index + 1, right_speaker))
            bounds[index][1], bounds[index + 1][0] = overlap_handoff(
                left_offset, left, right_offset, right, window, mapping
            )

    for boundary_index, (bridge_offset, bridge_segments) in enumerate(bridges or []):
        if boundary_index + 1 >= len(results):
            break
        left_offset, left_segments = results[boundary_index]
        right_offset, right_segments = results[boundary_index + 1]
        left_map = match_speakers_by_overlap(
            left_offset, left_segments, bridge_offset, bridge_segments
        )
        right_map = match_speakers_by_overlap(
            right_offset, right_segments, bridge_offset, bridge_segments
        )
        right_by_bridge = {
            bridge_speaker: source_speaker
            for source_speaker, bridge_speaker in right_map.items()
        }
        for left_speaker, bridge_speaker in left_map.items():
            if right_speaker := right_by_bridge.get(bridge_speaker):
                union(
                    (boundary_index, left_speaker),
                    (boundary_index + 1, right_speaker),
                )

    merged: list[dict[str, Any]] = []
    global_speakers: dict[tuple[int, str], str] = {}
    for chunk_index, (offset, segments) in enumerate(results):
        for segment in segments:
            midpoint = (float(segment["start"]) + float(segment["end"])) / 2 + offset
            if not bounds[chunk_index][0] <= midpoint < bounds[chunk_index][1]:
                continue
            root = find((chunk_index, segment["speaker"]))
            if root not in global_speakers:
                global_speakers[root] = f"S{len(global_speakers) + 1:02d}"
            merged.append(
                {
                    "id": len(merged),
                    "start": round(float(segment["start"]) + offset, 3),
                    "end": round(float(segment["end"]) + offset, 3),
                    "speaker": global_speakers[root],
                    "text": segment["text"],
                    "chunk": chunk_index + 1,
                }
            )
    merged.sort(key=lambda segment: (segment["start"], segment["end"]))
    for index, segment in enumerate(merged):
        segment["id"] = index
    return merged


def canonical_transcript(segments: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[{segment['start']:.2f}][{segment['speaker']}]"
        f"{segment['text']}[{segment['end']:.2f}]"
        for segment in segments
    )


async def transcribe(note_id: str) -> None:
    note = STORE.update(
        note_id, status="processing", error=None, processed_chunks=0,
        processing={"phase": "preparing", "phase_started_at": time.time()}
    )
    source = Path(note["source_path"])
    normalized = source.parent / "normalized.wav"
    chunk_dir = normalized.parent / "chunks"
    try:
        await FAN_CONTROL.start()
        await MODEL_RUNTIME.ensure(note["model_variant"])
        STORE.update(note_id, chunk_seconds=MAX_AUDIO_CHUNK_SECONDS,
                     chunk_overlap_seconds=CHUNK_OVERLAP_SECONDS,
                     processing={"phase": "normalizing", "phase_started_at": time.time()})
        await asyncio.to_thread(normalize_audio, source, normalized)
        duration = await asyncio.to_thread(probe_duration, normalized)
        chunks = await asyncio.to_thread(split_audio, normalized, duration)
        STORE.update(
            note_id,
            normalized_path=str(normalized),
            duration=duration,
            chunk_count=len(chunks),
        )
        chunk_durations = [min(MAX_AUDIO_CHUNK_SECONDS, duration - offset)
                           if duration is not None else (await asyncio.to_thread(probe_duration, path) or MAX_AUDIO_CHUNK_SECONDS)
                           for path, offset in chunks]

        timeout = httpx.Timeout(connect=30, read=6 * 60 * 60, write=60 * 60, pool=30)
        chunk_results: list[tuple[float, list[dict[str, Any]]]] = []
        raw_results: list[str] = []
        async with httpx.AsyncClient(timeout=timeout) as client:
            for index, (chunk_path, offset) in enumerate(chunks, start=1):
                STORE.update(note_id, processing={"phase": "transcribing", "phase_started_at": time.time(),
                                                  "chunk_durations": chunk_durations})
                started = time.monotonic()
                raw, segments = await transcribe_chunk(client, chunk_path, note)
                TIMINGS.record(note["model_variant"], chunk_durations[index - 1], time.monotonic() - started)
                raw_results.append(raw)
                chunk_results.append((offset, segments))
                STORE.update(note_id, processed_chunks=index)

        STORE.update(note_id, processing={"phase": "merging", "phase_started_at": time.time(),
                                          "chunk_durations": chunk_durations})
        segments = merge_chunk_segments(chunk_results, overlap_seconds=CHUNK_OVERLAP_SECONDS,
                                        chunk_seconds=MAX_AUDIO_CHUNK_SECONDS)
        if not segments:
            preview = "\n".join(raw_results)[:500]
            raise RuntimeError(f"모델 결과를 구간으로 해석하지 못했습니다: {preview}")
        speakers = sorted({segment["speaker"] for segment in segments})
        STORE.update(
            note_id,
            status="done",
            raw_transcript=canonical_transcript(segments),
            raw_segments=segments,
            segments=segments,
            correction_source_segments=[],
            corrected_segments=[],
            correction_partial={},
            correction_status="idle",
            correction_error=None,
            correction_total_windows=0,
            correction_processed_windows=0,
            correction_changes=0,
            correction_model=None,
            correction_service_tier=None,
            correction_backend=None,
            correction_phase="idle",
            correction_usage={},
            summary={},
            correction_artifacts_path=None,
            speaker_merge_history=[],
            summary_stale=False,
            correction_mode="whole-file",
            correction_output_mode="changes",
            speaker_names={speaker: speaker for speaker in speakers},
        )
    except Exception as error:
        detail = str(error)
        if isinstance(error, httpx.HTTPStatusError):
            detail = f"vLLM {error.response.status_code}: {error.response.text[:1000]}"
        STORE.update(note_id, status="error", error=detail)
    finally:
        await asyncio.shield(FAN_CONTROL.stop())
        shutil.rmtree(chunk_dir, ignore_errors=True)


async def queue_worker() -> None:
    while True:
        note_id = await QUEUE.get()
        try:
            await transcribe(note_id)
        finally:
            QUEUE.task_done()


def correction_document(note: dict, segments: list[dict]) -> dict:
    return {"title": note["title"], "known_terms": note.get("hotwords") or "",
            "speaker_names": note["speaker_names"], "target_ids": [item["id"] for item in segments],
            "segments": segments}


async def postprocess_transcript(note_id: str) -> None:
    note = STORE.get(note_id)
    source_segments = [{**segment} for segment in (note["correction_source_segments"] or note["segments"])]
    STORE.update(
        note_id,
        correction_status="processing",
        correction_error=None,
        correction_source_segments=source_segments,
        correction_total_windows=1,
        correction_processed_windows=0,
        correction_partial={},
        correction_usage={},
        correction_model=CODEX_MODEL,
        correction_service_tier=SERVICE_TIER,
        correction_backend="codex-cli",
        correction_mode="whole-file",
        correction_output_mode=CODEX.output_mode,
        correction_phase="whole-file",
    )
    try:
        directory = new_run_directory(DATA_DIR / "codex-runs", note_id)
        STORE.update(note_id, correction_artifacts_path=str(directory))
        document = correction_document(note, source_segments)
        corrected_segments, summary, usage = await CODEX.process(document, directory / "whole-file")
        changes = sum(
            before.get("text", "") != after.get("text", "")
            for before, after in zip(source_segments, corrected_segments, strict=True)
        )
        (directory / "corrected.txt").write_text(plain_text(corrected_segments, note["speaker_names"]))
        (directory / "corrected.json").write_text(json.dumps(corrected_segments, ensure_ascii=False, indent=2))
        (directory / "summary.md").write_text(summary_markdown(summary))
        STORE.update(note_id, summary=summary, correction_partial={}, correction_status="done",
                     correction_phase="done", correction_error=None, correction_usage=usage,
                     corrected_segments=corrected_segments, correction_changes=changes,
                     correction_processed_windows=1, summary_stale=False)
    except Exception as error:
        detail = "Codex 응답 시간이 초과되었습니다. 다시 시도하면 전체 파일을 재처리합니다." if isinstance(error, TimeoutError) else str(error)
        STORE.update(note_id, correction_status="error", correction_error=detail)


async def correction_queue_worker() -> None:
    while True:
        note_id = await CORRECTION_QUEUE.get()
        try:
            await postprocess_transcript(note_id)
        finally:
            CORRECTION_QUEUE.task_done()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if bool(AUTH_USERNAME) != bool(AUTH_PASSWORD):
        raise RuntimeError(
            "MOSS_AUTH_USERNAME과 MOSS_AUTH_PASSWORD는 함께 설정해야 합니다."
        )
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for note_id in STORE.pending_ids():
        QUEUE.put_nowait(note_id)
    for note_id in STORE.pending_correction_ids():
        CORRECTION_QUEUE.put_nowait(note_id)
    worker = asyncio.create_task(queue_worker())
    correction_worker = asyncio.create_task(correction_queue_worker())
    yield
    worker.cancel()
    correction_worker.cancel()
    with suppress(asyncio.CancelledError):
        await worker
    with suppress(asyncio.CancelledError):
        await correction_worker


app = FastAPI(
    title="MOSS Note",
    version="0.2.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


def valid_basic_auth(header: str | None) -> bool:
    if not AUTH_USERNAME:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return secrets.compare_digest(username, AUTH_USERNAME) and secrets.compare_digest(
        password, AUTH_PASSWORD
    )


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    basic_ok = valid_basic_auth(request.headers.get("Authorization"))
    cookie_ok = bool(AUTH_USERNAME) and valid_token(
        request.cookies.get("moss_session", ""), AUTH_USERNAME, AUTH_PASSWORD, "session"
    )
    if request.url.path not in {"/healthz", "/readyz", "/login", "/favicon.ico"} and not (basic_ok or cookie_ok):
        if request.url.path.startswith("/api/"):
            response = Response(
                "Authentication required", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="MOSS Note", charset="UTF-8"'},
            )
        else:
            response = RedirectResponse("/login", status_code=303)
    elif cookie_ok and not basic_ok and request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path != "/login" and request.headers.get("origin") != str(request.base_url).rstrip("/"):
        response = Response("Invalid request origin", status_code=403)
    else:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
    response.headers["Cache-Control"] = "no-store"
    return response


def login_page(request: Request, error: str = "", status_code: int = 200) -> HTMLResponse:
    csrf = request.cookies.get("moss_login_csrf", "")
    if not valid_token(csrf, AUTH_USERNAME, AUTH_PASSWORD, "login"):
        csrf = issue_token(AUTH_USERNAME, AUTH_PASSWORD, "login", 600)
    html = (STATIC_DIR / "login.html").read_text().replace("__CSRF__", csrf).replace("__ERROR__", error)
    response = HTMLResponse(html, status_code=status_code)
    response.set_cookie("moss_login_csrf", csrf, max_age=600, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict")
    return response


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/login", include_in_schema=False)
def get_login(request: Request):
    if not AUTH_USERNAME:
        return RedirectResponse("/", status_code=303)
    return login_page(request)


@app.post("/login", include_in_schema=False)
def post_login(request: Request, username: str = Form(...), password: str = Form(...), csrf: str = Form(...)):
    if not AUTH_USERNAME:
        return RedirectResponse("/", status_code=303)
    if not secrets.compare_digest(csrf.encode(), request.cookies.get("moss_login_csrf", "").encode()) or not valid_token(csrf, AUTH_USERNAME, AUTH_PASSWORD, "login"):
        return login_page(request, "다시 로그인해 주세요.", 403)
    if not (secrets.compare_digest(username.encode(), AUTH_USERNAME.encode()) and secrets.compare_digest(password.encode(), AUTH_PASSWORD.encode())):
        return login_page(request, "아이디 또는 비밀번호를 확인해 주세요.", 401)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("moss_session", issue_token(AUTH_USERNAME, AUTH_PASSWORD, "session", 43200),
                        max_age=43200, httponly=True, secure=request.url.scheme == "https", samesite="strict")
    response.delete_cookie("moss_login_csrf")
    return response


@app.post("/logout", include_in_schema=False)
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("moss_session")
    return response


async def model_health() -> dict[str, Any]:
    services: dict[str, dict[str, Any]] = {
        "vllm": {"ok": False, "url": VLLM_URL},
        "qwen": {"ok": False, "url": QWEN_VLLM_URL},
    }
    async with httpx.AsyncClient(timeout=3) as client:
        responses = await asyncio.gather(
            client.get(f"{VLLM_URL}/models"),
            client.get(f"{QWEN_VLLM_URL}/models"),
            return_exceptions=True,
        )
    for name, result in zip(("vllm", "qwen"), responses, strict=True):
        service = services[name]
        if isinstance(result, httpx.Response):
            service["ok"] = result.is_success
            if result.is_success:
                if name == "vllm":
                    models = result.json().get("data", [])
                    service["variant"] = MODEL_RUNTIME.variant_for_path(models[0].get("root")) if models else None
                service["models"] = [
                    item["id"] for item in result.json().get("data", [])
                ]
            else:
                service["error"] = f"HTTP {result.status_code}"
        else:
            service["error"] = str(result)
    return services


@app.get("/healthz", include_in_schema=False)
def liveness() -> dict[str, bool]:
    return {"ok": True}


@app.get("/readyz", include_in_schema=False)
async def readiness() -> JSONResponse:
    services = await model_health()
    ready = services["vllm"]["ok"] and (
        services["qwen"]["ok"] or not QWEN_REQUIRED
    )
    return JSONResponse(
        {"ready": ready, "moss": services["vllm"]["ok"], "qwen": services["qwen"]["ok"]},
        status_code=200 if ready else 503,
    )


@app.get("/api/health")
async def health() -> dict[str, Any]:
    services = await model_health()
    return {
        "ok": True,
        **services,
        "queue_size": QUEUE.qsize(),
        "correction_queue_size": CORRECTION_QUEUE.qsize(),
        "codex": {"available": CODEX.available(), "model": CODEX_MODEL, "reasoning_effort": EFFORT,
                  "service_tier": SERVICE_TIER, "speed": "fast"},
        "model_runtime": MODEL_RUNTIME.public_state(services["vllm"].get("variant"), services["vllm"]["ok"]),
        "limits": {
            "chunk_seconds": MAX_AUDIO_CHUNK_SECONDS,
            "overlap_seconds": CHUNK_OVERLAP_SECONDS,
            "max_upload_bytes": MAX_UPLOAD_BYTES,
        },
    }


@app.post("/api/diagnostics/upload")
async def upload_diagnostic(request: Request) -> dict[str, Any]:
    """Authenticated network probe: consume bounded bytes without disk writes or ASR."""
    started = time.monotonic()
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "업로드 제한을 초과했습니다.")
    return {"bytes": size, "receive_seconds": round(time.monotonic() - started, 4),
            "storage_written": False}


@app.post("/api/diagnostics/upload-metrics", status_code=204)
def record_upload_metric(metric: UploadMetric) -> Response:
    try:
        STORE.get(metric.note_id)
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
    STORE.record_upload(metric.model_dump())
    return Response(status_code=204)


@app.get("/api/diagnostics/upload-metrics")
def upload_metrics() -> list[dict[str, Any]]:
    return STORE.upload_metrics()


@app.post("/api/notes", status_code=202)
async def create_note(
    file: UploadFile = File(...),
    title: str = Form(""),
    language: str = Form("ko"),
    hotwords: str = Form(""),
    model_variant: str = Form("bf16"),
) -> dict[str, Any]:
    try:
        MODEL_RUNTIME.validate(model_variant)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if not file.filename:
        raise HTTPException(400, "파일 이름이 없습니다.")
    note_id = uuid.uuid4().hex
    note_dir = UPLOAD_DIR / note_id
    note_dir.mkdir(parents=True)
    extension = Path(file.filename).suffix.lower()[:12]
    source = note_dir / f"source{extension}"
    size = 0
    try:
        with source.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "업로드 제한을 초과했습니다.")
                destination.write(chunk)
    except Exception:
        shutil.rmtree(note_dir, ignore_errors=True)
        raise
    finally:
        await file.close()

    clean_title = title.strip() or Path(file.filename).stem
    note = STORE.create(
        {
            "id": note_id,
            "title": clean_title[:200],
            "original_filename": Path(file.filename).name,
            "media_type": (
                mimetypes.guess_type(file.filename)[0]
                if file.content_type in {None, "application/octet-stream"}
                else file.content_type
            ),
            "source_path": str(source),
            "status": "queued",
            "language": language.strip() or None,
            "hotwords": hotwords.strip() or None,
            "model_variant": model_variant,
        }
    )
    QUEUE.put_nowait(note_id)
    return public_note(note)


@app.get("/api/notes")
def list_notes() -> list[dict[str, Any]]:
    return [public_note(note) for note in STORE.list()]


@app.get("/api/notes/{note_id}")
def get_note(note_id: str) -> dict[str, Any]:
    try:
        return public_note(STORE.get(note_id))
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None


@app.patch("/api/notes/{note_id}")
def patch_note(note_id: str, patch: NotePatch) -> dict[str, Any]:
    with STORE.lock:
        return patch_note_locked(note_id, patch)


def patch_note_locked(note_id: str, patch: NotePatch) -> dict[str, Any]:
    changes = patch.model_dump(exclude_none=True)
    if "title" in changes:
        changes["title"] = changes["title"].strip()
    try:
        note = STORE.get(note_id)
        if patch.corrected_segments is not None and note["correction_status"] in {"queued", "processing"}:
            raise HTTPException(409, "AI 작업 중에는 교정본을 편집할 수 없습니다.")
        if patch.speaker_names is not None and note["correction_status"] in {"queued", "processing"}:
            raise HTTPException(409, "AI 작업 중에는 화자 이름을 편집할 수 없습니다.")
        if note["summary"] and any(field in changes and changes[field] != note[field]
                                   for field in ("speaker_names", "corrected_segments")):
            changes["summary_stale"] = True
        for field, base_field in (
            ("segments", "segments"),
            ("corrected_segments", "corrected_segments"),
        ):
            if field not in changes:
                continue
            base = note[base_field]
            submitted = changes[field]
            if len(base) != len(submitted):
                raise HTTPException(400, "발화 구간의 개수는 변경할 수 없습니다.")
            submitted_by_id = {item.get("id"): item for item in submitted}
            if set(submitted_by_id) != {item.get("id") for item in base}:
                raise HTTPException(400, "발화 ID는 변경할 수 없습니다.")
            changes[field] = [
                {
                    **item,
                    "text": str(submitted_by_id[item["id"]].get("text", "")).strip(),
                }
                for item in base
            ]
        return public_note(STORE.update(note_id, **changes))
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None


@app.delete("/api/notes/{note_id}", status_code=204)
def delete_note(note_id: str) -> Response:
    try:
        note = STORE.get(note_id)
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
    if note["status"] in {"queued", "processing"} or note["correction_status"] in {
        "queued",
        "processing",
    }:
        raise HTTPException(409, "전사 중인 노트는 완료 후 삭제할 수 있습니다.")
    STORE.delete(note_id)
    shutil.rmtree(Path(note["source_path"]).parent, ignore_errors=True)
    return Response(status_code=204)


@app.get("/api/notes/{note_id}/media")
def note_media(note_id: str) -> FileResponse:
    try:
        note = STORE.get(note_id)
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
    path = Path(note["source_path"])
    return FileResponse(path, media_type=note.get("media_type"), filename=note["original_filename"])


@app.post("/api/notes/{note_id}/postprocess", status_code=202)
async def start_postprocess(note_id: str) -> dict[str, Any]:
    with STORE.lock:
        return enqueue_postprocess(note_id)


def enqueue_postprocess(note_id: str) -> dict[str, Any]:
    try:
        note = STORE.get(note_id)
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
    if note["status"] != "done" or not note["segments"]:
        raise HTTPException(409, "전사가 완료된 노트만 교정할 수 있습니다.")
    if note["correction_status"] in {"queued", "processing"}:
        raise HTTPException(409, "이미 AI 교정 작업이 진행 중입니다.")
    if not CODEX.available():
        raise HTTPException(503, "Codex CLI가 설치되어 있지 않습니다.")
    source = note["segments"]
    if len(json.dumps(correction_document(note, source), ensure_ascii=False).encode()) > 1_000_000:
        raise HTTPException(413, "교정할 전사 텍스트가 너무 큽니다 (최대 1 MB).")
    note = STORE.update(
        note_id,
        correction_status="queued",
        correction_error=None,
        correction_source_segments=source,
        correction_partial={},
        correction_total_windows=1,
        correction_processed_windows=0,
        correction_usage={},
        correction_backend="codex-cli",
        correction_model=CODEX_MODEL,
        correction_service_tier=SERVICE_TIER,
        correction_mode="whole-file",
        correction_output_mode=CODEX.output_mode,
        correction_phase="queued",
    )
    CORRECTION_QUEUE.put_nowait(note_id)
    return public_note(note)


def require_speaker_editable(note: dict) -> None:
    if note["status"] != "done" or note["correction_status"] in {"queued", "processing"}:
        raise HTTPException(409, "전사와 AI 작업이 완료된 뒤 화자를 병합하거나 취소할 수 있습니다.")


@app.post("/api/notes/{note_id}/speakers/merge")
def merge_note_speakers(note_id: str, selection: SpeakerMerge) -> dict:
    try:
        with STORE.lock:
            note = STORE.get(note_id)
            require_speaker_editable(note)
            changes = merge_speakers(note, selection.sources, selection.target)
            return public_note(STORE.update(note_id, **changes))
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@app.post("/api/notes/{note_id}/speakers/undo")
def undo_note_speakers(note_id: str) -> dict:
    try:
        with STORE.lock:
            note = STORE.get(note_id)
            require_speaker_editable(note)
            return public_note(STORE.update(note_id, **undo_speaker_merge(note)))
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from error


@app.get("/api/notes/{note_id}/summary/{format_name}")
def export_summary(note_id: str, format_name: str) -> Response:
    try:
        note = STORE.get(note_id)
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
    if format_name not in {"md", "txt"}:
        raise HTTPException(404, "지원하지 않는 요약 형식입니다.")
    if not note["summary"] or note["correction_status"] != "done":
        raise HTTPException(409, "완료된 요약본이 없습니다.")
    content = summary_markdown(note["summary"])
    if note["summary_stale"]:
        content = "> 화자 또는 교정본 변경 이후 요약 갱신이 필요합니다.\n\n" + content
    filename = quote(f"{note['title']}-summary.{format_name}")
    return Response(content, media_type="text/plain; charset=utf-8", headers={
        "Content-Disposition": f"attachment; filename=summary.{format_name}; filename*=UTF-8''{filename}",
    })


@app.get("/api/notes/{note_id}/export/{format_name}")
def export_note(
    note_id: str,
    format_name: str,
    version: str = Query(default="edited", pattern="^(edited|corrected|raw)$"),
) -> Response:
    try:
        note = STORE.get(note_id)
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
    selected_segments = note["segments"]
    if version == "corrected":
        if not note["corrected_segments"]:
            raise HTTPException(409, "완료된 AI 교정본이 없습니다.")
        selected_segments = note["corrected_segments"]
    elif version == "raw":
        selected_segments = note["raw_segments"] or note["segments"]
    export_note_data = {**public_note(note), "segments": selected_segments}
    exporters = {
        "txt": (plain_text, "text/plain; charset=utf-8"),
        "srt": (srt_text, "application/x-subrip; charset=utf-8"),
        "vtt": (vtt_text, "text/vtt; charset=utf-8"),
    }
    if format_name == "json":
        content, media_type = json_text(export_note_data), "application/json; charset=utf-8"
    elif format_name in exporters:
        exporter, media_type = exporters[format_name]
        content = exporter(selected_segments, note["speaker_names"])
    else:
        raise HTTPException(400, "지원하지 않는 내보내기 형식입니다.")
    safe_title = "".join(char for char in note["title"] if char.isalnum() or char in "-_ ").strip()
    encoded_name = quote(f"{safe_title or 'transcript'}.{format_name}")
    headers = {
        "Content-Disposition": (
            f"attachment; filename=transcript.{format_name}; filename*=UTF-8''{encoded_name}"
        )
    }
    return Response(content, media_type=media_type, headers=headers)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
