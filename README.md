# MOSS Note

MOSS Note is a self-hosted meeting transcription workspace for Korean and 50+
languages. It combines speaker-aware long-form ASR with an optional LLM pass
that fixes technical terminology without changing timestamps or speaker IDs.

> 현재 UI와 문서는 한국어를 기본으로 제공합니다.

## 주요 기능

- 음성·영상 업로드와 FFmpeg 16 kHz mono 정규화
- [MOSS-Transcribe-Diarize 0.9B](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize) 기반 타임스탬프·화자 분리 전사
- 90분 이하는 단일 요청, 초과 파일은 자동 분할·병합
- 청크 경계를 가로지르는 5분 브리지 전사로 화자 ID 연결
- [Qwen3.8-27B-FP8](https://huggingface.co/Qwen/Qwen3.8-27B-FP8) 기반 기술 용어·오탈자 교정
- 교정 대상 48개와 앞뒤 문맥 12개를 사용하는 sliding window
- 교정 청크별 진행률, 실패·재시작 시 체크포인트 이어하기
- 전사 원본·편집본·AI 교정본 별도 보존과 변경 비교
- 오디오 위치 이동, 화자명 변경, 검색, 자동 저장
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
   └── correction queue ───── Qwen vLLM :8002 (GPU 1)
```

두 큐는 서로 독립적으로 한 작업씩 처리합니다. SQLite 작업 복구를 위해 앱 프로세스는
항상 `--workers 1`로 실행해야 합니다. vLLM 포트는 localhost 또는 사설 네트워크에만
노출합니다.

## 요구사항

검증된 구성은 Ubuntu 24.04, Python 3.12, FFmpeg, CUDA 13 호환 NVIDIA 드라이버와
48 GB GPU 2장입니다.

| 구성 요소 | 권장 자원 |
|---|---:|
| MOSS 전사 | GPU 1장, 약 20 GB VRAM |
| Qwen FP8 교정 | GPU 1장, 약 45 GB VRAM |
| 모델 저장공간 | 약 31 GB + 2 GB |
| Python/vLLM 환경 | 약 10 GB |

Qwen 후처리가 필요 없다면 `.env`에서 `MOSS_REQUIRE_QWEN=false`로 바꾸고 Qwen 모델을
다운로드하지 않아도 됩니다. MOSS의 90분 프로파일은 VRAM을 넉넉하게 사용하므로 더 작은
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

첫 설치는 약 33 GB 모델과 CUDA용 Python 패키지를 다운로드합니다. 준비가 끝나면
`http://127.0.0.1:8000`을 엽니다. SSH 서버에서는 8000번 포트를 포워딩하세요.

프로세스를 따로 실행할 수도 있습니다.

```bash
./scripts/run-vllm.sh   # MOSS, 127.0.0.1:8001
./scripts/run-qwen.sh   # Qwen, 127.0.0.1:8002
./scripts/run-app.sh    # Web, 127.0.0.1:8000
```

모든 실행 스크립트는 `.env`를 자동으로 읽습니다. 다른 파일을 쓰려면
`MOSS_ENV_FILE=/path/to/env ./scripts/start.sh`처럼 지정합니다.

## Docker로 앱 배포

GPU 모델 서버는 위의 네이티브 스크립트로 localhost에서 실행하고, FastAPI 앱만
컨테이너로 격리할 수 있습니다. Docker Compose 플러그인이 설치되어 있다면:

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
| `QWEN_WINDOW_TARGET` | `48` | 한 번에 확정하는 교정 발화 수 |
| `QWEN_WINDOW_CONTEXT` | `12` | 앞뒤에 붙이는 참고 발화 수 |
| `MOSS_REQUIRE_QWEN` | `true` | readiness에서 Qwen을 필수로 볼지 여부 |
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
- Qwen에는 발화 ID·화자·텍스트만 전달하며 결과에서는 텍스트만 반영합니다. 숫자,
  버전, URL, 이메일이 바뀌거나 길이가 지나치게 달라진 결과는 발화 단위로 버립니다.
- 실제 정확도는 언어, 마이크 품질, 겹쳐 말하기, 고유명사에 따라 달라집니다.
- 인증과 사용자별 권한이 필요한 다중 사용자 SaaS가 아니라 신뢰된 팀용 self-hosted 앱입니다.

## 라이선스

애플리케이션 코드는 [MIT License](LICENSE)로 배포합니다. 모델 가중치는 저장소에 포함되지
않으며 각 공식 모델 저장소의 라이선스를 따릅니다. 현재 사용하는 MOSS와 Qwen3.8 모델
카드는 Apache-2.0 라이선스를 명시합니다. 구성 요소별 근거와 컨테이너 재배포 주의사항은
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)에 정리되어 있습니다.
