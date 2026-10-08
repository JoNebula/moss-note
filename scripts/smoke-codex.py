#!/usr/bin/env python3
"""Exercise real Codex correction using synthetic, non-private transcript data."""
import asyncio
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.codex_postprocess import CodexPostprocessor, new_run_directory, summary_markdown
from app.store import NoteStore


def source_segments():
    return [
        {"id": 0, "start": 0.0, "end": 10.0, "speaker": "S01",
         "text": "쿠버네티스에서 레디스 캐시를 쓰면 될 것 같습니다."},
        {"id": 1, "start": 10.0, "end": 20.0, "speaker": "S02",
         "text": "금요일까지 캐시를 테스트하고 결과를 공유하겠습니다."},
    ]


def check_api():
    import httpx
    from dotenv import dotenv_values

    configuration = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    for _ in range(60):
        try:
            httpx.get("http://127.0.0.1:8000/healthz", timeout=2).raise_for_status()
            break
        except (httpx.ConnectError, httpx.HTTPStatusError):
            time.sleep(0.25)
    else:
        raise RuntimeError("The app did not become ready; no test note was created.")
    data = Path(configuration["MOSS_DATA_DIR"])
    store = NoteStore(data / "moss-note.sqlite3")
    source = source_segments()
    directory = new_run_directory(data / "diagnostics/codex-smoke", "api")
    note_id = uuid.uuid4().hex
    audio = data / "uploads" / note_id / "synthetic.wav"
    audio.parent.mkdir(parents=True)
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(bytes(20 * 32000))
    store.create({"id": note_id, "title": "Synthetic Codex correction smoke", "original_filename": audio.name,
                  "source_path": str(audio), "media_type": "audio/wav", "language": "ko",
                  "hotwords": "Kubernetes, Redis", "status": "done"})
    store.update(note_id, segments=source, raw_segments=source, duration=20,
                 speaker_names={"S01": "A", "S02": "B"})
    auth = (configuration["MOSS_AUTH_USERNAME"], configuration["MOSS_AUTH_PASSWORD"])
    with httpx.Client(base_url="http://127.0.0.1:8000", auth=auth, timeout=30) as client:
        client.post(f"/api/notes/{note_id}/postprocess").raise_for_status()
        started, phases = time.monotonic(), []
        while time.monotonic() - started < 300:
            response = client.get(f"/api/notes/{note_id}")
            response.raise_for_status()
            note = response.json()
            phase = (note["correction_status"], note["correction_phase"], note["correction_processed_windows"])
            if not phases or phase != phases[-1]:
                phases.append(phase)
                print(phase, flush=True)
            if note["correction_status"] in {"done", "error"}:
                break
            time.sleep(1)
        assert note["correction_status"] == "done", note["correction_error"]
        assert note["raw_segments"] == note["segments"] == source
        assert note["correction_model"] == "gpt-6.1-sol" and note["correction_backend"] == "codex-cli"
        assert note["correction_mode"] == "whole-file"
        assert note["correction_output_mode"] == "changes"
        assert note["correction_service_tier"] == "priority"
        assert note["correction_total_windows"] == note["correction_processed_windows"] == 1
        artifacts = Path(store.get(note_id)["correction_artifacts_path"])
        assert len(list(artifacts.glob("*/manifest.json"))) == 1
        assert json.loads((artifacts / "whole-file" / "manifest.json").read_text())["requested_service_tier"] == "priority"
        assert json.loads((artifacts / "whole-file" / "manifest.json").read_text())["output_mode"] == "changes"
        whole_file = artifacts / "whole-file"
        assert json.loads((whole_file / "input-transcript.json").read_text())["segments"] == source
        assert set(json.loads((whole_file / "result.json").read_text())) == {"segments", "summary"}
        assert len(note["corrected_segments"]) == len(source)
        for before, after in zip(source, note["corrected_segments"], strict=True):
            assert {key: value for key, value in before.items() if key != "text"} == {key: value for key, value in after.items() if key != "text"}
        assert note["summary"]["key_points"] and note["summary"]["action_items"]
        for route, filename in [("export/txt?version=corrected", "corrected.txt"),
                                ("export/srt?version=corrected", "corrected.srt"),
                                ("summary/md", "summary.md"), ("summary/txt", "summary.txt")]:
            response = client.get(f"/api/notes/{note_id}/{route}")
            response.raise_for_status()
            assert "attachment" in response.headers["content-disposition"]
            if route.startswith("summary/"):
                assert "00:00:10" not in response.text and "segment_ids" not in response.text
            (directory / filename).write_text(response.text)
        correction_seconds = time.monotonic() - started
        merged = client.post(f"/api/notes/{note_id}/speakers/merge", json={"sources": ["S02"], "target": "S01"})
        merged.raise_for_status()
        merged = merged.json()
        assert merged["speaker_merge_undo_count"] == 1 and merged["summary_stale"]
        assert merged["raw_segments"] == source
        assert {segment["speaker"] for segment in merged["corrected_segments"]} == {"S01"}
        edited = [{**segment, "text": segment["text"] + " 확인."} for segment in merged["corrected_segments"]]
        client.patch(f"/api/notes/{note_id}", json={"corrected_segments": edited}).raise_for_status()
        restored = client.post(f"/api/notes/{note_id}/speakers/undo")
        restored.raise_for_status()
        restored = restored.json()
        assert [segment["speaker"] for segment in restored["corrected_segments"]] == ["S01", "S02"]
        assert [segment["text"] for segment in restored["corrected_segments"]] == [segment["text"] for segment in edited]
        assert restored["raw_segments"] == source and restored["summary_stale"]
        (directory / "report.json").write_text(json.dumps({"note": note, "phases": phases,
                "seconds": correction_seconds, "service_artifacts": str(artifacts),
                "speaker_merge_and_text_preserving_undo": True}, ensure_ascii=False, indent=2))
        client.delete(f"/api/notes/{note_id}").raise_for_status()
        print(f"One-call API correction/summary, downloads, speaker merge and text-preserving undo passed: {directory}")


async def main():
    source = source_segments()
    directory = new_run_directory(Path(os.getenv("MOSS_DATA_DIR", "/data/artifacts/moss-note")) / "diagnostics/codex-smoke", "synthetic")
    worker = CodexPostprocessor.from_env()
    document = {"title": "Synthetic cache meeting", "known_terms": "Kubernetes, Redis",
                "speaker_names": {"S01": "A", "S02": "B"}, "segments": source}
    corrected, summary, usage = await worker.process(document, directory / "whole-file")
    report = {"directory": str(directory), "corrected": corrected, "summary": summary,
              "usage": usage, "invocations": 1}
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    (directory / "summary.md").write_text(summary_markdown(summary))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", action="store_true", help="Test live app using a temporary synthetic note")
    args = parser.parse_args()
    if args.api:
        check_api()
    else:
        asyncio.run(main())
