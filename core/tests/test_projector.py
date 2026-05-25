"""Unit tests for the audit projector (structural / lifecycle only).

These tests do NOT need a live Postgres or immudb — they verify the
projector's module structure, singleton behaviour, and that the class
exposes the expected interface. Integration tests belong in a separate
e2e suite that runs against the full docker-compose stack.
"""

from __future__ import annotations

from app.audit.projector import AuditProjector, get_projector


def test_get_projector_returns_singleton() -> None:
    p1 = get_projector()
    p2 = get_projector()
    assert p1 is p2


def test_projector_is_stopped_initially() -> None:
    p = AuditProjector()
    assert p._task is None
    assert p._pool is None


def test_projector_stop_is_idempotent_when_not_started() -> None:
    import asyncio
    p = AuditProjector()
    asyncio.run(p.stop())  # should not raise


def test_projector_start_requires_dsn() -> None:
    """start() with an invalid DSN should raise (asyncpg rejects it)."""
    import asyncio
    import pytest
    p = AuditProjector()
    with pytest.raises(Exception):
        asyncio.run(p.start("postgresql://bad-host-that-does-not-exist/db", poll_seconds=1))
