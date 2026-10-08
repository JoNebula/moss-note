import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.main as main
import app.codex_postprocess as codex
from app.store import NoteStore


def source():
    return [{"id": 0, "start": 0.0, "end": 10.0, "speaker": "S01", "text": "레디스 캐시를 씁니다."},
            {"id": 1, "start": 10.0, "end": 20.0, "speaker": "S02", "text": "금요일에 테스트합니다."}]


def summary():
    return {"title": "캐시 검토", "overview": "캐시 구성과 테스트 일정을 논의했다.",
            "key_points": [{"text": "Redis 캐시를 사용한다.", "segment_ids": [0]}],
            "decisions": [], "action_items": [{"text": "금요일에 테스트한다.", "segment_ids": [1]}],
            "open_questions": []}


def test_cli_is_pinned_and_has_no_tools(tmp_path):
    command = codex.CodexPostprocessor("/usr/bin/codex").command(tmp_path)
    assert command[:2] == ["/usr/bin/codex", "exec"]
    assert command[command.index("--model") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="medium"' in command
    assert 'service_tier="priority"' in command
    assert ["--enable", "fast_mode"] in [command[index:index + 2] for index in range(len(command))]
    assert 'forced_login_method="chatgpt"' in command
    assert "--ignore-user-config" in command and "--ignore-rules" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[-1] == "-"
    assert any(item.startswith("model_instructions_file=") for item in command)
    for tool in codex.NO_TOOLS:
        assert any(command[index:index + 2] == ["--disable", tool] for index in range(len(command)))


def test_run_collects_usage_without_passing_server_secrets(monkeypatch, tmp_path):
    captured = {}

    class Process:
        returncode = 0

        async def communicate(self):
            captured["prompt"] = captured["stdin"].read()
            (captured["cwd"] / "result.json").write_text(json.dumps(summary()))
            return b'{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":7,"cached_input_tokens":4}}\n', b""

    async def spawn(*args, **kwargs):
        captured.update(kwargs)
        return Process()

    monkeypatch.setenv("MOSS_AUTH_PASSWORD", "private")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-use")
    monkeypatch.setenv("CODEX_API_KEY", "do-not-use")
    monkeypatch.setattr(codex.asyncio, "create_subprocess_exec", spawn)
    result, usage = asyncio.run(codex.CodexPostprocessor("/fake/codex").run("data only", {}, tmp_path / "job"))
    assert result == summary()
    assert usage == {"input_tokens": 12, "output_tokens": 7, "cached_input_tokens": 4}
    assert captured["prompt"] == b"data only"
    assert captured["start_new_session"] is True
    assert not any(key.startswith(("MOSS_", "QWEN_")) for key in captured["env"])
    assert "OPENAI_API_KEY" not in captured["env"] and "CODEX_API_KEY" not in captured["env"]
    assert captured["env"]["TMPDIR"].startswith("/data/")
    assert json.loads((tmp_path / "job" / "manifest.json").read_text())["reasoning_effort"] == "medium"
    assert json.loads((tmp_path / "job" / "manifest.json").read_text())["requested_service_tier"] == "priority"


def test_timeout_terminates_real_child_process(monkeypatch, tmp_path):
    worker = codex.CodexPostprocessor("/usr/bin/sleep", timeout=0.05)
    monkeypatch.setattr(worker, "command", lambda directory: ["/usr/bin/sleep", "60"])
    original = codex.asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(codex.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(TimeoutError):
        asyncio.run(worker.run("", {}, tmp_path / "timeout"))
    assert processes[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)


def test_cancel_terminates_real_child_process(monkeypatch, tmp_path):
    worker = codex.CodexPostprocessor("/usr/bin/sleep")
    monkeypatch.setattr(worker, "command", lambda directory: ["/usr/bin/sleep", "60"])
    original = codex.asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(codex.asyncio, "create_subprocess_exec", spawn)

    async def check():
        task = asyncio.create_task(worker.run("", {}, tmp_path / "cancel"))
        while not processes:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(check())
    assert processes[0].returncode is not None


@pytest.mark.parametrize("evidence", [[999], [True], [], ["0"]])
def test_summary_requires_real_segment_ids(evidence):
    result = summary()
    result["key_points"][0]["segment_ids"] = evidence
    with pytest.raises(ValueError):
        codex.validate_summary(result, source())


def test_summary_export_omits_sources_and_timestamps():
    result = codex.validate_summary(summary(), source())
    exported = codex.summary_markdown(result)
    assert "# 캐시 검토" in exported
    assert "- Redis 캐시를 사용한다.\n" in exported
    assert "- 금요일에 테스트한다.\n" in exported
    assert "00:00:" not in exported and "segment_ids" not in exported
    assert "미해결 질문" not in exported


@pytest.fixture
def note(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "STORE", NoteStore(tmp_path / "notes.sqlite3"))
    monkeypatch.setattr(main, "CORRECTION_QUEUE", asyncio.Queue())
    main.STORE.create({"id": "test", "title": "회의록", "original_filename": "test.wav",
                       "source_path": str(tmp_path / "test.wav"), "status": "done", "media_type": "audio/wav",
                       "language": "ko", "hotwords": "Redis"})
    main.STORE.update("test", segments=source(), raw_segments=source(), speaker_names={"S01": "A", "S02": "B"})
    worker = codex.CodexPostprocessor("/fake/codex")
    monkeypatch.setattr(worker, "available", lambda: True)
    corrected = [{**item, "text": "Redis 캐시를 씁니다." if item["id"] == 0 else item["text"]} for item in source()]
    worker.process = AsyncMock(return_value=(corrected, summary(), {"input_tokens": 60, "output_tokens": 10}))
    monkeypatch.setattr(main, "CODEX", worker)
    return worker


def test_end_to_end_snapshot_correction_summary_and_downloads(note):
    async def check():
        queued = await main.start_postprocess("test")
        assert queued["correction_model"] == "gpt-6.1-sol"
        assert queued["correction_service_tier"] == "priority"
        assert queued["correction_source_segments"] == source()
        main.STORE.update("test", segments=[{**item, "text": "나중에 편집한 텍스트"} for item in source()])
        await main.postprocess_transcript("test")

    asyncio.run(check())
    result = main.STORE.get("test")
    assert result["correction_status"] == result["correction_phase"] == "done"
    assert result["correction_mode"] == "whole-file"
    assert result["correction_total_windows"] == result["correction_processed_windows"] == 1
    assert note.process.await_count == 1
    assert note.process.await_args.args[0]["segments"] == source()
    assert result["correction_changes"] == 1
    assert result["correction_usage"] == {"input_tokens": 60, "output_tokens": 10}
    assert result["summary"] == summary()
    assert result["raw_segments"] == source()
    assert result["segments"][0]["text"] == "나중에 편집한 텍스트"
    assert result["corrected_segments"][0] == {**source()[0], "text": "Redis 캐시를 씁니다."}
    assert result["correction_partial"] == {}
    assert "correction_artifacts_path" not in main.public_note(result)
    assert (Path(result["correction_artifacts_path"]) / "summary.md").exists()
    assert "Redis" in main.export_note("test", "txt", "corrected").body.decode()
    response = main.export_summary("test", "md")
    assert "캐시 검토" in response.body.decode()
    assert "filename=summary.md" in response.headers["content-disposition"]


def test_whole_file_failure_publishes_nothing_and_retries_once(note):
    note.process.side_effect = RuntimeError("temporary failure")

    async def check():
        await main.start_postprocess("test")
        await main.postprocess_transcript("test")
        failed = main.STORE.get("test")
        assert failed["correction_status"] == "error"
        assert failed["correction_phase"] == "whole-file"
        assert failed["correction_processed_windows"] == 0
        assert failed["correction_partial"] == {}
        assert failed["corrected_segments"] == []
        assert failed["summary"] == {}
        with pytest.raises(HTTPException) as error:
            main.export_summary("test", "md")
        assert error.value.status_code == 409
        note.process.side_effect = None
        await main.start_postprocess("test")
        await main.postprocess_transcript("test")

    asyncio.run(check())
    assert note.process.await_count == 2
    assert main.STORE.get("test")["correction_status"] == "done"
    assert main.STORE.get("test")["correction_usage"]["input_tokens"] == 60


def test_old_window_checkpoints_are_not_used(note):
    main.STORE.update("test", correction_status="error", correction_mode="windows",
                     correction_backend="codex-cli", correction_model=codex.MODEL,
                     correction_partial={0: "old correction"}, correction_total_windows=2,
                     correction_processed_windows=1)

    async def check():
        await main.start_postprocess("test")
        await main.postprocess_transcript("test")

    asyncio.run(check())
    assert note.process.await_count == 1
    assert note.process.await_args.args[0]["segments"] == source()
    assert main.STORE.get("test")["correction_status"] == "done"


def test_start_rejects_duplicate_unfinished_and_missing_cli(note):
    async def check():
        await main.start_postprocess("test")
        with pytest.raises(HTTPException) as error:
            await main.start_postprocess("test")
        assert error.value.status_code == 409
        main.STORE.update("test", correction_status="idle", status="processing")
        with pytest.raises(HTTPException) as error:
            await main.start_postprocess("test")
        assert error.value.status_code == 409
        main.STORE.update("test", status="done")
        note.available = lambda: False
        with pytest.raises(HTTPException) as error:
            await main.start_postprocess("test")
        assert error.value.status_code == 503

    asyncio.run(check())


def test_corrected_edits_are_blocked_during_summary(note):
    main.STORE.update("test", correction_status="processing", correction_phase="summarizing", corrected_segments=source())
    with pytest.raises(HTTPException) as error:
        main.patch_note("test", main.NotePatch(corrected_segments=source()))
    assert error.value.status_code == 409


def test_entire_transcript_is_one_request_not_sliding_windows(tmp_path):
    segments = [{"id": index, "start": index * 10.0, "end": index * 10.0 + 10,
                 "speaker": "S01", "text": "전사 원문입니다."} for index in range(321)]
    worker = codex.CodexPostprocessor("/fake/codex")
    output = {"segments": [{"id": item["id"], "text": item["text"]} for item in segments], "summary": summary()}
    worker.run = AsyncMock(return_value=(output, {}))
    corrected, result, _ = asyncio.run(worker.process({"title": "회의", "segments": segments}, tmp_path / "whole"))
    assert worker.run.await_count == 1
    call = worker.run.await_args
    assert len(call.kwargs["document"]["segments"]) == 321
    assert call.kwargs["document"]["target_ids"] == list(range(321))
    assert call.args[1]["properties"]["segments"]["minItems"] == 0
    assert call.args[1]["properties"]["segments"]["maxItems"] == 321
    assert corrected == segments and result == summary()


def test_sparse_changes_restore_complete_file_with_unchanged_metadata(tmp_path):
    worker = codex.CodexPostprocessor("/fake/codex")
    worker.run = AsyncMock(return_value=({"segments": [{"id": 0, "text": "Redis 캐시를 씁니다."}], "summary": summary()}, {}))
    corrected, _, _ = asyncio.run(worker.process({"segments": source()}, tmp_path / "changes"))
    assert len(corrected) == 2 and corrected[1] == source()[1]
    assert corrected[0] == {**source()[0], "text": "Redis 캐시를 씁니다."}
    assert worker.run.await_count == 1


def test_empty_changes_restore_entire_original(tmp_path):
    worker = codex.CodexPostprocessor("/fake/codex")
    worker.run = AsyncMock(return_value=({"segments": [], "summary": summary()}, {}))
    corrected, _, _ = asyncio.run(worker.process({"segments": source()}, tmp_path / "changes"))
    assert corrected == source()


@pytest.mark.parametrize("edits", [[{"id": 99, "text": "외부 ID"}], [{"id": True, "text": "boolean ID"}],
                                  [{"id": 0, "text": "원문"}, {"id": 0, "text": "중복 ID"}],
                                  [{"id": 0, "text": "원문", "speaker": "S99"}], "not a list"])
def test_sparse_changes_reject_invalid_identity(tmp_path, edits):
    worker = codex.CodexPostprocessor("/fake/codex")
    worker.run = AsyncMock(return_value=({"segments": edits, "summary": summary()}, {}))
    with pytest.raises(ValueError):
        asyncio.run(worker.process({"segments": source()}, tmp_path / "changes"))


def test_catalog_uses_exact_authenticated_model_and_handles_bad_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    worker = codex.CodexPostprocessor("/fake/codex", use_model_catalog=True)
    directory = tmp_path / "job"
    directory.mkdir()
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [{"slug": "other-model"}, {"slug": codex.MODEL, "service_tiers": [{"id": "priority"}]}]}))
    worker.prepare_catalog(directory)
    assert json.loads((directory / "model-catalog.json").read_text())["models"] == [{"slug": codex.MODEL, "service_tiers": [{"id": "priority"}]}]
    assert any(item.startswith("model_catalog_json=") for item in worker.command(directory))
    other = tmp_path / "bad-cache"
    other.mkdir()
    (tmp_path / "models_cache.json").write_text("broken JSON")
    worker.prepare_catalog(other)
    assert not (other / "model-catalog.json").exists()


