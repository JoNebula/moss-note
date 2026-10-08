# MOSS Note

MOSS Note is a self-hosted meeting transcription workspace for Korean and 50+
languages. It combines speaker-aware long-form ASR with an optional LLM pass
that fixes technical terminology without changing timestamps or speaker IDs.

> 현재 UI와 문서는 한국어를 기본으로 제공합니다.

현재 Jetson의 모델 경로, 20분 청크·2분 중첩, Cloudflare와 systemd 운영 설정은
[Jetson 배포 문서](deploy/JETSON.md)를 따릅니다. AI 교정은 로컬 Qwen 대신
기존 ChatGPT 로그인으로 **Codex CLI (`gpt-6.1-sol`, `medium`, Fast)**를 실행합니다.
전사 텍스트·제목·중요 단어는 이 단계에서 OpenAI로 전송되며, 교정본과 별도 핵심
요약을 다운로드할 수 있습니다. Codex 사용량 한도가 적용되고 음성 전사는 로컬입니다.

Jetson Thor로 옮길 때는 [Thor 이전 절차](deploy/THOR.md)를 따르세요.
코드와 설정 예시는 Git에 보관하고, 모델·회의 데이터·비밀 설정은 SSH로 별도 이전합니다.

## 주요 기능

- 음성·영상 업로드와 FFmpeg 16 kHz mono 정규화
- [MOSS-Transcribe-Diarize 0.9B](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize) 기반 타임스탬프·화자 분리 전사
- Jetson 배포는 20분 청크·2분 중첩으로 자동 분할·화자 연결·병합
- 원본 BF16 및 RTN W8A16/W4A16 모델 선택
- Codex CLI (`gpt-6.1-sol`, `medium`) 기반 기술 용어·오탈자 교정
- 전체 전사 파일을 한 번의 CLI 요청으로 교정하고 요약까지 함께 수신
- 변경된 발화만 반환받아 전체 교정본을 복원해 출력 대기 시간 절감
- 출처 표시 없는 핵심 요약·결정 사항·후속 작업, 별도 Markdown/TXT 다운로드
- 전체 응답 검증 후 교정본·요약 동시 반영, 실패 시 기존 결과 보존
- 전사 원본·편집본·AI 교정본 별도 보존과 변경 비교
- 오디오 위치 이동, 화자명 변경, 검색, 자동 저장
- 선택한 화자들을 원하는 화자로 병합, 이후 문장 편집을 보존하는 되돌리기
- TXT, SRT, WebVTT, JSON 내보내기
- SQLite와 로컬 파일시스템을 사용하는 단일 사용자 배포

## 구조

```text
Browser
   │
   ▼
FastAPI app :8000 ───── SQLite + uploaded media
   │                         data/
   ├── transcription queue ── MOSS vLLM :8001 (GPU 0)
   └── correction queue ───── Codex CLI ── OpenAI (ChatGPT login)
```

두 큐는 서로 독립적으로 한 작업씩 처리합니다. SQLite 작업 복구를 위해 앱 프로세스는
항상 `--workers 1`로 실행해야 합니다. vLLM 포트는 localhost 또는 사설 네트워크에만
노출합니다.

## 요구사항

원본 저장소의 로컬 MOSS + Qwen 구성은 Ubuntu 24.04, Python 3.12, FFmpeg,
CUDA 13 호환 NVIDIA 드라이버와 48 GB GPU 2장에서 검증됐습니다.
현재 Codex 교정에는 Qwen GPU와 가중치가 필요 없으며, Jetson 런타임은 별도 배포 문서에 기록돼 있습니다.

| 구성 요소 | 권장 자원 |
|---|---:|
| MOSS 전사 | GPU 1장, 약 20 GB VRAM |
| Qwen FP8 교정 | GPU 1장, 약 45 GB VRAM |
| 모델 저장공간 | 약 31 GB + 2 GB |
| Python/vLLM 환경 | 약 10 GB |

기본 `MOSS_REQUIRE_QWEN=false`에서는 Qwen을 다운로드하거나 실행하지 않습니다.
Codex CLI는 앱 서비스 사용자로 ChatGPT 로그인하고 `MOSS_CODEX_BINARY`를 설정해야 합니다.
MOSS의 90분 프로파일은 VRAM을 넉넉하게 사용하므로 더 작은
GPU에서는 `MOSS_CHUNK_SECONDS`와 vLLM 메모리 설정을 함께 낮춰야 합니다.

