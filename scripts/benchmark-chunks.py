#!/usr/bin/env python3
"""Compare complete chunk/bridge pipelines without changing production notes."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time

import httpx


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[300, 150])
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output directory")
    args.output.mkdir(parents=True)
    os.environ.setdefault("MOSS_DATA_DIR", str(args.output / "benchmark-store"))
    from app import main as app

    app.VLLM_URL = "http://127.0.0.1:8001/v1"
    app.BRIDGE_AUDIO_SECONDS = 60
    duration = app.probe_duration(args.audio)
    report = {"audio": str(args.audio), "duration": duration, "bridge_seconds": 60, "runs": []}
    async with httpx.AsyncClient(timeout=600) as client:
        for run in range(args.runs):
            for size in args.sizes:
                app.MAX_AUDIO_CHUNK_SECONDS = size
                directory = args.output / f"run-{run}-{size}s"
                directory.mkdir()
                audio = directory / "normalized.wav"
                audio.symlink_to(args.audio.resolve())
                started = time.perf_counter()
                chunks = app.split_audio(audio, duration)
                bridges = app.make_bridge_chunks(audio, duration, chunks)
                split_seconds = time.perf_counter() - started
                result = {"chunk_seconds": size, "run": run, "split_seconds": split_seconds, "clips": []}
                chunk_results, bridge_results = [], []
                for kind, clips, results in [("chunk", chunks, chunk_results), ("bridge", bridges, bridge_results)]:
                    for path, offset in clips:
                        started = time.perf_counter()
                        raw, segments = await app.transcribe_chunk(client, path, {"language": "ko"})
                        elapsed = time.perf_counter() - started
                        assert segments, f"No parsed segments: {path}"
                        tokenized = await client.post("http://127.0.0.1:8001/tokenize", json={"model": "moss-mtd", "prompt": raw})
                        tokenized.raise_for_status()
                        result["clips"].append({"kind": kind, "offset": offset, "seconds": elapsed,
                            "output_tokens": tokenized.json()["count"], "raw": raw, "segments": segments})
                        results.append((offset, segments))
                        print(f"{size}s run {run} {kind} offset {offset:g}: {elapsed:.3f}s", flush=True)
                started = time.perf_counter()
                merged = app.merge_chunk_segments(chunk_results, bridge_results)
                result["merge_seconds"] = time.perf_counter() - started
                result["total_seconds"] = split_seconds + result["merge_seconds"] + sum(c["seconds"] for c in result["clips"])
                result["segments"] = merged
                result["speaker_count"] = len({segment["speaker"] for segment in merged})
                report["runs"].append(result)
                (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                print(f"TOTAL {size}s run {run}: {result['total_seconds']:.3f}s, {len(merged)} segments, {result['speaker_count']} speakers", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
