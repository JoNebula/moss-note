from __future__ import annotations

import wave
from pathlib import Path

import app.main as main


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
