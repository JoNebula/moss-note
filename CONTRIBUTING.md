# Contributing

1. Install Python 3.12, uv, FFmpeg, and Node.js.
2. Run `uv sync --group dev`.
3. Run `make check` before opening a pull request.

Tests must not download model weights or require a GPU. Keep model and runtime
data under the gitignored `models/`, `data/`, and `logs/` directories.
