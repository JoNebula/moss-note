from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import time

import httpx


ROOT = Path(__file__).resolve().parents[1]


class ModelRuntime:
    """Serialize allowlisted model switches between complete transcription jobs."""

    def __init__(self, variants: dict, selection_file: Path | None, url: str, managed: bool = False):
        self.variants = variants
        self.selection_file = selection_file
        self.url = url.rstrip("/")
        self.managed = managed
        self.lock = asyncio.Lock()
        self.target: str | None = None
        self.error: str | None = None

    @classmethod
    def from_env(cls) -> ModelRuntime:
        managed = os.getenv("MOSS_MANAGED_MODELS", "false").lower() == "true"
        if managed:
            catalogue = Path(os.environ["MOSS_MODEL_VARIANTS_FILE"])
            variants = json.loads(catalogue.read_text())
            if set(variants) != {"bf16", "rtn-w8", "rtn-w4"}:
                raise ValueError("Unexpected model variant catalogue")
            for variant in variants.values():
                path = Path(variant["path"])
                if not path.is_absolute() or not path.resolve().is_relative_to("/data/models"):
                    raise ValueError("Managed models must use pinned /data/models paths")
            selection = Path(os.environ["MOSS_MODEL_SELECTION_FILE"])
        else:
            variants = {"bf16": {"label": "BF16", "path": os.getenv("MOSS_MODEL_PATH", "")}}
            selection = None
        return cls(variants, selection, os.getenv("MOSS_VLLM_URL", "http://127.0.0.1:8001/v1"), managed)

    def validate(self, variant: str) -> None:
        if variant not in self.variants:
            raise ValueError("Unknown model variant")
        if self.managed and not (Path(self.variants[variant]["path"]) / "config.json").is_file():
            raise ValueError("Selected model is not installed")

    def variant_for_path(self, path: str | None) -> str | None:
        return next((key for key, value in self.variants.items() if value["path"] == path), None)

    def selected_variant(self) -> str:
        if self.selection_file and self.selection_file.exists():
            variant = json.loads(self.selection_file.read_text())["variant"]
        else:
            variant = self.variant_for_path(os.getenv("MOSS_MODEL_PATH")) or "bf16"
        self.validate(variant)
        return variant

    def selected_path(self) -> str:
        return self.variants[self.selected_variant()]["path"]

    def public_state(self, active: str | None, online: bool) -> dict:
        return {
            "options": [
                {"id": key, "label": value["label"], "available": not self.managed or (Path(value["path"]) / "config.json").is_file()}
                for key, value in self.variants.items()
            ],
            "active": active if self.managed else ("bf16" if online else None),
            "target": self.target,
            "status": "loading" if self.target else ("ready" if online else "offline"),
            "error": self.error,
        }

    def _write_selection(self, variant: str) -> None:
        self.validate(variant)
        assert self.selection_file is not None
        self.selection_file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.selection_file.parent, delete=False) as file:
            temporary = Path(file.name)
            try:
                json.dump({"variant": variant}, file)
                file.flush()
                os.fsync(file.fileno())
                temporary.chmod(0o640)
                os.replace(temporary, self.selection_file)
            finally:
                temporary.unlink(missing_ok=True)

    async def active_variant(self) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.url}/models")
                response.raise_for_status()
                models = response.json().get("data", [])
                return self.variant_for_path(models[0].get("root")) if models else None
        except (httpx.HTTPError, ValueError):
            return None

    async def _restart(self) -> None:
        process = await asyncio.create_subprocess_exec(
            "sudo", "-n", "/usr/bin/systemctl", "restart", "moss-note-model",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(f"Model service restart failed: {stderr.decode().strip()[:500]}")

    async def _wait_ready(self, variant: str, timeout: float = 180) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.active_variant() == variant:
                return
            await asyncio.sleep(2)
        raise RuntimeError("Selected model did not become ready within 180 seconds")

    async def ensure(self, variant: str) -> None:
        self.validate(variant)
        if not self.managed:
            return
        async with self.lock:
            active = await self.active_variant()
            if active == variant:
                self._write_selection(variant)
                self.error = None
                return
            previous = active or self.selected_variant()
            self.target, self.error = variant, None
            try:
                self._write_selection(variant)
                await self._restart()
                await self._wait_ready(variant)
            except Exception as error:
                self.error = str(error)
                # Restore the prior known-good model; never transcribe with a
                # silently substituted precision after a failed switch.
                try:
                    self._write_selection(previous)
                    await self._restart()
                    await self._wait_ready(previous)
                except Exception as rollback_error:
                    self.error += f"; model recovery failed: {rollback_error}"
                raise RuntimeError(self.error) from error
            finally:
                self.target = None


if __name__ == "__main__":
    print(ModelRuntime.from_env().selected_path())
