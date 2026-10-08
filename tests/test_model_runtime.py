from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest

import app.main as main
import app.model_runtime as runtime_module
from app.model_runtime import ModelRuntime
from app.store import NoteStore


@pytest.fixture
def runtime(tmp_path):
    variants = {}
    for key in ("bf16", "rtn-w8", "rtn-w4"):
        directory = tmp_path / key
        directory.mkdir()
        (directory / "config.json").write_text("{}")
        variants[key] = {"label": key, "path": str(directory)}
    return ModelRuntime(variants, tmp_path / "selected.json", "http://127.0.0.1:8001/v1", True)


def test_unknown_and_missing_variants_rejected(runtime):
    with pytest.raises(ValueError, match="Unknown"):
        runtime.validate("../../another-model")
    Path(runtime.variants["rtn-w4"]["path"], "config.json").unlink()
    with pytest.raises(ValueError, match="not installed"):
        runtime.validate("rtn-w4")
    assert not runtime.public_state("bf16", True)["options"][2]["available"]


def test_managed_catalogue_rejects_paths_outside_model_storage(tmp_path, monkeypatch):
    catalogue = tmp_path / "variants.json"
    catalogue.write_text(json.dumps({key: {"path": "/tmp/model", "label": key} for key in ("bf16", "rtn-w8", "rtn-w4")}))
    monkeypatch.setenv("MOSS_MANAGED_MODELS", "true")
    monkeypatch.setenv("MOSS_MODEL_VARIANTS_FILE", str(catalogue))
    monkeypatch.setenv("MOSS_MODEL_SELECTION_FILE", str(tmp_path / "selected.json"))
    with pytest.raises(ValueError, match="/data/models"):
        ModelRuntime.from_env()


def test_explicit_model_storage_root_still_rejects_escape(tmp_path, monkeypatch):
    root = tmp_path / "models"
    root.mkdir()
    catalogue = tmp_path / "variants.json"
    variants = {key: {"path": str(root / key / "v1"), "label": key}
                for key in ("bf16", "rtn-w8", "rtn-w4")}
    catalogue.write_text(json.dumps(variants))
    monkeypatch.setenv("MOSS_MANAGED_MODELS", "true")
    monkeypatch.setenv("MOSS_MODELS_DIR", str(root))
    monkeypatch.setenv("MOSS_MODEL_VARIANTS_FILE", str(catalogue))
    monkeypatch.setenv("MOSS_MODEL_SELECTION_FILE", str(tmp_path / "selected.json"))
    assert ModelRuntime.from_env().variants == variants
    variants["rtn-w4"]["path"] = str(root / ".." / "outside")
    catalogue.write_text(json.dumps(variants))
    with pytest.raises(ValueError, match="pinned"):
        ModelRuntime.from_env()
    monkeypatch.setenv("MOSS_MODELS_DIR", "models")
    with pytest.raises(ValueError, match="absolute"):
        ModelRuntime.from_env()


def test_same_model_does_not_restart(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "active_variant", AsyncMock(return_value="bf16"))
    restart = AsyncMock()
    monkeypatch.setattr(runtime, "_restart", restart)
    asyncio.run(runtime.ensure("bf16"))
    restart.assert_not_awaited()
    assert runtime.selected_variant() == "bf16"
    assert runtime.selection_file.stat().st_mode & 0o777 == 0o640


def test_switch_persists_selected_precision_and_waits_for_it(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "active_variant", AsyncMock(return_value="bf16"))
    restart, ready = AsyncMock(), AsyncMock()
    monkeypatch.setattr(runtime, "_restart", restart)
    monkeypatch.setattr(runtime, "_wait_ready", ready)
    asyncio.run(runtime.ensure("rtn-w8"))
    restart.assert_awaited_once()
    ready.assert_awaited_once_with("rtn-w8")
    assert runtime.selected_variant() == "rtn-w8"
    assert runtime.target is None


def test_failed_switch_rolls_back_and_still_fails_requested_job(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "active_variant", AsyncMock(return_value="bf16"))
    restart = AsyncMock()
    ready = AsyncMock(side_effect=[RuntimeError("boot failed"), None])
    monkeypatch.setattr(runtime, "_restart", restart)
    monkeypatch.setattr(runtime, "_wait_ready", ready)
    with pytest.raises(RuntimeError, match="boot failed"):
        asyncio.run(runtime.ensure("rtn-w4"))
    assert runtime.selected_variant() == "bf16"
    assert restart.await_count == 2
    assert runtime.target is None
    assert runtime.error == "boot failed"


def test_wrong_root_never_counts_as_ready(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "active_variant", AsyncMock(return_value="bf16"))
    monkeypatch.setattr(runtime_module, "time", SimpleNamespace(monotonic=iter([0, 0, 181]).__next__))
    monkeypatch.setattr(runtime_module.asyncio, "sleep", AsyncMock())
    with pytest.raises(RuntimeError, match="did not become ready"):
        asyncio.run(runtime._wait_ready("rtn-w8"))


def test_upload_records_variant_and_rejects_arbitrary_models(runtime, tmp_path, monkeypatch):
    async def no_transcription(_):
        pass

    monkeypatch.setattr(main, "MODEL_RUNTIME", runtime)
    monkeypatch.setattr(main, "STORE", NoteStore(tmp_path / "notes.sqlite3"))
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(main, "QUEUE", asyncio.Queue())
    monkeypatch.setattr(main, "CORRECTION_QUEUE", asyncio.Queue())
    monkeypatch.setattr(main, "transcribe", no_transcription)
    monkeypatch.setattr(main, "AUTH_USERNAME", "admin")
    monkeypatch.setattr(main, "AUTH_PASSWORD", "admin")
    with TestClient(main.app) as client:
        bad = client.post("/api/notes", auth=("admin", "admin"),
                          data={"model_variant": "/data/models/unapproved"}, files={"file": ("test.wav", b"test")})
        assert bad.status_code == 400
        assert main.STORE.list() == []
        assert list(main.UPLOAD_DIR.iterdir()) == []
        response = client.post("/api/notes", auth=("admin", "admin"),
                               data={"model_variant": "rtn-w4"}, files={"file": ("test.wav", b"test")})
        assert response.status_code == 202
        note = main.STORE.get(response.json()["id"])
        assert note["model_variant"] == "rtn-w4"
        assert Path(note["source_path"]).read_bytes() == b"test"


def test_unmanaged_runtime_only_supports_default_model(monkeypatch):
    monkeypatch.delenv("MOSS_MANAGED_MODELS", raising=False)
    runtime = ModelRuntime.from_env()
    asyncio.run(runtime.ensure("bf16"))
    with pytest.raises(ValueError):
        asyncio.run(runtime.ensure("rtn-w8"))
