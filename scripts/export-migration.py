#!/usr/bin/env python3
"""Export an offline note snapshot; never include login or tunnel credentials."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def export_data(source: Path, destination: Path) -> dict:
    source, destination = source.resolve(), destination.resolve()
    if source == destination or destination.is_relative_to(source):
        raise ValueError("The snapshot must be outside the live data directory")
    database = source / "moss-note.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        busy = connection.execute(
            "SELECT COUNT(*) FROM notes WHERE status IN ('queued', 'processing') "
            "OR correction_status IN ('queued', 'processing')"
        ).fetchone()[0]
        if busy:
            raise ValueError("Pending jobs exist; finish them before migration")
        note_count = connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        destination.mkdir(mode=0o2770, parents=True, exist_ok=False)
        with sqlite3.connect(destination / "moss-note.sqlite3") as backup:
            connection.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("SQLite backup integrity check failed")
    # Timings belong to Orin. Thor starts its own duration/model timing history.
    ignored = shutil.ignore_patterns("moss-note.sqlite3*", "timings.sqlite3*")
    shutil.copytree(source, destination, dirs_exist_ok=True, ignore=ignored)
    hashes = {}
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            with path.open("rb") as file:
                hashes[str(path.relative_to(destination))] = hashlib.file_digest(file, "sha256").hexdigest()
    return {"restore_data_dir": str(source), "note_count": note_count, "sha256": hashes}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    subprocess.run(["findmnt", "--mountpoint", "/data"], check=True, stdout=subprocess.DEVNULL)
    usage = shutil.disk_usage("/data")
    if usage.free / usage.total < 0.10:
        parser.error("/data has less than 10% free space")
    output = args.output or Path("/data/artifacts/moss-note") / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-thor-migration"
    )
    if not all(path.resolve().is_relative_to("/data") for path in (args.data_dir, output)):
        parser.error("Source and snapshot must both be on /data")
    state = subprocess.run(["systemctl", "is-active", "moss-note-app"], capture_output=True, text=True)
    if state.stdout.strip() not in {"inactive", "failed"}:
        parser.error("Stop moss-note-tunnel and moss-note-app first; no live export is allowed")
    output.mkdir(mode=0o2770, parents=True, exist_ok=False)
    manifest = export_data(args.data_dir, output / "data")
    manifest["source_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    manifest["models"] = json.loads((ROOT / "deploy/model-variants.json").read_text())
    manifest["excluded"] = ["credentials", "Codex login", "Orin timing history", "Python environments", "model weights"]
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Snapshot: {output}; {manifest['note_count']} notes; SHA256 and SQLite verified")


if __name__ == "__main__":
    main()
