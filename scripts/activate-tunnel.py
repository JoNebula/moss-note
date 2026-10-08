#!/usr/bin/env python3
"""Activate the prepared tunnel and print its DNS target without its token."""
import base64
import getpass
import json
import os
from pathlib import Path
import subprocess
import uuid

token_file = Path(__file__).resolve().parents[1] / ".cloudflare-tunnel-token"
saved_token = token_file.read_text().strip() if token_file.exists() else ""
token = saved_token or getpass.getpass("Cloudflare tunnel token (hidden): ").strip()
payload = json.loads(base64.b64decode(token + "=" * (-len(token) % 4)))
tunnel_id = str(uuid.UUID(payload["t"]))
if not saved_token:
    descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(token + "\n")
token_file.chmod(0o600)
subprocess.run(
    ["sudo", "systemctl", "enable", "--now", "moss-note-tunnel"], check=True
)
print(f"DNS: CNAME stt -> {tunnel_id}.cfargotunnel.com (Proxied, TTL Auto)")
print("Cloudflare application route: stt.seongwoonjo.com -> http://localhost:8000")
