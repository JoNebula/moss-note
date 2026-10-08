from __future__ import annotations

import base64
import re
import asyncio
import pytest

import app.main as main
from fastapi.testclient import TestClient
from app.auth import issue_token, valid_token


@pytest.fixture(autouse=True)
def isolated_queues(monkeypatch):
    monkeypatch.setattr(main, "QUEUE", asyncio.Queue())
    monkeypatch.setattr(main, "CORRECTION_QUEUE", asyncio.Queue())


def basic(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def test_auth_is_disabled_when_username_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(main, "AUTH_USERNAME", "")
    monkeypatch.setattr(main, "AUTH_PASSWORD", "")
    assert main.valid_basic_auth(None)


@pytest.mark.parametrize("path", ["/app.js?v=20261008-performance", "/styles.css?v=20261008-performance"])
def test_static_assets_are_not_cached(monkeypatch, path) -> None:
    monkeypatch.setattr(main, "AUTH_USERNAME", "admin")
    monkeypatch.setattr(main, "AUTH_PASSWORD", "admin")
    with TestClient(main.app) as client:
        response = client.get(path, headers={"Authorization": basic("admin", "admin")})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


def test_health_exposes_actual_upload_and_chunk_limits(monkeypatch) -> None:
    async def model_health():
        return {"vllm": {"ok": True}, "qwen": {"ok": False}}

    monkeypatch.setattr(main, "model_health", model_health)
    monkeypatch.setattr(main, "AUTH_USERNAME", "admin")
    monkeypatch.setattr(main, "AUTH_PASSWORD", "admin")
    monkeypatch.setattr(main, "MAX_AUDIO_CHUNK_SECONDS", 300)
    monkeypatch.setattr(main, "CHUNK_OVERLAP_SECONDS", 120)
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 96000000)
    with TestClient(main.app) as client:
        response = client.get("/api/health", headers={"Authorization": basic("admin", "admin")})
        assert response.status_code == 200
        assert response.json()["limits"] == {
            "chunk_seconds": 300, "overlap_seconds": 120, "max_upload_bytes": 96000000,
        }


def test_basic_auth_uses_exact_credentials(monkeypatch) -> None:
    monkeypatch.setattr(main, "AUTH_USERNAME", "deploy")
    monkeypatch.setattr(main, "AUTH_PASSWORD", "correct horse battery staple")

    assert main.valid_basic_auth(basic("deploy", "correct horse battery staple"))
    assert not main.valid_basic_auth(basic("deploy", "wrong"))
    assert not main.valid_basic_auth("Bearer token")
    assert not main.valid_basic_auth("Basic !!!")


def test_health_is_public_but_api_requires_auth(monkeypatch) -> None:
    monkeypatch.setattr(main, "AUTH_USERNAME", "deploy")
    monkeypatch.setattr(main, "AUTH_PASSWORD", "secret")

    with TestClient(main.app) as client:
        assert client.get("/healthz").status_code == 200
        response = client.get("/api/notes")
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Basic ")
        assert client.get(
            "/api/notes", headers={"Authorization": basic("deploy", "secret")}
        ).status_code == 200


def test_browser_login_and_logout(monkeypatch) -> None:
    monkeypatch.setattr(main, "AUTH_USERNAME", "deploy")
    monkeypatch.setattr(main, "AUTH_PASSWORD", "secret")
    with TestClient(main.app, base_url="https://testserver") as client:
        page = client.get("/")
        assert page.url.path == "/login"
        assert "www-authenticate" not in page.headers
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        response = client.post("/login", data={"username": "deploy", "password": "wrong", "csrf": csrf})
        assert response.status_code == 401
        assert "www-authenticate" not in response.headers
        csrf = client.cookies["moss_login_csrf"]
        response = client.post("/login", data={"username": "deploy", "password": "secret", "csrf": csrf}, follow_redirects=False)
        assert response.status_code == 303
        assert "Secure" in response.headers["set-cookie"]
        assert "HttpOnly" in response.headers["set-cookie"]
        assert client.get("/api/notes").status_code == 200
        assert client.post("/logout", headers={"origin": "https://evil.example"}).status_code == 403
        assert client.post("/logout", headers={"origin": "https://testserver"}, follow_redirects=False).status_code == 303
        assert client.get("/api/notes").status_code == 401


def test_login_rejects_missing_csrf_cookie(monkeypatch) -> None:
    monkeypatch.setattr(main, "AUTH_USERNAME", "deploy")
    monkeypatch.setattr(main, "AUTH_PASSWORD", "secret")
    with TestClient(main.app) as client:
        csrf = issue_token("deploy", "secret", "login", 600)
        assert client.post("/login", data={"username": "deploy", "password": "secret", "csrf": csrf}).status_code == 403


def test_favicon_and_second_login_page_do_not_invalidate_form(monkeypatch) -> None:
    monkeypatch.setattr(main, "AUTH_USERNAME", "admin")
    monkeypatch.setattr(main, "AUTH_PASSWORD", "admin")
    with TestClient(main.app, base_url="https://testserver") as client:
        page = client.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        favicon = client.get("/favicon.ico")
        assert favicon.status_code == 204
        assert "set-cookie" not in favicon.headers
        second_page = client.get("/login")
        assert re.search(r'name="csrf" value="([^"]+)"', second_page.text)[1] == csrf
        assert client.cookies["moss_login_csrf"] == csrf
        result = client.post("/login", data={"username": "admin", "password": "admin", "csrf": csrf}, follow_redirects=False)
        assert result.status_code == 303
        assert client.get("/api/notes").status_code == 200


def test_sessions_reject_tampering_expiry_and_changed_credentials() -> None:
    token = issue_token("deploy", "secret", "session", 600)
    assert valid_token(token, "deploy", "secret", "session")
    assert not valid_token(token + "x", "deploy", "secret", "session")
    assert not valid_token(token, "deploy", "new secret", "session")
    assert not valid_token(token, "deploy", "secret", "login")
    assert not valid_token(issue_token("deploy", "secret", "session", -1), "deploy", "secret", "session")
