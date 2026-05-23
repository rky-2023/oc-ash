"""Smoke tests for the health endpoints.

These confirm the scaffold is importable and the FastAPI app boots.
They do NOT verify downstream dependency readiness; that happens in
Phase 2 task 2.15 (full Phase 2 smoke).
"""

from fastapi.testclient import TestClient

from app.main import app


def test_health_liveness() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["service"] == "openclaw-core"
    assert body["version"] == "0.1.0"


def test_health_deps_structure() -> None:
    client = TestClient(app)
    response = client.get("/health/deps")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "deps" in body
    # Every expected dep must appear, even if its status is "not-yet-wired".
    for dep in ("nats", "immudb", "postgres", "vault", "opa"):
        assert dep in body["deps"]


def test_app_metadata() -> None:
    assert app.title == "openclaw-core"
    assert app.version == "0.1.0"
