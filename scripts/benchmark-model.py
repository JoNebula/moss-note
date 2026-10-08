#!/usr/bin/env python3
"""Compare real transcription latency without modifying application notes."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import time
import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--label", required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--runs", type=int, default=2)
parser.add_argument("audio", type=Path, nargs="+")
args = parser.parse_args()
prompt = "请将音频转写为文本，每一段需以起始时间戳和说话人编号（[S01]、[S02]、[S03]…）开头，正文为对应的语音内容，并在段末标注结束时间戳，以清晰标明该段语音范围。"
report = {"label": args.label, "cases": []}
with httpx.Client(base_url="http://127.0.0.1:8001", timeout=600) as client:
    deadline = time.monotonic() + 180
    while True:
        try:
            client.get("/health", timeout=5).raise_for_status()
            break
        except httpx.HTTPError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)
    for path in args.audio:
        case = {"audio": str(path), "runs": []}
        for index in range(args.runs + 1):
            with path.open("rb") as audio:
                started = time.perf_counter()
                response = client.post(
                    "/v1/audio/transcriptions",
                    data={"model": "moss-mtd", "response_format": "json", "temperature": "0", "max_completion_tokens": "8192", "prompt": prompt},
                    files={"file": (path.name, audio, "audio/wav")},
                )
                elapsed = time.perf_counter() - started
            response.raise_for_status()
            payload = response.json()
            text = payload["text"]
            tokenized = client.post("/tokenize", json={"model": "moss-mtd", "prompt": text})
            tokenized.raise_for_status()
            count = tokenized.json()["count"]
            assert text.strip(), "Empty transcript"
            result = {"seconds": elapsed, "output_tokens": count, "tokens_per_second": count / elapsed, "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "response": payload}
            print(f"{args.label} {path.name} run {index}: {elapsed:.3f}s, {count} tokens, {count/elapsed:.1f} tokens/s", flush=True)
            if index:
                case["runs"].append(result)
            else:
                case["warmup"] = result
        case["median_seconds"] = statistics.median(x["seconds"] for x in case["runs"])
        report["cases"].append(case)
args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(f"Saved benchmark: {args.output}", flush=True)
