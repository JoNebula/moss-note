import asyncio
from copy import deepcopy
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

import app.main as main
from app.speakers import merge_speakers, undo_speaker_merge
from app.store import NoteStore


@pytest.fixture
def note(monkeypatch, tmp_path):
    store = NoteStore(tmp_path / "notes.sqlite3")
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "QUEUE", asyncio.Queue())
    monkeypatch.setattr(main, "CORRECTION_QUEUE", asyncio.Queue())
    monkeypatch.setattr(main, "AUTH_USERNAME", "")
    segments = [{"id": index, "start": index * 10.0, "end": index * 10.0 + 10,
                 "speaker": f"S0{index + 1}", "text": f"원문 {index}"} for index in range(3)]
    store.create({"id": "test", "title": "회의", "source_path": str(tmp_path / "audio.wav"),
                  "original_filename": "audio.wav", "media_type": "audio/wav", "status": "done",
                  "language": "ko", "hotwords": None})
    return store.update("test", segments=segments, corrected_segments=segments, raw_segments=segments,
                        correction_source_segments=segments, speaker_names={"S01": "A", "S02": "B", "S03": "C"})


def test_merge_applies_to_editable_versions_and_exports_not_raw(note):
    before = deepcopy(note)
    result = main.merge_note_speakers("test", main.SpeakerMerge(sources=["S02", "S03", "S02"], target="S01"))
    for field in ("segments", "corrected_segments", "correction_source_segments"):
        assert {item["speaker"] for item in result[field]} == {"S01"}
        for original, merged in zip(before[field], result[field], strict=True):
            assert {key: value for key, value in original.items() if key != "speaker"} == {key: value for key, value in merged.items() if key != "speaker"}
    assert result["raw_segments"] == before["raw_segments"]
    assert result["speaker_names"] == {"S01": "A"}
    assert result["speaker_merge_undo_count"] == 1
    assert "speaker_merge_history" not in result
    exported = json.loads(main.export_note("test", "json", "corrected").body)
    assert {item["speaker"] for item in exported["segments"]} == {"S01"}
    assert "speaker_merge_history" not in exported and "correction_artifacts_path" not in exported
    raw = json.loads(main.export_note("test", "json", "raw").body)
    assert raw["segments"] == before["raw_segments"]


def test_undo_keeps_subsequent_text_and_name_edits(note):
    main.merge_note_speakers("test", main.SpeakerMerge(sources=["S02"], target="S01"))
    current = main.STORE.get("test")
    edited = [{**item, "text": "나중에 편집한 문장"} for item in current["corrected_segments"]]
    main.STORE.update("test", corrected_segments=edited, speaker_names={"S01": "Alice", "S03": "C"})
    result = main.undo_note_speakers("test")
    assert [item["speaker"] for item in result["corrected_segments"]] == ["S01", "S02", "S03"]
    assert all(item["text"] == "나중에 편집한 문장" for item in result["corrected_segments"])
    assert result["speaker_names"] == {"S01": "Alice", "S02": "B", "S03": "C"}
    assert result["speaker_merge_undo_count"] == 0
    assert result["raw_segments"] == note["raw_segments"]


@pytest.mark.parametrize("sources,target", [(["S99"], "S01"), (["S01"], "S01"), (["S01"], "S99"), ([], "S01")])
def test_invalid_merge_has_no_effect(note, sources, target):
    with pytest.raises(ValueError):
        merge_speakers(note, sources, target)
    assert main.STORE.get("test") == note


@pytest.mark.parametrize("status,correction", [("processing", "idle"), ("done", "queued"), ("done", "processing")])
def test_merge_and_undo_are_blocked_during_processing(note, status, correction):
    main.STORE.update("test", status=status, correction_status=correction)
    for operation in (lambda: main.merge_note_speakers("test", main.SpeakerMerge(sources=["S02"], target="S01")),
                      lambda: main.undo_note_speakers("test")):
        with pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 409


def test_summary_stale_flag_and_undo_restore(note):
    main.STORE.update("test", summary={"title": "old summary"})
    merged = main.merge_note_speakers("test", main.SpeakerMerge(sources=["S02"], target="S01"))
    assert merged["summary_stale"] is True and merged["summary"] == {"title": "old summary"}
    restored = main.undo_note_speakers("test")
    assert restored["summary_stale"] is False
    main.merge_note_speakers("test", main.SpeakerMerge(sources=["S02"], target="S01"))
    main.STORE.update("test", summary={"title": "new merged-speaker summary"}, summary_stale=False)
    restored = main.undo_note_speakers("test")
    assert restored["summary_stale"] is True


def test_undo_after_new_correction_restores_all_speaker_ids(note):
    main.STORE.update("test", corrected_segments=[], correction_source_segments=[])
    main.merge_note_speakers("test", main.SpeakerMerge(sources=["S02"], target="S01"))
    current = main.STORE.get("test")
    main.STORE.update("test", corrected_segments=current["segments"], correction_source_segments=current["segments"])
    result = main.undo_note_speakers("test")
    assert [item["speaker"] for item in result["corrected_segments"]] == ["S01", "S02", "S03"]


def test_undo_after_text_edit_keeps_summary_stale(note):
    main.STORE.update("test", summary={"title": "old summary"})
    main.merge_note_speakers("test", main.SpeakerMerge(sources=["S02"], target="S01"))
    current = main.STORE.get("test")
    edited = [{**item, "text": "수정된 문장"} for item in current["corrected_segments"]]
    main.patch_note("test", main.NotePatch(corrected_segments=edited))
    restored = main.undo_note_speakers("test")
    assert restored["summary_stale"] is True


def test_merge_stack_and_empty_undo(note):
    with pytest.raises(ValueError):
        undo_speaker_merge(note)
    main.merge_note_speakers("test", main.SpeakerMerge(sources=["S02"], target="S01"))
    main.merge_note_speakers("test", main.SpeakerMerge(sources=["S03"], target="S01"))
    first = main.undo_note_speakers("test")
    assert [item["speaker"] for item in first["segments"]] == ["S01", "S01", "S03"]
    second = main.undo_note_speakers("test")
    assert second["segments"] == note["segments"]


def test_merge_endpoint_and_input_validation(note):
    with TestClient(main.app) as client:
        assert client.post("/api/notes/test/speakers/merge", json={"sources": [], "target": "S01"}).status_code == 422
        assert client.post("/api/notes/test/speakers/merge", json={"sources": ["S99"], "target": "S01"}).status_code == 400
        response = client.post("/api/notes/test/speakers/merge", json={"sources": ["S02"], "target": "S01"})
        assert response.status_code == 200 and response.json()["speaker_merge_undo_count"] == 1
        assert client.post("/api/notes/test/speakers/undo").status_code == 200
        assert client.post("/api/notes/test/speakers/undo").status_code == 409
        assert client.post("/api/notes/missing/speakers/merge", json={"sources": ["S02"], "target": "S01"}).status_code == 404
