import pytest
import asyncio
from fastapi.testclient import TestClient

import app.main as main
from app.progress import TimingCache, progress_state
from app.store import NoteStore


@pytest.fixture(autouse=True)
def isolate_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "QUEUE", asyncio.Queue())
    monkeypatch.setattr(main, "CORRECTION_QUEUE", asyncio.Queue())
    monkeypatch.setattr(main, "STORE", NoteStore(tmp_path / "runtime-notes.sqlite3"))
    monkeypatch.setattr(main, "TIMINGS", TimingCache(tmp_path / "runtime-timings.sqlite3"))


def test_timing_cache_persists_and_isolates_model_and_duration_bands(tmp_path):
    path = tmp_path / "timings.sqlite3"
    cache = TimingCache(path)
    cache.record("rtn-w4", 1200, 100)
    cache.record("rtn-w4", 1200, 120)
    cache.record("rtn-w4", 600, 100)
    cache.record("bf16", 1200, 300)
    reopened = TimingCache(path)
    estimate, count = reopened.estimate("rtn-w4", 1200)
    assert estimate == pytest.approx(110) and count == 2
    assert reopened.estimate("bf16", 1200) == (300, 1)
    assert reopened.estimate("rtn-w4", 600) == (100, 1)
    assert reopened.estimate("rtn-w8", 1200) == (288, 0)


def test_cache_rejects_invalid_samples_and_bounds_history(tmp_path):
    cache = TimingCache(tmp_path / "timings.sqlite3")
    for value in (0, -1, float("nan"), float("inf")):
        cache.record("bf16", 1200, value)
        cache.record("bf16", value, 10)
    assert cache.estimate("bf16", 1200)[1] == 0
    for _ in range(45):
        cache.record("bf16", 1200, 100)
    assert cache.estimate("bf16", 1200)[1] == 40


def test_progress_has_partial_chunk_eta_and_never_pretends_completion(tmp_path):
    cache = TimingCache(tmp_path / "timings.sqlite3")
    cache.record("rtn-w4", 1200, 100)
    note = {"status": "processing", "model_variant": "rtn-w4", "processed_chunks": 0,
            "processing": {"phase": "transcribing", "phase_started_at": 1000,
                           "chunk_durations": [1200, 240]}}
    progress = progress_state(note, cache, now=1050)
    assert progress["percent"] == 40.3
    assert progress["remaining_seconds"] == 74
    late = progress_state(note, cache, now=2000)
    assert late["percent"] < 100 and late["remaining_seconds"] > 0
    note["processed_chunks"] = 1
    note["processing"]["phase_started_at"] = 1050
    assert progress_state(note, cache, now=1050)["remaining_seconds"] == 24
    note["processed_chunks"] = 2
    note["processing"]["phase"] = "merging"
    assert progress_state(note, cache, now=1060)["percent"] == 99
    note["status"] = "done"
    assert progress_state(note, cache)["percent"] == 100


def test_queued_and_preparing_progress_are_indeterminate(tmp_path):
    cache = TimingCache(tmp_path / "timings.sqlite3")
    for status, phase in (("queued", "transcribing"), ("processing", "preparing")):
        state = progress_state({"status": status, "processing": {"phase": phase}}, cache)
        assert state["percent"] is None and state["remaining_seconds"] is None


def test_upload_probe_requires_auth_and_never_creates_notes(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "AUTH_USERNAME", "admin")
    monkeypatch.setattr(main, "AUTH_PASSWORD", "admin")
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 1024)
    with TestClient(main.app) as client:
        assert client.post("/api/diagnostics/upload", content=b"probe").status_code == 401
        response = client.post("/api/diagnostics/upload", auth=("admin", "admin"), content=b"probe")
        assert response.status_code == 200
        assert response.json()["bytes"] == 5 and not response.json()["storage_written"]
        assert client.post("/api/diagnostics/upload", auth=("admin", "admin"), content=b"x" * 1025).status_code == 413


def test_browser_upload_measurements_are_bounded_persisted_and_authenticated(tmp_path, monkeypatch):
    store = NoteStore(tmp_path / "notes.sqlite3")
    store.create({"id": "test-note", "title": "test", "original_filename": "test.wav",
                  "source_path": str(tmp_path / "test.wav"), "status": "done"})
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "AUTH_USERNAME", "admin")
    monkeypatch.setattr(main, "AUTH_PASSWORD", "admin")
    payload = {"note_id": "test-note", "bytes": 40000000, "transfer_seconds": 20,
               "response_wait_seconds": 2, "cf_ray": "test-ICN"}
    with TestClient(main.app) as client:
        assert client.post("/api/diagnostics/upload-metrics", json=payload).status_code == 401
        assert client.post("/api/diagnostics/upload-metrics", auth=("admin", "admin"), json=payload).status_code == 204
        result = client.get("/api/diagnostics/upload-metrics", auth=("admin", "admin")).json()[0]
        assert result["transfer_seconds"] == 20 and result["response_wait_seconds"] == 2
        assert result["cf_ray"] == "test-ICN"
        invalid = {**payload, "transfer_seconds": -1}
        assert client.post("/api/diagnostics/upload-metrics", auth=("admin", "admin"), json=invalid).status_code == 422
        unknown = {**payload, "note_id": "missing"}
        assert client.post("/api/diagnostics/upload-metrics", auth=("admin", "admin"), json=unknown).status_code == 404
    for _ in range(45):
        store.record_upload(payload)
    assert len(NoteStore(store.database).upload_metrics()) == 40
