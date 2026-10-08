"""Exercise the live serial queue and cooling lifecycle with an existing sample."""
import json
from datetime import UTC, datetime
from pathlib import Path
import subprocess
import time

import httpx
from dotenv import dotenv_values


configuration = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
OUTPUT = Path(configuration["MOSS_DATA_DIR"]) / "diagnostics" / (
    datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-model-choice"
)
AUDIO = Path(configuration["MOSS_DATA_DIR"]) / "jfk.flac"
OUTPUT.mkdir(parents=True, exist_ok=False)
results = []
auth = (configuration["MOSS_AUTH_USERNAME"], configuration["MOSS_AUTH_PASSWORD"])


def fan_state():
    active = subprocess.run(["systemctl", "is-active", "moss-note-fan"],
                            capture_output=True, text=True).stdout.strip()
    pwm = [p.read_text().strip() for p in Path("/sys/class/hwmon").glob("hwmon*/pwm1")]
    return {"service": active, "pwm": pwm}


with httpx.Client(base_url="http://127.0.0.1:8000", auth=auth, timeout=30) as client:
    initial_variant = client.get("/api/health").json()["model_runtime"]["active"]
    for variant in ("bf16", "rtn-w8", "rtn-w4", initial_variant):
        with AUDIO.open("rb") as audio:
            response = client.post("/api/notes", data={"title": f"Model choice smoke {variant}",
                                    "model_variant": variant}, files={"file": ("smoke.flac", audio)})
        response.raise_for_status()
        note_id = response.json()["id"]
        print(f"Started {variant}: {note_id}", flush=True)
        started = time.monotonic()
        saw_max = False
        observed_roots = set()
        while time.monotonic() - started < 300:
            note = client.get(f"/api/notes/{note_id}").json()
            fan = fan_state()
            saw_max |= fan["service"] == "active" and bool(fan["pwm"]) and all(pwm == "255" for pwm in fan["pwm"])
            if note["status"] == "processing":
                try:
                    model_response = httpx.get("http://127.0.0.1:8001/v1/models", timeout=0.5)
                    observed_roots.update(model["root"] for model in model_response.json()["data"])
                except (httpx.HTTPError, ValueError, KeyError):
                    pass
            if note["status"] in {"done", "error"}:
                break
            time.sleep(0.15)
        else:
            raise RuntimeError(f"Timed out: {note_id}")
        # Another user's next job may start immediately after this one.
        busy = any(n["status"] in {"queued", "processing"} for n in client.get("/api/notes").json())
        for _ in range(100):
            fan = fan_state()
            if fan["service"] == "inactive" or busy:
                break
            time.sleep(0.1)
        health = client.get("/api/health").json()
        automatic = subprocess.run(["systemctl", "is-active", "nvfancontrol"],
                                   capture_output=True, text=True).stdout.strip()
        result = {"variant": variant, "note": note, "elapsed": round(time.monotonic() - started, 3),
                  "active_variant": health["model_runtime"]["active"], "saw_max_fan": saw_max,
                  "fan_after": fan, "automatic_after": automatic,
                  "other_jobs_active": busy, "observed_roots": sorted(observed_roots),
                  "chunk_seconds": health["limits"]["chunk_seconds"]}
        results.append(result)
        (OUTPUT / "live-results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
        assert note["status"] == "done", note.get("error")
        catalogue = json.loads(Path(configuration["MOSS_MODEL_VARIANTS_FILE"]).read_text())
        assert note["model_variant"] == variant and catalogue[variant]["path"] in observed_roots
        assert note["segments"] and saw_max and result["chunk_seconds"] == 1200
        if not busy:
            assert automatic == "active" and fan["service"] == "inactive"
        print(f"Passed {variant}, {result['elapsed']}s; max fan observed; other jobs active: {busy}", flush=True)
        client.delete(f"/api/notes/{note_id}").raise_for_status()
        if busy:
            print("Stopping smoke sequence without interrupting user jobs", flush=True)
            break
