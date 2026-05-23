"""Smoke tests for the audit middleware.

These confirm the middleware doesn't break the request flow even when
NATS isn't reachable (the test environment has no oc-nats container).
Real end-to-end "publish lands on the bus" tests live alongside the
audit-appender service in Phase 2 task 2.8 (which can spin up an
embedded NATS or hit a CI-provided one).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


def test_health_endpoint_unaffected_by_audit_middleware() -> None:
    """/health is in the skip-list; middleware doesn't try to publish for it."""
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    # Skip-listed paths don't get the conv-id header
    assert "X-OC-Conv-Id" not in response.headers


def test_auth_endpoint_gets_conv_id_header() -> None:
    """Non-skip-list endpoints get the X-OC-Conv-Id stamp even when NATS publish fails."""
    client = TestClient(app)
    response = client.get("/auth/health")
    assert response.status_code == 200
    # The middleware should stamp the response with a conv id
    assert "X-OC-Conv-Id" in response.headers
    # And it should look like a UUID hex (32 chars, no dashes)
    cid = response.headers["X-OC-Conv-Id"]
    assert len(cid) == 32
    assert all(c in "0123456789abcdef" for c in cid)


def test_404_still_audited() -> None:
    """404s pass through the middleware too. Conv-id header should be present."""
    client = TestClient(app)
    response = client.get("/no-such-route-12345")
    assert response.status_code == 404
    assert "X-OC-Conv-Id" in response.headers


def test_post_with_body_does_not_break() -> None:
    """Audit middleware reads the body for hashing — handler must still see it."""
    client = TestClient(app)
    response = client.post(
        "/auth/register/begin",
        json={"username": "audit-test", "display_name": "Audit Test"},
    )
    # The route either accepts (200) or returns a 4xx from validation.
    # Either way, we should NOT see a 500 from the middleware eating
    # the body before the handler runs.
    assert response.status_code != 500
    assert "X-OC-Conv-Id" in response.headers
