from __future__ import annotations

import asyncio
import base64
import binascii
import mimetypes
import os
import secrets
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.postprocess import (
    apply_corrections,
    correct_window,
    make_correction_windows,
)
from app.store import NoteStore
from app.transcript import json_text, parse_transcript, plain_text, srt_text, vtt_text


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("MOSS_DATA_DIR", ROOT / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
STATIC_DIR = ROOT / "static"
VLLM_URL = os.getenv("MOSS_VLLM_URL", "http://127.0.0.1:8001/v1").rstrip("/")
MODEL_NAME = os.getenv("MOSS_MODEL_NAME", "moss-mtd")
QWEN_VLLM_URL = os.getenv("QWEN_VLLM_URL", "http://127.0.0.1:8002/v1").rstrip("/")
QWEN_MODEL_NAME = os.getenv("QWEN_MODEL_NAME", "qwen3.8-27b")
QWEN_REQUIRED = os.getenv("MOSS_REQUIRE_QWEN", "true").lower() not in {
    "0",
    "false",
    "no",
}
AUTH_USERNAME = os.getenv("MOSS_AUTH_USERNAME", "")
AUTH_PASSWORD = os.getenv("MOSS_AUTH_PASSWORD", "")
CORRECTION_TARGET_SIZE = max(1, int(os.getenv("QWEN_WINDOW_TARGET", "48")))
CORRECTION_CONTEXT_SIZE = max(0, int(os.getenv("QWEN_WINDOW_CONTEXT", "12")))
CORRECTION_MAX_TOKENS = max(512, int(os.getenv("QWEN_MAX_TOKENS", "8192")))
MAX_UPLOAD_BYTES = int(float(os.getenv("MOSS_MAX_UPLOAD_GB", "5")) * 1024**3)
MAX_COMPLETION_TOKENS = int(os.getenv("MOSS_MAX_COMPLETION_TOKENS", "65536"))
# The official model limit is 90 minutes. The env var may lower the chunk size
# for constrained deployments or testing, but never raises it beyond 90 min.
MAX_AUDIO_CHUNK_SECONDS = max(
    1, min(int(os.getenv("MOSS_CHUNK_SECONDS", "5400")), 5400)
)
BRIDGE_AUDIO_SECONDS = max(
    2, min(int(os.getenv("MOSS_BRIDGE_SECONDS", "300")), MAX_AUDIO_CHUNK_SECONDS)
)
STORE = NoteStore(DATA_DIR / "moss-note.sqlite3")
QUEUE: asyncio.Queue[str] = asyncio.Queue()
CORRECTION_QUEUE: asyncio.Queue[str] = asyncio.Queue()

DEFAULT_PROMPT = (
    "请将音频转写为文本，每一段需以起始时间戳和说话人编号（[S01]、[S02]、[S03]…）开头，"
    "正文为对应的语音内容，并在段末标注结束时间戳，以清晰标明该段语音范围。"
)


class NotePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    segments: list[dict[str, Any]] | None = None
    corrected_segments: list[dict[str, Any]] | None = None
    speaker_names: dict[str, str] | None = None


def public_note(note: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in note.items()
        if key
        not in {
            "source_path",
            "normalized_path",
            "raw_transcript",
            "correction_partial",
        }
    }


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
    """Return <=90-minute WAV chunks and their original-timeline offsets."""
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
        offset += chunk_duration
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
) -> dict[str, str]:
    """Greedily find a one-to-one speaker mapping on duplicated audio."""
    scores: dict[tuple[str, str], float] = {}
    for source in source_segments:
        source_start = float(source["start"]) + source_offset
        source_end = float(source["end"]) + source_offset
        for bridge in bridge_segments:
            bridge_start = float(bridge["start"]) + bridge_offset
            bridge_end = float(bridge["end"]) + bridge_offset
            overlap = min(source_end, bridge_end) - max(source_start, bridge_start)
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


def merge_chunk_segments(
    results: list[tuple[float, list[dict[str, Any]]]],
    bridges: list[tuple[float, list[dict[str, Any]]]] | None = None,
) -> list[dict[str, Any]]:
    """Offset timestamps and connect adjacent speaker IDs through bridges."""
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
    return merged


def canonical_transcript(segments: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[{segment['start']:.2f}][{segment['speaker']}]"
        f"{segment['text']}[{segment['end']:.2f}]"
        for segment in segments
    )


