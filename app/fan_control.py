from __future__ import annotations

import asyncio
import logging
import os


class FanControl:
    def __init__(self, enabled: bool = False):
        self.enabled = enabled

    @classmethod
    def from_env(cls) -> FanControl:
        return cls(os.getenv("MOSS_MAX_FAN_DURING_TRANSCRIPTION", "false").lower() == "true")

    async def _command(self, action: str) -> None:
        process = await asyncio.create_subprocess_exec(
            "sudo", "-n", "/usr/bin/systemctl", action, "moss-note-fan.service",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, error = await process.communicate()
        if process.returncode:
            raise RuntimeError(f"Fan {action} failed: {error.decode().strip()}")

    async def start(self) -> None:
        if self.enabled:
            await self._command("start")

    async def stop(self) -> None:
        if self.enabled:
            try:
                await self._command("stop")
            except Exception:
                # A cooling-control failure must not kill the transcription queue.
                logging.exception("Failed to restore automatic fan control")
