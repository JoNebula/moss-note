import importlib.util
import json
import configparser
import shutil
from pathlib import Path
import sqlite3

import pytest

from app.store import NoteStore
from dotenv import dotenv_values


spec = importlib.util.spec_from_file_location(
    "export_migration", Path(__file__).resolve().parents[1] / "scripts/export-migration.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
restore_spec = importlib.util.spec_from_file_location(
    "restore_migration", Path(__file__).resolve().parents[1] / "scripts/restore-migration.py"
)
restore = importlib.util.module_from_spec(restore_spec)
restore_spec.loader.exec_module(restore)


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


def snapshot_note(tmp_path):
    source, _ = source_note(tmp_path)
    snapshot = tmp_path / "snapshot"
    manifest = module.export_data(source, snapshot / "data")
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    return source, snapshot


def test_restore_rebases_only_audio_paths_and_preserves_source(tmp_path):
    source, snapshot = snapshot_note(tmp_path)
    target = tmp_path / "thor/data"
    result = restore.restore_data(snapshot, target)
    assert result["note_count"] == 1
    assert NoteStore(target / "moss-note.sqlite3").get("test")["source_path"] == str(target / "uploads/test.wav")
    assert NoteStore(source / "moss-note.sqlite3").get("test")["source_path"] == str(source / "uploads/test.wav")
    assert NoteStore(snapshot / "data/moss-note.sqlite3").get("test")["source_path"] == str(source / "uploads/test.wav")


def test_restore_refuses_corruption_or_overwrite(tmp_path):
    _, snapshot = snapshot_note(tmp_path)
    target = tmp_path / "thor/data"
    (snapshot / "data/uploads/test.wav").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        restore.restore_data(snapshot, target)
    assert not target.exists()
    target.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        restore.restore_data(snapshot, target)


def test_restore_refuses_manifest_path_escape(tmp_path):
    _, snapshot = snapshot_note(tmp_path)
    manifest = json.loads((snapshot / "manifest.json").read_text())
    manifest["sha256"] = {"../../outside": "invalid"}
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="escapes"):
        restore.verify_snapshot(snapshot)


def test_local_configuration_keeps_secrets_private_and_rebases_units(tmp_path):
    root = tmp_path / "thor"
    deploy = root / "deploy"
    deploy.mkdir(parents=True)
    original = Path(__file__).resolve().parents[1] / "deploy"
    for filename in ["model-variants.json", *[f"moss-note-{name}.service" for name in ("app", "model", "fan", "tunnel")]]:
        shutil.copy2(original / filename, deploy / filename)
    source = tmp_path / "private.env"
    source.write_text("MOSS_AUTH_USERNAME=lab\nMOSS_AUTH_PASSWORD='test secret'\nMOSS_REQUIRE_QWEN=true\n")
    data = root / "data/v1"
    restore.configure(root, data, source, True)
    env = dotenv_values(root / ".env")
    assert env["MOSS_AUTH_PASSWORD"] == "test secret"
    assert env["MOSS_REQUIRE_QWEN"] == "false"
    assert env["MOSS_MODELS_DIR"] == str(root / "models")
    assert env["MOSS_DATA_DIR"] == str(data)
    assert (root / ".env").stat().st_mode & 0o777 == 0o600
    assert all(Path(entry["path"]).is_relative_to(root / "models") for entry in
               json.loads((deploy / "model-variants.local.json").read_text()).values())
    unit = configparser.ConfigParser(interpolation=None)
    unit.read(deploy / "local/moss-note-app.service")
    assert unit["Service"]["WorkingDirectory"] == str(root)
    assert unit["Service"]["ExecStart"] == str(root / "scripts/run-app.sh")
    assert "NOPASSWD: ALL" not in (deploy / "local/moss-note.sudoers").read_text()
    with pytest.raises(FileExistsError):
        restore.configure(root, data, source, True)