## 빠른 시작

```bash
git clone YOUR_REPOSITORY_URL moss-note
cd moss-note
cp .env.example .env
# .env에서 사용할 GPU 번호와 주소를 확인하세요.

./scripts/setup.sh
./scripts/doctor.sh
./scripts/start.sh
```

기본 설치는 MOSS 모델과 CUDA용 Python 패키지만 다운로드합니다. 준비가 끝나면
`http://127.0.0.1:8000`을 엽니다. SSH 서버에서는 8000번 포트를 포워딩하세요.

프로세스를 따로 실행할 수도 있습니다.

```bash
./scripts/run-vllm.sh   # MOSS, 127.0.0.1:8001
./scripts/run-qwen.sh   # 레거시 선택 사항, 현재 AI 교정에는 불필요
./scripts/run-app.sh    # Web, 127.0.0.1:8000
```

모든 실행 스크립트는 `.env`를 자동으로 읽습니다. 다른 파일을 쓰려면
`MOSS_ENV_FILE=/path/to/env ./scripts/start.sh`처럼 지정합니다.

## Docker로 앱 배포

GPU 모델 서버는 위의 네이티브 스크립트로 localhost에서 실행하고, FastAPI 앱만
컨테이너로 격리할 수 있습니다. Docker Compose 플러그인이 설치되어 있다면:

현재 Docker 이미지에는 Codex CLI와 로그인 자격 증명이 포함되지 않습니다.
AI 교정은 CLI가 설치·로그인된 네이티브 앱 서비스에서 검증했으며 컨테이너에서는 추가 설정이 필요합니다.

```bash
docker compose up -d --build
docker compose ps
```

`compose.yaml`은 Linux host network를 사용해 localhost의 8001·8002번 vLLM에 연결하고,
앱도 `127.0.0.1:8000`에만 바인딩합니다. 따라서 모델 포트를 외부에 열 필요가 없습니다.
업로드와 DB는 `moss-data` Docker volume에 보존됩니다. Compose 없이도 앱 이미지를
검증할 수 있습니다.

```bash
docker build -t moss-note:0.2.0 .
```

## 운영 배포

1. `.env`를 만들고 GPU, 모델 경로, 업로드 제한을 설정합니다.
2. `deploy/moss-note.service.example`을 실제 사용자와 경로에 맞춰 systemd에 등록합니다.
3. 앱은 localhost에 유지하고 Caddy/Nginx 같은 HTTPS 리버스 프록시만 외부에 엽니다.
4. 외부 사용 시 `MOSS_AUTH_USERNAME`과 강한 `MOSS_AUTH_PASSWORD`를 모두 설정합니다.
5. `data/`를 정기적으로 백업하고 접근 권한을 제한합니다.

`deploy/Caddyfile.example`에 최소 TLS 프록시 예시가 있습니다. HTTP Basic 인증은 반드시
HTTPS 뒤에서만 사용하세요. 더 자세한 내용은 [SECURITY.md](SECURITY.md)를 참고하세요.

상태 점검 엔드포인트:

```bash
curl http://127.0.0.1:8000/healthz  # 앱 프로세스 liveness
curl http://127.0.0.1:8000/readyz   # 필요한 모델 서버 readiness
curl http://127.0.0.1:8000/api/health  # 상세 상태, 인증 적용 대상
```

## 주요 환경 변수

