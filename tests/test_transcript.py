from app.transcript import format_srt_time, parse_transcript, srt_text


def test_parse_contiguous_multiline_segments() -> None:
    raw = """[0.48][S01]안녕하세요.[1.66]
    [2.10][S02] 반갑습니다.\n회의를 시작하죠. [5.25]"""
    assert parse_transcript(raw) == [
        {"id": 0, "start": 0.48, "end": 1.66, "speaker": "S01", "text": "안녕하세요."},
        {
            "id": 1,
            "start": 2.1,
            "end": 5.25,
            "speaker": "S02",
            "text": "반갑습니다. 회의를 시작하죠.",
        },
    ]


def test_srt_export_uses_renamed_speaker() -> None:
    segments = parse_transcript("[0.0][S01]테스트[1.234]")
    assert "민수: 테스트" in srt_text(segments, {"S01": "민수"})
    assert format_srt_time(3661.001) == "01:01:01,001"
