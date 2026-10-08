#!/usr/bin/env python3
"""Create deployment credentials once, without exposing them in logs."""
from pathlib import Path
import os
import secrets

root = Path(__file__).resolve().parents[1]
env = root / ".env"
credentials = root / ".deployment-credentials"
if not credentials.exists():
    password = secrets.token_urlsafe(24)
    with credentials.open("x") as output:
        os.chmod(credentials, 0o600)
        output.write(f"Username: seongwoon\nPassword: {password}\n")
    with env.open("a") as output:
        output.write(f"\nMOSS_AUTH_USERNAME=seongwoon\nMOSS_AUTH_PASSWORD={password}\n")
os.chmod(env, 0o600)
print(f"Login credentials: {credentials}")