def test_run_compacts_wire_input_but_archives_full_file(tmp_path, monkeypatch):
    captured = {}

    class Process:
        returncode = 0

        async def communicate(self):
            captured["request"] = captured["stdin"].read().decode()
            (captured["cwd"] / "result.json").write_text('{}')
            return b'', b''

    async def spawn(*args, **kwargs):
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(codex.asyncio, "create_subprocess_exec", spawn)
    document = {"title": "회의", "segments": source(), "target_ids": [0, 1]}
    asyncio.run(codex.CodexPostprocessor("/fake/codex", compact_input=True).run("instructions", {}, tmp_path / "job", document=document))
    assert json.loads((tmp_path / "job" / "input-transcript.json").read_text()) == document
    wire = json.loads(captured["request"].split("\n")[-1])
    assert len(wire["segments"]) == 2
    assert wire["segments"][0] == {"id": 0, "speaker": "S01", "text": source()[0]["text"]}
    assert "target_ids" not in wire


def test_whole_file_validation_rejects_protected_number_change(tmp_path):
    segments = [{**source()[0], "text": "Redis 1.0 버전입니다."}]
    worker = codex.CodexPostprocessor("/fake/codex")
    worker.run = AsyncMock(return_value=({"segments": [{"id": 0, "text": "Redis 2.0 버전입니다."}], "summary": summary()}, {}))
    with pytest.raises(ValueError, match="보존 검사"):
        asyncio.run(worker.process({"title": "회의", "segments": segments}, tmp_path / "whole"))


def test_failed_reprocessing_keeps_previous_valid_results(note):
    async def check():
        await main.start_postprocess("test")
        await main.postprocess_transcript("test")
        previous = main.STORE.get("test")
        note.process.side_effect = RuntimeError("failure")
        await main.start_postprocess("test")
        await main.postprocess_transcript("test")
        result = main.STORE.get("test")
        assert result["corrected_segments"] == previous["corrected_segments"]
        assert result["summary"] == previous["summary"]
        assert result["raw_segments"] == source()

    asyncio.run(check())
