"""Smoke tests for the auth subsystem.

These confirm the package imports, the FastAPI app boots with the auth
router attached, and the public endpoints return the expected shape.
They do NOT exercise a real WebAuthn ceremony (which needs a browser).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import db as auth_db
from app.main import app


def _use_tmp_db(monkeypatch) -> None:
    """Redirect the auth DB to a temp file so tests don't write into the
    real /var/lib path."""
    tmpdir = Path(tempfile.mkdtemp())
    auth_db._initialised = False  # force re-init against the new path
    monkeypatch.setattr(
        "app.config.settings.auth_db_path", tmpdir / "auth.db"
    )


def test_auth_health() -> None:
    client = TestClient(app)
    r = client.get("/auth/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["subsystem"] == "auth"


def test_register_begin_returns_options(monkeypatch) -> None:
    _use_tmp_db(monkeypatch)
    client = TestClient(app)
    r = client.post(
        "/auth/register/begin",
        json={"username": "smoke@example.test", "display_name": "Smoke User"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "challenge_id" in body
    assert "options" in body
    # WebAuthn options have these top-level fields per the spec
    options = body["options"]
    assert "challenge" in options
    assert "rp" in options
    assert "user" in options
    assert options["rp"]["id"]                  # rp_id non-empty
    assert options["user"]["name"] == "smoke@example.test"


def test_login_begin_usernameless_returns_options(monkeypatch) -> None:
    _use_tmp_db(monkeypatch)
    client = TestClient(app)
    # No username → usernameless / resident-key flow
    r = client.post("/auth/login/begin", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "challenge_id" in body
    assert "challenge" in body["options"]


def test_me_requires_token() -> None:
    client = TestClient(app)
    r = client.get("/auth/me")
    assert r.status_code == 401


def test_me_rejects_garbage_token() -> None:
    client = TestClient(app)
    r = client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401
