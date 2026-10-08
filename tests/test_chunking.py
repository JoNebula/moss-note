from __future__ import annotations

import wave
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import app.main as main
from app.store import NoteStore
from app.progress import TimingCache


def make_silent_wav(path: Path, seconds: float) -> None:
    sample_rate = 16_000
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * int(sample_rate * seconds))


def test_split_audio_caps_each_chunk(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "normalized.wav"
    make_silent_wav(source, 2.2)
    monkeypatch.setattr(main, "MAX_AUDIO_CHUNK_SECONDS", 1)
    monkeypatch.setattr(main, "CHUNK_OVERLAP_SECONDS", 0)

    chunks = main.split_audio(source, 2.2)

    assert [offset for _, offset in chunks] == [0.0, 1.0, 2.0]
    assert all(path.exists() for path, _ in chunks)
    assert all((main.probe_duration(path) or 0) <= 1.01 for path, _ in chunks)


def test_merge_offsets_time_and_isolates_speakers() -> None:
    local = [{"id": 0, "start": 0.5, "end": 1.5, "speaker": "S01", "text": "말"}]

    merged = main.merge_chunk_segments([(0.0, local), (5400.0, local)])

    assert merged[0]["speaker"] == "S01"
    assert merged[1]["speaker"] == "S02"
    assert merged[1]["start"] == 5400.5
    assert merged[1]["end"] == 5401.5
    assert merged[1]["chunk"] == 2


def test_bridge_connects_same_speaker_across_chunks() -> None:
    left = [
        {"id": 0, "start": 5300.0, "end": 5350.0, "speaker": "S01", "text": "왼쪽"},
    ]
    right = [
        {"id": 0, "start": 10.0, "end": 50.0, "speaker": "S08", "text": "오른쪽"},
    ]
    bridge = [
        {"id": 0, "start": 50.0, "end": 100.0, "speaker": "S03", "text": "왼쪽"},
        {"id": 1, "start": 160.0, "end": 200.0, "speaker": "S03", "text": "오른쪽"},
    ]

    merged = main.merge_chunk_segments(
        [(0.0, left), (5400.0, right)],
        [(5250.0, bridge)],
    )

    assert merged[0]["speaker"] == merged[1]["speaker"] == "S01"


def segment(start, end, speaker, text):
    return {"start": start, "end": end, "speaker": speaker, "text": text}


def test_overlapping_chunks_use_18_minute_stride_without_extra_tail(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MAX_AUDIO_CHUNK_SECONDS", 1200)
    monkeypatch.setattr(main, "CHUNK_OVERLAP_SECONDS", 120)
    commands = []
    monkeypatch.setattr(main, "run_command", commands.append)
    chunks = main.split_audio(tmp_path / "normalized.wav", 2280)
    assert [offset for _, offset in chunks] == [0, 1080]
    assert [cmd[cmd.index("-t") + 1] for cmd in commands] == ["1200", "1200"]


def test_overlap_wav_contains_same_audio_and_complete_tail(monkeypatch, tmp_path):
    source = tmp_path / "normalized.wav"
    make_silent_wav(source, 4.5)
    monkeypatch.setattr(main, "MAX_AUDIO_CHUNK_SECONDS", 2)
    monkeypatch.setattr(main, "CHUNK_OVERLAP_SECONDS", 1)
    chunks = main.split_audio(source, 4.5)
    assert [offset for _, offset in chunks] == [0, 1, 2, 3]
    assert [main.probe_duration(path) for path, _ in chunks] == [2, 2, 2, 1.5]


def test_overlap_connects_swapped_ids_and_removes_duplicate_utterances():
    left = [segment(100, 110, "S01", "처음"),
            segment(1110, 1120, "S01", "네, 맞아요."),
            segment(1135, 1145, "S02", "그 다음 이야기"),
            segment(1170, 1180, "S01", "계속 이야기")]
    right = [segment(30.2, 40.1, "S09", "네 맞아요"),
             segment(55.1, 65.2, "S03", "그 다음 이야기."),
             segment(90, 100, "S09", "계속 이야기"),
             segment(140, 150, "S03", "새로운 이야기")]
    merged = main.merge_chunk_segments([(0, left), (1080, right)],
                                        overlap_seconds=120, chunk_seconds=1200)
    assert len(merged) == 5
    assert [s["text"] for s in merged] == ["처음", "네, 맞아요.", "그 다음 이야기", "계속 이야기", "새로운 이야기"]
    assert [s["speaker"] for s in merged] == ["S01", "S01", "S02", "S01", "S02"]
    assert [s["id"] for s in merged] == list(range(5))
    assert merged[-1]["start"] == 1220


def test_overlap_silent_boundary_does_not_invent_speaker_connection():
    left = [segment(10, 20, "S01", "왼쪽")]
    right = [segment(130, 140, "S01", "오른쪽")]
    merged = main.merge_chunk_segments([(0, left), (1080, right)],
                                        overlap_seconds=120, chunk_seconds=1200)
    assert [s["speaker"] for s in merged] == ["S01", "S02"]
    assert len(merged) == 2


def test_overlap_mapping_propagates_across_three_chunks():
    first = [segment(1130, 1140, "S01", "첫 번째 연결")]
    middle = [segment(50, 60, "S09", "첫 번째 연결"),
              segment(1130, 1140, "S09", "두 번째 연결")]
    last = [segment(50, 60, "S03", "두 번째 연결"),
            segment(140, 150, "S03", "마지막")]
    merged = main.merge_chunk_segments([(0, first), (1080, middle), (2160, last)],
                                        overlap_seconds=120, chunk_seconds=1200)
    assert [s["text"] for s in merged] == ["첫 번째 연결", "두 번째 연결", "마지막"]
    assert {s["speaker"] for s in merged} == {"S01"}


def test_overlap_without_matching_text_partitions_at_midpoint():
    left = [segment(1100, 1110, "S01", "왼쪽 유지"),
            segment(1160, 1170, "S01", "왼쪽 버림")]
    right = [segment(20, 30, "S08", "오른쪽 버림"),
             segment(80, 90, "S08", "오른쪽 유지")]
    merged = main.merge_chunk_segments([(0, left), (1080, right)],
                                        overlap_seconds=120, chunk_seconds=1200)
    assert [s["text"] for s in merged] == ["왼쪽 유지", "오른쪽 유지"]


def test_short_single_chunk_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "MAX_AUDIO_CHUNK_SECONDS", 1200)
    monkeypatch.setattr(main, "CHUNK_OVERLAP_SECONDS", 120)
    source = tmp_path / "normalized.wav"
    assert main.split_audio(source, 1200) == [(source, 0)]
    result = main.merge_chunk_segments([(0, [segment(1, 2, "S01", "테스트")])],
                                      overlap_seconds=120, chunk_seconds=1200)
    assert len(result) == 1 and result[0]["start"] == 1


def test_pipeline_transcribes_only_overlapping_chunks_and_records_plan(monkeypatch, tmp_path):
    source = tmp_path / "source.wav"
    make_silent_wav(source, 4.5)
    store = NoteStore(tmp_path / "notes.sqlite3")
    store.create({"id": "overlap", "title": "test", "source_path": str(source),
                  "original_filename": "source.wav", "status": "queued"})
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main, "TIMINGS", TimingCache(tmp_path / "timings.sqlite3"))
    monkeypatch.setattr(main, "MAX_AUDIO_CHUNK_SECONDS", 2)
    monkeypatch.setattr(main, "CHUNK_OVERLAP_SECONDS", 1)
    monkeypatch.setattr(main.MODEL_RUNTIME, "ensure", AsyncMock())
    monkeypatch.setattr(main.FAN_CONTROL, "start", AsyncMock())
    monkeypatch.setattr(main.FAN_CONTROL, "stop", AsyncMock())
    def unexpected_bridge(*args):
        raise AssertionError("Overlapping chunks must not create extra bridge requests")
    monkeypatch.setattr(main, "make_bridge_chunks", unexpected_bridge)
    calls = []
    async def transcribe_chunk(client, path, note):
        offset = int(path.stem.split("-")[1])
        calls.append(offset)
        duration = main.probe_duration(path)
        segments = [segment(i / 2 + 0.1, i / 2 + 0.4, f"S{offset + 1:02}",
                            f"발화 {offset * 2 + i}") for i in range(round(duration * 2))]
        return main.canonical_transcript(segments), segments
    monkeypatch.setattr(main, "transcribe_chunk", transcribe_chunk)
    asyncio.run(main.transcribe("overlap"))
    note = store.get("overlap")
    assert note["status"] == "done", note["error"]
    assert calls == [0, 1, 2, 3]
    assert note["chunk_seconds"] == 2 and note["chunk_overlap_seconds"] == 1
    assert note["chunk_count"] == note["processed_chunks"] == 4
    assert [s["text"] for s in note["segments"]] == [f"발화 {i}" for i in range(9)]
    assert {s["speaker"] for s in note["segments"]} == {"S01"}
    assert not (tmp_path / "chunks").exists()