전체 목록과 기본값은 [.env.example](.env.example)에 있습니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MOSS_CUDA_DEVICE` | `0` | MOSS GPU 번호 |
| `QWEN_CUDA_DEVICE` | `1` | Qwen GPU 번호 |
| `MOSS_APP_HOST` | `127.0.0.1` | 웹 앱 바인드 주소 |
| `MOSS_CHUNK_SECONDS` | `5400` | 최대 전사 청크 길이, 최대 90분 |
| `MOSS_BRIDGE_SECONDS` | `300` | 화자 연결용 경계 브리지 길이 |
| `MOSS_CODEX_BINARY` | `/home/jetson/.local/bin/codex` | 로그인된 Codex CLI 경로 |
| `MOSS_CODEX_TIMEOUT_SECONDS` | `1200` | CLI 호출당 제한 시간 |
| `MOSS_REQUIRE_QWEN` | `false` | 레거시 Qwen readiness 검사 (현재 교정에는 불필요) |
| `MOSS_AUTH_USERNAME` | 빈 값 | 선택형 HTTP Basic 사용자명 |
| `MOSS_AUTH_PASSWORD` | 빈 값 | 선택형 HTTP Basic 비밀번호 |

## 데이터와 개인정보

다음 경로는 `.gitignore`와 `.dockerignore`에 포함되며 저장소나 이미지에 들어가지 않습니다.

- `data/`: 녹음 원본, 정규화 파일, SQLite DB
- `models/`: Hugging Face 체크포인트
- `logs/`: vLLM 로그
- `.env`: 배포별 설정과 자격 증명
- `.venv*`, `.tools/`: 로컬 런타임 도구

모델 다운로드 스크립트는 공식 Hugging Face 저장소만 사용합니다. MOSS 실행에는
`--trust-remote-code`가 필요하므로 모델 출처를 임의로 바꾸지 마세요.

## 개발과 테스트

GPU나 모델 다운로드 없이 단위 테스트와 앱 이미지 빌드를 검증할 수 있습니다.

```bash
uv sync --python 3.12 --group dev
make check
make docker-build
```

GitHub Actions는 Python 테스트, Python/JavaScript/shell 문법 검사와 Docker 빌드를
실행합니다.

## 정확도와 제한

- 실시간 스트리밍이 아니라 업로드 후 일괄 처리 방식입니다.
- 90분 초과 파일의 화자는 경계 브리지에서 양쪽 모두 발화한 경우에만 자동 연결할 수
  있습니다. 브리지에서 말하지 않은 화자는 별도 ID로 남을 수 있습니다.
- Codex 교정은 전체 전사 JSON과 제목·화자명·중요 단어를 파일로 준비한 뒤 CLI 표준 입력으로
  한 번 전달합니다. 결과 JSON은 바뀐 발화의 텍스트와 별도 요약만 담습니다. 서버가 변경되지
  않은 발화를 원문에서 복원해 전체 교정본을 만들고 ID·화자·시간·순서는 보존합니다.
  숫자, 버전, URL, 이메일 변경이나 과도한 수정이 검출되면 전체 결과를 반영하지 않습니다.
- 교정 입력은 최대 1 MB이며 모델의 입력·출력 토큰 한도도 적용됩니다. 파일을 사용한다고
  토큰이 줄거나 한도가 없어지는 것은 아닙니다. 실패 시 전체 파일을 다시 처리합니다.
- Fast 교정 요청에는 `service_tier="priority"`를 명시하고 `fast_mode`를 활성화합니다.
  [공식 OpenAI Docs](https://learn.chatgpt.com/docs/agent-configuration/speed)에 따르면 구독 포함
  사용량은 Standard의 2.5배, 구매 크레딧은 2배를 사용합니다. 실제 시간과 가용성은 달라질 수 있습니다.
- 화자 병합은 편집본과 AI 교정본에 적용하고 원본은 유지합니다. 최근 20회 병합을 취소할 수
  있으며 이후 문장 편집은 유지됩니다. 화자나 교정본이 바뀌면 기존 요약의 갱신 필요 여부를 표시합니다.
- 실제 정확도는 언어, 마이크 품질, 겹쳐 말하기, 고유명사에 따라 달라집니다.
- 인증과 사용자별 권한이 필요한 다중 사용자 SaaS가 아니라 신뢰된 팀용 self-hosted 앱입니다.

## 라이선스

애플리케이션 코드는 [MIT License](LICENSE)로 배포합니다. 모델 가중치는 저장소에 포함되지
않으며 각 공식 모델 저장소의 라이선스를 따릅니다. 현재 사용하는 MOSS와 Qwen3.8 모델
카드는 Apache-2.0 라이선스를 명시합니다. 구성 요소별 근거와 컨테이너 재배포 주의사항은
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 정리되어 있습니다.
