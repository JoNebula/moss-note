from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx


NUMBER_PATTERN = re.compile(r"(?<![\w])[-+]?\d+(?:[.,:/-]\d+)*%?(?![\w])")
PROTECTED_PATTERN = re.compile(
    r"(?:https?://\S+|www\.\S+|[\w.+-]+@[\w.-]+\.\w+)", re.IGNORECASE
)


@dataclass(frozen=True)
class CorrectionWindow:
    index: int
    segments: list[dict[str, Any]]
    target_ids: list[int]


def make_correction_windows(
    segments: list[dict[str, Any]], target_size: int = 48, context_size: int = 12
) -> list[CorrectionWindow]:
    """Split a transcript into non-overlapping targets with overlapping context."""
    if target_size < 1 or context_size < 0:
        raise ValueError("target_size must be positive and context_size non-negative")
    windows: list[CorrectionWindow] = []
    for target_start in range(0, len(segments), target_size):
        target_end = min(len(segments), target_start + target_size)
        context_start = max(0, target_start - context_size)
        context_end = min(len(segments), target_end + context_size)
        windows.append(
            CorrectionWindow(
                index=len(windows),
                segments=segments[context_start:context_end],
                target_ids=[int(item["id"]) for item in segments[target_start:target_end]],
            )
        )
    return windows


def _schema(target_count: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "minItems": target_count,
                "maxItems": target_count,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "text": {"type": "string"},
                    },
                    "required": ["id", "text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["segments"],
        "additionalProperties": False,
    }


SYSTEM_PROMPT = """당신은 한국어 음성 전사 교정기입니다. 제공된 주변 발화는 문맥으로만 사용하고 target_ids에 포함된 발화만 반환하세요.

허용되는 수정:
- 음성 인식으로 잘못 표기된 IT·과학·비즈니스 기술 용어를 정확한 표기로 교정
- 제품, 오픈소스, 라이브러리, 프로토콜, API 이름은 널리 알려진 공식 영문 대소문자를 우선 사용
  (예: 쿠버네티스→Kubernetes, 레디스→Redis, 파이토치→PyTorch, 깃허브→GitHub)
- 명백한 오탈자, 띄어쓰기와 최소한의 문장부호 교정
- 문맥상 명백한 동음이의어 오류 교정

금지되는 수정:
- 내용을 요약하거나 문체를 더 전문적으로 재작성
- 원문에 없던 사실, 주어, 목적어, 설명을 추가
- 숫자, 버전, URL, 이메일, 사람 이름을 추측해서 변경
- 발화 합치기·나누기, 순서·ID 변경

확신이 없으면 반드시 원문을 그대로 유지하세요. JSON 스키마에 맞는 결과만 반환하세요."""


def request_payload(
    window: CorrectionWindow,
    model_name: str,
    hotwords: str | None,
    max_tokens: int,
) -> dict[str, Any]:
    compact_segments = [
        {
            "id": int(segment["id"]),
            "speaker": segment.get("speaker", ""),
            "text": segment.get("text", ""),
        }
        for segment in window.segments
    ]
    user_payload = {
        "known_terms": hotwords or "",
        "target_ids": window.target_ids,
        "segments": compact_segments,
    }
    return {
        "model": model_name,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False),
            },
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "chat_template_kwargs": {
            "enable_thinking": False,
            "preserve_thinking": False,
        },
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "transcript_correction",
                "strict": True,
                "schema": _schema(len(window.target_ids)),
            },
        },
    }


def parse_response(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("Qwen 응답에 message.content가 없습니다.") from error
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise ValueError("Qwen 응답 본문 형식이 올바르지 않습니다.")
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped)
    try:
        result = json.loads(stripped)
    except json.JSONDecodeError as error:
        raise ValueError("Qwen 응답을 JSON으로 해석하지 못했습니다.") from error
    if not isinstance(result, dict):
        raise ValueError("Qwen 응답의 최상위 값은 객체여야 합니다.")
    return result


def _protected_values(text: str) -> tuple[list[str], list[str]]:
    return NUMBER_PATTERN.findall(text), PROTECTED_PATTERN.findall(text)


def validate_corrections(
    source_segments: list[dict[str, Any]],
    target_ids: list[int],
    response: dict[str, Any],
) -> dict[int, str]:
    """Validate identity and retain source text when protected values changed."""
    items = response.get("segments")
    if not isinstance(items, list):
        raise ValueError("Qwen 응답에 segments 배열이 없습니다.")
    expected = set(target_ids)
    source_by_id = {int(item["id"]): item for item in source_segments}
    corrected: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            raise ValueError("교정 결과의 발화 ID가 올바르지 않습니다.")
        segment_id = item["id"]
        text = item.get("text")
        if segment_id not in expected or segment_id in corrected or not isinstance(text, str):
            raise ValueError("교정 결과의 발화 ID 또는 텍스트가 올바르지 않습니다.")
        text = text.strip()
        source_text = str(source_by_id[segment_id].get("text", "")).strip()
        if not text or _protected_values(text) != _protected_values(source_text):
            text = source_text
        if source_text and not (0.35 <= len(text) / len(source_text) <= 2.5):
            text = source_text
        corrected[segment_id] = text
    if set(corrected) != expected:
        raise ValueError("교정 결과에 누락되거나 불필요한 발화 ID가 있습니다.")
    return corrected


def apply_corrections(
    source_segments: list[dict[str, Any]], corrected_text: dict[int, str]
) -> list[dict[str, Any]]:
    """Apply text only; all timing, speaker and chunk metadata stay untouched."""
    return [
        {
            **segment,
            "text": corrected_text.get(int(segment["id"]), segment.get("text", "")),
        }
        for segment in source_segments
    ]


async def correct_window(
    client: httpx.AsyncClient,
    api_url: str,
    model_name: str,
    window: CorrectionWindow,
    hotwords: str | None,
    max_tokens: int,
    retries: int = 2,
) -> dict[int, str]:
    last_error: Exception | None = None
    for _ in range(retries + 1):
        try:
            response = await client.post(
                f"{api_url.rstrip('/')}/chat/completions",
                json=request_payload(window, model_name, hotwords, max_tokens),
            )
            response.raise_for_status()
            return validate_corrections(
                window.segments, window.target_ids, parse_response(response.json())
            )
        except (httpx.HTTPError, ValueError) as error:
            last_error = error
    assert last_error is not None
    raise last_error
