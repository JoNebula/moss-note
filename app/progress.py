from __future__ import annotations

import math
from pathlib import Path
import sqlite3
import statistics
import time


class TimingCache:
    """Persist measured chunk latencies, separately for each model and duration band."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS samples (
                variant TEXT NOT NULL, bucket INTEGER NOT NULL,
                duration REAL NOT NULL, seconds REAL NOT NULL, recorded_at REAL NOT NULL
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS sample_lookup ON samples(variant, bucket, recorded_at)")

    @staticmethod
    def bucket(duration: float) -> int:
        return next((limit for limit in (60, 300, 600, 1200, 5400) if duration <= limit), 5400)

    def record(self, variant: str, duration: float, seconds: float) -> None:
        if not all(math.isfinite(value) and value > 0 for value in (duration, seconds)):
            return
        with sqlite3.connect(self.path) as connection:
            connection.execute("INSERT INTO samples VALUES (?, ?, ?, ?, ?)",
                               (variant, self.bucket(duration), duration, seconds, time.time()))
            connection.execute("""DELETE FROM samples WHERE variant = ? AND bucket = ? AND rowid NOT IN (
                SELECT rowid FROM samples WHERE variant = ? AND bucket = ?
                ORDER BY recorded_at DESC, rowid DESC LIMIT 40)""",
                               (variant, self.bucket(duration), variant, self.bucket(duration)))

    def estimate(self, variant: str, duration: float) -> tuple[float, int]:
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute("SELECT duration, seconds FROM samples WHERE variant = ? AND bucket = ?",
                                      (variant, self.bucket(duration))).fetchall()
        if rows:
            rate = statistics.median(seconds / audio for audio, seconds in rows)
            return max(1.0, duration * rate), len(rows)
        rate = {"bf16": 0.15, "rtn-w8": 0.12, "rtn-w4": 0.1}.get(variant, 0.15)
        if duration > 600:
            rate *= 2
        return max(1.0, duration * rate), 0


def progress_state(note: dict, cache: TimingCache, now: float | None = None) -> dict:
    if note["status"] == "error":
        return {"phase": "error", "percent": None, "remaining_seconds": None, "estimated": False}
    if note["status"] == "done":
        return {"phase": "done", "percent": 100, "remaining_seconds": 0, "estimated": False}
    state = note.get("processing") or {}
    durations = state.get("chunk_durations", [])
    phase = state.get("phase", note["status"])
    if note["status"] == "queued" or not durations:
        return {"phase": note["status"] if note["status"] == "queued" else phase,
                "percent": None, "remaining_seconds": None, "estimated": True}
    estimates = [cache.estimate(note.get("model_variant", "bf16"), duration) for duration in durations]
    expected = [estimate for estimate, _ in estimates]
    completed = min(note.get("processed_chunks", 0), len(expected))
    partial = 0.0
    current_remaining = 0.0
    if phase == "transcribing" and completed < len(expected):
        elapsed = max(0, (time.time() if now is None else now) - state.get("phase_started_at", 0))
        # Never show a completed current chunk based only on an estimate.
        partial = min(elapsed, expected[completed] * 0.9)
        current_remaining = max(expected[completed] - elapsed, expected[completed] * 0.1)
    remaining = current_remaining + sum(expected[completed + 1:]) if completed < len(expected) else 1
    percent = min(99, (sum(expected[:completed]) + partial) / sum(expected) * 100)
    return {"phase": phase, "percent": round(percent, 1), "remaining_seconds": round(remaining),
            "estimated": True, "historical_samples": sum(count for _, count in estimates),
            "chunk_index": min(completed + 1, len(expected)), "chunk_count": len(expected)}