async def transcribe(note_id: str) -> None:
    note = STORE.update(
        note_id, status="processing", error=None, processed_chunks=0
    )
    source = Path(note["source_path"])
    normalized = source.parent / "normalized.wav"
    chunk_dir = normalized.parent / "chunks"
    try:
        await asyncio.to_thread(normalize_audio, source, normalized)
        duration = await asyncio.to_thread(probe_duration, normalized)
        chunks = await asyncio.to_thread(split_audio, normalized, duration)
        bridges = await asyncio.to_thread(
            make_bridge_chunks, normalized, duration, chunks
        )
        STORE.update(
            note_id,
            normalized_path=str(normalized),
            duration=duration,
            chunk_count=len(chunks),
        )

        timeout = httpx.Timeout(connect=30, read=6 * 60 * 60, write=60 * 60, pool=30)
        chunk_results: list[tuple[float, list[dict[str, Any]]]] = []
        bridge_results: list[tuple[float, list[dict[str, Any]]]] = []
        raw_results: list[str] = []
        async with httpx.AsyncClient(timeout=timeout) as client:
            for index, (chunk_path, offset) in enumerate(chunks, start=1):
                raw, segments = await transcribe_chunk(client, chunk_path, note)
                raw_results.append(raw)
                chunk_results.append((offset, segments))
                STORE.update(note_id, processed_chunks=index)

            for bridge_path, offset in bridges:
                _, segments = await transcribe_chunk(client, bridge_path, note)
                bridge_results.append((offset, segments))

        segments = merge_chunk_segments(chunk_results, bridge_results)
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
            speaker_names={speaker: speaker for speaker in speakers},
        )
    except Exception as error:
        detail = str(error)
        if isinstance(error, httpx.HTTPStatusError):
            detail = f"vLLM {error.response.status_code}: {error.response.text[:1000]}"
        STORE.update(note_id, status="error", error=detail)
    finally:
        shutil.rmtree(chunk_dir, ignore_errors=True)


async def queue_worker() -> None:
    while True:
        note_id = await QUEUE.get()
        try:
            await transcribe(note_id)
        finally:
            QUEUE.task_done()


async def postprocess_transcript(note_id: str) -> None:
    note = STORE.get(note_id)
    resume = bool(
        note["correction_source_segments"]
        and note["correction_partial"]
        and note["correction_total_windows"]
    )
    source_segments = [
        {**segment}
        for segment in (
            note["correction_source_segments"] if resume else note["segments"]
        )
    ]
    windows = make_correction_windows(
        source_segments, CORRECTION_TARGET_SIZE, CORRECTION_CONTEXT_SIZE
    )
    processed_windows = min(
        note["correction_processed_windows"] if resume else 0, len(windows)
    )
    STORE.update(
        note_id,
        correction_status="processing",
        correction_error=None,
        correction_source_segments=source_segments,
        correction_total_windows=len(windows),
        correction_processed_windows=processed_windows,
        correction_model=QWEN_MODEL_NAME,
    )
    corrected_text: dict[int, str] = {
        int(segment_id): text
        for segment_id, text in (note["correction_partial"] if resume else {}).items()
    }
    try:
        timeout = httpx.Timeout(connect=30, read=60 * 60, write=60, pool=30)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for index, window in enumerate(
                windows[processed_windows:], start=processed_windows + 1
            ):
                corrected_text.update(
                    await correct_window(
                        client,
                        QWEN_VLLM_URL,
                        QWEN_MODEL_NAME,
                        window,
                        note.get("hotwords"),
                        CORRECTION_MAX_TOKENS,
                    )
                )
                STORE.update(
                    note_id,
                    correction_processed_windows=index,
                    correction_partial=corrected_text,
                )
        corrected_segments = apply_corrections(source_segments, corrected_text)
        changes = sum(
            before.get("text", "") != after.get("text", "")
            for before, after in zip(source_segments, corrected_segments, strict=True)
        )
        STORE.update(
            note_id,
            corrected_segments=corrected_segments,
            correction_partial={},
            correction_status="done",
            correction_error=None,
            correction_changes=changes,
        )
    except Exception as error:
        detail = str(error)
        if isinstance(error, httpx.HTTPStatusError):
            detail = f"Qwen vLLM {error.response.status_code}: {error.response.text[:1000]}"
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
    if request.url.path not in {"/healthz", "/readyz"} and not valid_basic_auth(
        request.headers.get("Authorization")
    ):
        return Response(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="MOSS Note", charset="UTF-8"'},
        )
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
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
    }


@app.post("/api/notes", status_code=202)
async def create_note(
    file: UploadFile = File(...),
    title: str = Form(""),
    language: str = Form("ko"),
    hotwords: str = Form(""),
) -> dict[str, Any]:
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
    changes = patch.model_dump(exclude_none=True)
    if "title" in changes:
        changes["title"] = changes["title"].strip()
    try:
        note = STORE.get(note_id)
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
def start_postprocess(note_id: str) -> dict[str, Any]:
    try:
        note = STORE.get(note_id)
    except KeyError:
        raise HTTPException(404, "노트를 찾을 수 없습니다.") from None
    if note["status"] != "done" or not note["segments"]:
        raise HTTPException(409, "전사가 완료된 노트만 교정할 수 있습니다.")
    if note["correction_status"] in {"queued", "processing"}:
        raise HTTPException(409, "이미 AI 교정 작업이 진행 중입니다.")
    note = STORE.update(
        note_id,
        correction_status="queued",
        correction_error=None,
        correction_source_segments=(
            note["correction_source_segments"]
            if note["correction_status"] == "error" and note["correction_partial"]
            else []
        ),
        correction_partial=(
            note["correction_partial"]
            if note["correction_status"] == "error" and note["correction_partial"]
            else {}
        ),
        correction_total_windows=(
            note["correction_total_windows"]
            if note["correction_status"] == "error" and note["correction_partial"]
            else 0
        ),
        correction_processed_windows=(
            note["correction_processed_windows"]
            if note["correction_status"] == "error" and note["correction_partial"]
            else 0
        ),
    )
    CORRECTION_QUEUE.put_nowait(note_id)
    return public_note(note)


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
    export_note_data = {**note, "segments": selected_segments}
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
