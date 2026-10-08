import importlib.util
from pathlib import Path
import sqlite3

import pytest

from app.store import NoteStore


spec = importlib.util.spec_from_file_location(
    "export_migration", Path(__file__).resolve().parents[1] / "scripts/export-migration.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def source_note(tmp_path, status="done"):
    source = tmp_path / "live"
    store = NoteStore(source / "moss-note.sqlite3")
    store.create({"id": "test", "title": "Synthetic meeting", "original_filename": "test.wav",
                  "source_path": str(source / "uploads/test.wav"), "status": status})
    (source / "uploads").mkdir()
    (source / "uploads/test.wav").write_bytes(b"synthetic")
    (source / "timings.sqlite3").write_bytes(b"Orin history")
    return source, store


def test_snapshot_preserves_data_but_not_machine_timings(tmp_path):
    source, store = source_note(tmp_path)
    destination = tmp_path / "snapshot"
    result = module.export_data(source, destination)
    assert result["note_count"] == 1
    assert result["restore_data_dir"] == str(source)
    assert (destination / "uploads/test.wav").read_bytes() == b"synthetic"
    assert not (destination / "timings.sqlite3").exists()
    assert len(result["sha256"]["moss-note.sqlite3"]) == 64
    with sqlite3.connect(destination / "moss-note.sqlite3") as database:
        assert database.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert database.execute("SELECT source_path FROM notes").fetchone()[0] == str(source / "uploads/test.wav")
    assert store.get("test")["title"] == "Synthetic meeting"


@pytest.mark.parametrize("queue", ["asr", "correction"])
def test_snapshot_rejects_pending_jobs(tmp_path, queue):
    source, store = source_note(tmp_path, "queued" if queue == "asr" else "done")
    if queue == "correction":
        store.update("test", correction_status="processing")
    with pytest.raises(ValueError, match="Pending jobs"):
        module.export_data(source, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()


def test_snapshot_does_not_overwrite_existing_export(tmp_path):
    source, _ = source_note(tmp_path)
    destination = tmp_path / "snapshot"
    destination.mkdir()
    with pytest.raises(FileExistsError):
        module.export_data(source, destination)


def test_snapshot_cannot_nest_inside_live_data(tmp_path):
    source, _ = source_note(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        module.export_data(source, source / "snapshot")
