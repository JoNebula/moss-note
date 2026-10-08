#!/usr/bin/env python3
"""Exercise authenticated upload, real transcription, and subtitle export."""
import json
from pathlib import Path
import time
import httpx
from dotenv import dotenv_values

root = Path(__file__).resolve().parents[1]
env = dotenv_values(root / ".env")
data = Path(env["MOSS_DATA_DIR"])
auth = (env["MOSS_AUTH_USERNAME"], env["MOSS_AUTH_PASSWORD"])
with httpx.Client(base_url="http://127.0.0.1:8000", auth=auth, timeout=60) as client:
    response = client.get("/")
    response.raise_for_status()
    response = client.get("/readyz")
    response.raise_for_status()
    with (data / "jfk.flac").open("rb") as audio:
        response = client.post(
            "/api/notes",
            data={"title": "Deployment smoke test", "language": "en"},
            files={"file": ("jfk.flac", audio, "audio/flac")},
        )
    response.raise_for_status()
    note_id = response.json()["id"]
    print(f"Uploaded smoke test: {note_id}", flush=True)
    for _ in range(300):
        response = client.get(f"/api/notes/{note_id}")
        response.raise_for_status()
        note = response.json()
        if note["status"] == "error":
            raise RuntimeError(note.get("error"))
        if note["status"] == "done":
            assert note["segments"], "Empty transcription"
            text = " ".join(segment["text"] for segment in note["segments"])
            assert "country" in text.lower(), text
            with (data / "smoke-result.json").open("w") as output:
                json.dump(note, output, indent=2, ensure_ascii=False)
            print(text, flush=True)
            response = client.get(f"/api/notes/{note_id}/export/srt")
            response.raise_for_status()
            assert "-->" in response.text
            (data / "smoke-result.srt").write_text(response.text)
            print("Authenticated upload, transcription, and SRT export passed", flush=True)
            break
        time.sleep(2)
    else:
        raise TimeoutError("Transcription did not finish within 10 minutes")
