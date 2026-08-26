from __future__ import annotations

import pytest

from app.postprocess import (
    apply_corrections,
    make_correction_windows,
    validate_corrections,
)


def segments(count: int) -> list[dict]:
    return [
        {
            "id": index,
            "start": float(index),
            "end": float(index + 1),
            "speaker": "S01",
            "text": f"원문 {index}",
        }
        for index in range(count)
    ]


def test_sliding_windows_have_context_but_targets_do_not_overlap() -> None:
    source = segments(121)
    windows = make_correction_windows(source, target_size=48, context_size=12)

    assert [len(window.target_ids) for window in windows] == [48, 48, 25]
    assert windows[0].segments[0]["id"] == 0
    assert windows[0].segments[-1]["id"] == 59
    assert windows[1].segments[0]["id"] == 36
    assert windows[1].segments[-1]["id"] == 107
    assert [item for window in windows for item in window.target_ids] == list(range(121))


def test_validation_accepts_term_fix_and_preserves_numbers() -> None:
    source = [
        {"id": 1, "text": "쿠버네티스 1.29 버전을 써요"},
        {"id": 2, "text": "레디스 서버입니다"},
    ]
    response = {
        "segments": [
            {"id": 1, "text": "Kubernetes 2.0 버전을 써요"},
            {"id": 2, "text": "Redis 서버입니다."},
        ]
    }

    result = validate_corrections(source, [1, 2], response)

    assert result[1] == source[0]["text"]
    assert result[2] == "Redis 서버입니다."


def test_validation_rejects_missing_or_duplicate_ids() -> None:
    source = [{"id": 1, "text": "원문"}, {"id": 2, "text": "원문"}]
    with pytest.raises(ValueError):
        validate_corrections(
            source,
            [1, 2],
            {"segments": [{"id": 1, "text": "교정"}, {"id": 1, "text": "교정"}]},
        )


def test_apply_corrections_changes_text_only() -> None:
    source = segments(2)
    corrected = apply_corrections(source, {0: "Redis를 사용합니다."})

    assert corrected[0]["text"] == "Redis를 사용합니다."
    assert {key: value for key, value in corrected[0].items() if key != "text"} == {
        key: value for key, value in source[0].items() if key != "text"
    }
    assert corrected[1] == source[1]
