"""Compare bounded synthetic uploads without saving files or starting ASR."""
import argparse
import json
from pathlib import Path
import statistics
import time

import httpx
from dotenv import dotenv_values


parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--mib", type=int, default=40)
parser.add_argument("--runs", type=int, default=3)
args = parser.parse_args()
configuration = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
auth = (configuration["MOSS_AUTH_USERNAME"], configuration["MOSS_AUTH_PASSWORD"])
payload = b"x" * (args.mib * 1024**2)
report = {"size_bytes": len(payload), "client": "Jetson itself; not the user's browser network", "routes": []}
for base in ("http://127.0.0.1:8000", "https://tts.seongwoonjo.com"):
    route = {"url": base, "runs": []}
    with httpx.Client(base_url=base, auth=auth, timeout=180) as client:
        client.get("/healthz").raise_for_status()
        for index in range(args.runs):
            started = time.monotonic()
            response = client.post("/api/diagnostics/upload", content=payload,
                                   headers={"Content-Type": "application/octet-stream"})
            elapsed = time.monotonic() - started
            response.raise_for_status()
            measurement = {"seconds": round(elapsed, 4), "mb_per_second": round(len(payload) / 1e6 / elapsed, 3),
                           "server": response.json(), "cf_ray": response.headers.get("cf-ray"),
                           "http_version": response.http_version}
            assert measurement["server"]["bytes"] == len(payload)
            route["runs"].append(measurement)
            print(f"{base} run {index}: {elapsed:.3f}s, {measurement['mb_per_second']} MB/s", flush=True)
    route["median_seconds"] = statistics.median(result["seconds"] for result in route["runs"])
    report["routes"].append(route)
    args.output.write_text(json.dumps(report, indent=2))
