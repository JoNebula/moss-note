"""Validate real overlapping transcription without changing existing notes."""
import argparse
import json
from pathlib import Path
import subprocess
import time

import httpx
from dotenv import dotenv_values


parser = argparse.ArgumentParser()
parser.add_argument("audio", type=Path)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
configuration = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
auth = (configuration["MOSS_AUTH_USERNAME"], configuration["MOSS_AUTH_PASSWORD"])
with httpx.Client(base_url="http://127.0.0.1:8000", auth=auth, timeout=30) as client:
    health = client.get("/api/health").json()
    assert health["limits"]["chunk_seconds"] == 1200
    assert health["limits"]["overlap_seconds"] == 120
    variant = health["model_runtime"]["active"]
    assert variant is not None
    with args.audio.open("rb") as audio:
        response = client.post("/api/notes", data={"title": "Overlap smoke: 22 minute sample",
                               "language": "ko", "model_variant": variant},
                               files={"file": (args.audio.name, audio)})
    response.raise_for_status()
    note_id = response.json()["id"]
    print(f"Started overlap smoke {note_id}, variant {variant}", flush=True)
    started = time.monotonic()
    previous = None
    saw_max = False
    while time.monotonic() - started < 900:
        response = client.get(f"/api/notes/{note_id}")
        response.raise_for_status()
        note = response.json()
        saw_max |= any(path.read_text().strip() == "255" for path in
                       Path("/sys/devices/platform/pwm-fan/hwmon").glob("hwmon*/pwm1"))
        progress = (note["status"], note["processed_chunks"], note["chunk_count"])
        if progress != previous:
            print(progress, flush=True)
            previous = progress
        if note["status"] in {"done", "error"}:
            break
        time.sleep(2)
    else:
        raise RuntimeError(f"Timed out: {note_id}")
    time.sleep(1)
    automatic = subprocess.run(["systemctl", "is-active", "nvfancontrol"],
                               capture_output=True, text=True).stdout.strip()
    report = {"audio": str(args.audio), "seconds": round(time.monotonic() - started, 3),
              "note": note, "saw_max_fan": saw_max, "automatic_after": automatic}
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    assert note["status"] == "done", note["error"]
    assert note["chunk_count"] == note["processed_chunks"] == 2
    assert note["chunk_seconds"] == 1200 and note["chunk_overlap_seconds"] == 120
    assert note["duration"] == 1320 and max(s["end"] for s in note["segments"]) > 1300
    assert saw_max
    print(f"Passed: {len(note['segments'])} segments, {report['seconds']}s; automatic fan: {automatic}", flush=True)
    client.delete(f"/api/notes/{note_id}").raise_for_status()
