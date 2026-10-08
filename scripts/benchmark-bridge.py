#!/usr/bin/env python3
"""Compare wider speaker bridges using already measured main-chunk outputs."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time

import httpx


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chunk-seconds", type=int, default=300)
    parser.add_argument("--bridge-seconds", type=int, default=120)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output directory")
    source = json.loads(args.report.read_text())
    base = [r for r in source["runs"] if r["chunk_seconds"] == args.chunk_seconds][-1]
    args.output.mkdir(parents=True)
    os.environ.setdefault("MOSS_DATA_DIR", str(args.output / "benchmark-store"))
    os.environ.setdefault("MOSS_MAX_COMPLETION_TOKENS", "8192")
    from app import main as app

    app.VLLM_URL = "http://127.0.0.1:8001/v1"
    app.MAX_AUDIO_CHUNK_SECONDS = args.chunk_seconds
    app.BRIDGE_AUDIO_SECONDS = args.bridge_seconds
    audio = args.output / "normalized.wav"
    audio.symlink_to(Path(source["audio"]).resolve())
    started = time.perf_counter()
    chunks = app.split_audio(audio, source["duration"])
    bridges = app.make_bridge_chunks(audio, source["duration"], chunks)
    split_seconds = time.perf_counter() - started
    results, clips = [], []
    async with httpx.AsyncClient(timeout=600) as client:
        for path, offset in bridges:
            started = time.perf_counter()
            raw, segments = await app.transcribe_chunk(client, path, {"language": "ko"})
            elapsed = time.perf_counter() - started
            assert segments
            results.append((offset, segments))
            clips.append({"offset": offset, "seconds": elapsed, "raw": raw, "segments": segments})
            print(f"Bridge {args.bridge_seconds}s offset {offset:g}: {elapsed:.3f}s", flush=True)
    main_clips = [clip for clip in base["clips"] if clip["kind"] == "chunk"]
    started = time.perf_counter()
    merged = app.merge_chunk_segments([(c["offset"], c["segments"]) for c in main_clips], results)
    merge_seconds = time.perf_counter() - started
    report = {"reference_chunk_report": str(args.report), "reference_run": base["run"],
        "chunk_seconds": args.chunk_seconds, "bridge_seconds": args.bridge_seconds,
        "split_seconds": split_seconds, "merge_seconds": merge_seconds, "clips": clips,
        "estimated_pipeline_seconds": split_seconds + merge_seconds + sum(c["seconds"] for c in main_clips + clips),
        "segments": merged, "speaker_count": len({s["speaker"] for s in merged})}
    (args.output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Estimated pipeline: {report['estimated_pipeline_seconds']:.3f}s; {report['speaker_count']} speaker IDs", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
