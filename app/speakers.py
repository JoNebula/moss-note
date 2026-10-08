"""Non-destructive speaker reassignment with text-preserving undo."""
import hashlib
import json


EDITABLE_VERSIONS = ("segments", "corrected_segments", "correction_source_segments")


def summary_fingerprint(summary: dict) -> str:
    return hashlib.sha256(json.dumps(summary, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def summary_context(note: dict) -> str:
    return summary_fingerprint({"segments": note["corrected_segments"], "speaker_names": note["speaker_names"]})


def merge_speakers(note: dict, sources: list[str], target: str) -> dict:
    speakers = {item["speaker"] for item in note["segments"]}
    selected = set(sources)
    if target not in speakers or not selected or not selected <= speakers:
        raise ValueError("현재 노트에 있는 화자만 병합할 수 있습니다.")
    selected.discard(target)
    if not selected:
        raise ValueError("대상 화자와 다른 화자를 하나 이상 선택하세요.")
    history = {"speakers_by_id": {str(item["id"]): item["speaker"] for item in note["segments"]},
               "speaker_names": dict(note["speaker_names"]), "summary_stale": note["summary_stale"],
               "summary_fingerprint": summary_fingerprint(note["summary"]),
               "summary_context": summary_context(note)}
    changes = {field: [{**item, "speaker": target if item["speaker"] in selected else item["speaker"]}
                       for item in note[field]] for field in EDITABLE_VERSIONS}
    remaining = {item["speaker"] for item in changes["segments"]}
    changes["speaker_names"] = {key: value for key, value in note["speaker_names"].items() if key in remaining}
    changes["speaker_merge_history"] = [*note["speaker_merge_history"], history][-20:]
    changes["summary_stale"] = bool(note["summary"]) or note["summary_stale"]
    return changes


def undo_speaker_merge(note: dict) -> dict:
    if not note["speaker_merge_history"]:
        raise ValueError("취소할 화자 병합이 없습니다.")
    previous = note["speaker_merge_history"][-1]
    assignment = previous["speakers_by_id"]
    changes = {field: [{**item, "speaker": assignment.get(str(item["id"]), item["speaker"])}
                       for item in note[field]] for field in EDITABLE_VERSIONS}
    names = {**previous["speaker_names"], **note["speaker_names"]}
    remaining = {item["speaker"] for item in changes["segments"]}
    changes["speaker_names"] = {key: value for key, value in names.items() if key in remaining}
    changes["speaker_merge_history"] = note["speaker_merge_history"][:-1]
    same_summary = summary_fingerprint(note["summary"]) == previous["summary_fingerprint"]
    same_context = summary_context({**note, **changes}) == previous.get("summary_context")
    changes["summary_stale"] = previous["summary_stale"] if same_summary and same_context else bool(note["summary"])
    return changes
