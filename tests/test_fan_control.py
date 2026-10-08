import asyncio
from unittest.mock import AsyncMock

import pytest

import app.main as main
from app.fan_control import FanControl
from app.store import NoteStore


def test_disabled_does_not_run_commands(monkeypatch):
    fan = FanControl()
    command = AsyncMock()
    monkeypatch.setattr(fan, "_command", command)
    asyncio.run(fan.start())
    asyncio.run(fan.stop())
    command.assert_not_awaited()


def test_enabled_starts_and_stops_service(monkeypatch):
    fan = FanControl(True)
    command = AsyncMock()
    monkeypatch.setattr(fan, "_command", command)
    asyncio.run(fan.start())
    asyncio.run(fan.stop())
    assert [call.args for call in command.await_args_list] == [("start",), ("stop",)]


def test_restore_failure_does_not_crash_queue(monkeypatch, caplog):
    fan = FanControl(True)
    monkeypatch.setattr(fan, "_command", AsyncMock(side_effect=RuntimeError("failed")))
    asyncio.run(fan.stop())
    assert "Failed to restore" in caplog.text


@pytest.mark.parametrize("error", [RuntimeError("failure"), asyncio.CancelledError()])
def test_transcription_failure_and_cancellation_restore_fan(tmp_path, monkeypatch, error):
    store = NoteStore(tmp_path / "notes.sqlite3")
    source = tmp_path / "source.wav"
    source.write_bytes(b"test")
    note = store.create({"id": "fan-test", "title": "test", "source_path": str(source),
                         "original_filename": "source.wav", "status": "queued"})
    fan = FanControl(True)
    command = AsyncMock()
    monkeypatch.setattr(fan, "_command", command)
    monkeypatch.setattr(main, "FAN_CONTROL", fan)
    monkeypatch.setattr(main, "STORE", store)
    monkeypatch.setattr(main.MODEL_RUNTIME, "ensure", AsyncMock(side_effect=error))
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(main.transcribe(note["id"]))
    else:
        asyncio.run(main.transcribe(note["id"]))
        assert store.get(note["id"])["status"] == "error"
    assert [call.args for call in command.await_args_list] == [("start",), ("stop",)]
