from __future__ import annotations

import base64

import app.main as main
from fastapi.testclient import TestClient


def basic(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def test_auth_is_disabled_when_username_is_empty(monkeypatch) -> None:
    monkeypatch.setattr(main, "AUTH_USERNAME", "")
    monkeypatch.setattr(main, "AUTH_PASSWORD", "")
    assert main.valid_basic_auth(None)


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
