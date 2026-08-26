FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MOSS_DATA_DIR=/app/data \
    MOSS_APP_HOST=0.0.0.0 \
    MOSS_APP_PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN python -m pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev \
    && useradd --create-home --uid 10001 moss-note \
    && mkdir -p /app/data \
    && chown -R moss-note:moss-note /app/data

USER moss-note
EXPOSE 8000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD [".venv/bin/python", "-c", "import os, urllib.request; port=os.getenv('MOSS_APP_PORT', '8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=3)"]

CMD ["sh", "-c", "exec .venv/bin/uvicorn app.main:app --host \"${MOSS_APP_HOST:-0.0.0.0}\" --port \"${MOSS_APP_PORT:-8000}\" --workers 1 --proxy-headers"]
