"""Signed, expiring browser tokens; changing credentials revokes sessions."""
import hashlib
import hmac
import secrets
import time


def issue_token(username: str, password: str, purpose: str, lifetime: int) -> str:
    payload = f"{purpose}.{int(time.time()) + lifetime}.{secrets.token_hex(16)}"
    key = hashlib.sha256(f"{username}\0{password}".encode()).digest()
    signature = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def valid_token(token: str, username: str, password: str, purpose: str) -> bool:
    try:
        kind, expiry, nonce, signature = token.split(".")
        if kind != purpose or int(expiry) <= time.time() or len(nonce) != 32:
            return False
        key = hashlib.sha256(f"{username}\0{password}".encode()).digest()
        expected = hmac.new(
            key, f"{kind}.{expiry}.{nonce}".encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False
