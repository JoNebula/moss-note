#!/usr/bin/env python3
"""Compare transcript transport and sparse output using synthetic data only."""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.codex_postprocess import CodexPostprocessor, DATA_INSTRUCTIONS, new_run_directory


def benchmark_document(count: int) -> dict:
    statements = [
        "Kubernetes 클러스터의 배포 설정을 확인했습니다.",
        "Redis 캐시 연결은 정상이며 추가 변경은 필요하지 않습니다.",
        "실험 로그를 저장하고 다음 회의에서 결과를 비교하겠습니다.",
        "데이터 전처리 단계에서 누락된 샘플이 있는지 확인했습니다.",
        "PyTorch 학습 코드의 입력 형식은 기존 설정을 유지합니다.",
        "새로운 평가 데이터는 아직 확보하지 못했습니다.",
        "GitHub 저장소에 실험 설정과 측정 결과를 기록했습니다.",
        "추가 실험 일정은 결과를 확인한 뒤 결정하기로 했습니다.",
    ]
    segments = []
    for index in range(count):
        text = statements[index % len(statements)]
        if index % 16 == 0:
            text = "파이토치와 쿠버네티스의 연동 설정을 확인했습니다."
        if index % 9 == 0:
            text += " 실험 버전은 1.2이고 샘플은 32개입니다."
        segments.append({"id": index, "start": index * 5.0, "end": index * 5.0 + 5,
                         "speaker": f"S0{index % 2 + 1}", "text": text})
    return {"title": "Synthetic lab infrastructure meeting", "known_terms": "Kubernetes, Redis, PyTorch, GitHub",
            "speaker_names": {"S01": "A", "S02": "B"}, "segments": segments}


class BenchmarkWorker(CodexPostprocessor):
    def __init__(self, mode: str, timeout: float):
        base = CodexPostprocessor.from_env()
        super().__init__(base.binary, timeout, output_mode="changes" if mode == "direct-optimized" else "full",
                         compact_input=mode == "direct-optimized", use_model_catalog=mode == "direct-optimized")
        self.mode = mode

    def command(self, directory: Path) -> list[str]:
        command = super().command(directory)
        if self.mode == "file-full":
            for feature in ("code_mode_host", "code_mode", "unified_exec", "shell_tool"):
                index = command.index(feature)
                del command[index - 1:index + 1]
            (directory / "input-transcript.json").write_text(json.dumps(self.file_document, ensure_ascii=False, indent=2))
            instructions = DATA_INSTRUCTIONS.replace(
                "Do not use tools, execute commands, browse, read files, or access other conversations.",
                "Read only input-transcript.json using cat with the shell tool. Never browse, modify files, or access any other files or conversations.")
            (directory / "instructions.txt").write_text(instructions)
            command[-1:-1] = ["--enable", "code_mode_host", "--enable", "code_mode", "--enable", "unified_exec", "--enable", "shell_tool"]
        return command

    async def run(self, prompt: str, schema: dict, directory: Path, *, document: dict | None = None):
        if self.mode == "file-full":
            self.file_document = document
            prompt += "\nFirst use the shell tool to run cat input-transcript.json and read the complete file. Return the full corrected transcript and summary. Do not answer before reading it. Do not read any other files."
            return await super().run(prompt, schema, directory)
        if self.mode == "direct-sparse":
            schema = deepcopy(schema)
            schema["properties"]["segments"]["minItems"] = 0
            prompt = prompt.replace("모든 발화를 빠짐없이 원래 ID 순서로 한 번씩 반환하고 text만 수정하세요.",
                                    "전체 발화를 검토하되 segments에는 text가 실제로 변경된 발화만 ID 순서로 반환하세요. 수정 없는 발화는 반환하지 마세요. 없으면 빈 배열입니다. 서버가 변경분을 원문에 반영해 전체 파일을 복원합니다.")
        response, usage = await super().run(prompt, schema, directory, document=document)
        if self.mode == "direct-sparse":
            source = {item["id"]: item["text"] for item in document["segments"]}
            edits = response["segments"]
            assert all(type(item["id"]) is int and item["id"] in source for item in edits)
            assert len({item["id"] for item in edits}) == len(edits)
            source.update({item["id"]: item["text"] for item in edits})
            response = {**response, "segments": [{"id": key, "text": value} for key, value in source.items()]}
        return response, usage


async def main(args):
    root = new_run_directory(Path("/data/artifacts/moss-note"), "codex-latency-benchmark")
    document = benchmark_document(args.segments)
    measurements = []
    modes = args.modes
    for repeat in range(args.repeats):
        for mode in (modes if repeat % 2 == 0 else list(reversed(modes))):
            worker = BenchmarkWorker(mode, args.timeout)
            directory = root / f"{repeat + 1}-{mode}"
            started = time.monotonic()
            print(f"START repeat={repeat + 1} mode={mode} segments={args.segments}", flush=True)
            try:
                corrected, summary, usage = await worker.process(document, directory)
                assert len(corrected) == len(document["segments"])
                for original, edited in zip(document["segments"], corrected, strict=True):
                    assert {k: v for k, v in original.items() if k != "text"} == {k: v for k, v in edited.items() if k != "text"}
                targets = [index for index in range(args.segments) if index % 16 == 0]
                fixed = sum("PyTorch" in corrected[index]["text"] and "Kubernetes" in corrected[index]["text"] for index in targets)
                assert fixed == len(targets), "Known technical terms were not all corrected"
                raw = json.loads((directory / "result.json").read_text())
                events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
                record = {"mode": mode, "repeat": repeat + 1, "seconds": time.monotonic() - started,
                          "usage": usage, "returned_segments": len(raw["segments"]), "corrected_targets": fixed,
                          "tool_calls": sum(event.get("item", {}).get("type") == "mcp_tool_call" and event["type"] == "item.completed" for event in events),
                          "command_calls": sum(event.get("item", {}).get("type") == "command_execution" and event["type"] == "item.completed" for event in events),
                          "valid": True}
                if mode == "file-full":
                    assert record["command_calls"] >= 1, "File mode must actually read the transcript tool"
            except Exception as error:
                record = {"mode": mode, "repeat": repeat + 1, "seconds": time.monotonic() - started,
                          "valid": False, "error": str(error) or type(error).__name__}
            measurements.append(record)
            (root / "measurements.json").write_text(json.dumps({"segments": args.segments, "measurements": measurements}, indent=2))
            print(json.dumps(record), flush=True)
    medians = {mode: statistics.median(item["seconds"] for item in measurements if item["mode"] == mode and item["valid"])
               for mode in modes if any(item["mode"] == mode and item["valid"] for item in measurements)}
    report = {"segments": args.segments, "synthetic_only": True, "model": "gpt-6.1-sol", "effort": "medium",
              "tier": "priority", "median_seconds": medians, "measurements": measurements,
              "selected": min(medians, key=medians.get) if medians else None}
    (root / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"directory": str(root), **report}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--segments", type=int, default=64)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--modes", nargs="+", choices=["direct-full", "file-full", "direct-sparse", "direct-optimized"],
                        default=["direct-full", "file-full", "direct-sparse", "direct-optimized"])
    asyncio.run(main(parser.parse_args()))
