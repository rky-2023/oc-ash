"""Smoke tests for the audit-appender's lifecycle.

End-to-end tests (envelope lands in immudb) require a live oc-immudb
and oc-nats — covered in integration tests under tests/integration/
(future). These tests verify the module's lifecycle plumbing without
talking to anything external.
"""

from __future__ import annotations

import pytest

from app.audit.appender import get_appender


@pytest.mark.asyncio
async def test_appender_stop_on_never_started_is_noop() -> None:
    appender = get_appender()
    # stop() on an appender that never start()ed should be a no-op
    # and not raise.
    await appender.stop()
    assert appender._task is None


def test_appender_subjects_match_adr_taxonomy() -> None:
    """The appender consumes the audit-relevant subjects per ADR-001 D3."""
    from app.audit.appender import SUBJECTS

    assert "oc.event.>" in SUBJECTS
    assert "oc.a2a.>" in SUBJECTS
    assert "oc.mcp.>" in SUBJECTS
    assert "oc.notify.>" in SUBJECTS
    # health pings are operational, not auditable
    assert "oc.health.>" not in SUBJECTS


def test_audit_envelope_canonical_chain_helper() -> None:
    """canonical_sha256 returns a 'sha256:<hex>' string the appender uses
    for chain-head tracking."""
    from app.audit.envelope import (
        Action,
        Actor,
        ActorKind,
        AuditEnvelope,
        Direction,
    )

    env = AuditEnvelope.new(
        subject="oc.event.test",
        actor=Actor(kind=ActorKind.USER, id="x"),
        action=Action.EVENT,
        direction=Direction.INTERNAL,
    )
    h = env.canonical_sha256()
    assert h.startswith("sha256:")
    assert len(h) == 7 + 64  # "sha256:" + 64 hex chars


@pytest.mark.asyncio
async def test_seed_chain_head_recovers_hash_from_immudb(monkeypatch) -> None:
    """On restart, _seed_chain_head must recompute the latest entry's
    canonical_sha256 so the chain survives a restart (no spurious break)."""
    from app.audit import appender as appender_mod
    from app.audit.appender import AuditAppender
    from app.audit.envelope import (
        Action,
        Actor,
        ActorKind,
        AuditEnvelope,
        Direction,
    )

    prior = AuditEnvelope.new(
        subject="oc.event.test",
        actor=Actor(kind=ActorKind.USER, id="x"),
        action=Action.EVENT,
        direction=Direction.INTERNAL,
    )

    class _FakeWriter:
        is_connected = True

        async def get_latest_value(self) -> bytes:
            return prior.to_wire_bytes()

    monkeypatch.setattr(appender_mod, "get_writer", lambda: _FakeWriter())

    app = AuditAppender()
    assert app._chain_head is None
    await app._seed_chain_head()
    assert app._chain_head == prior.canonical_sha256()


@pytest.mark.asyncio
async def test_seed_chain_head_empty_ledger_leaves_none(monkeypatch) -> None:
    """Empty ledger (or disconnected writer) → _chain_head stays None
    (first envelope opens a fresh root); startup must not raise."""
    from app.audit import appender as appender_mod
    from app.audit.appender import AuditAppender

    class _EmptyWriter:
        is_connected = True

        async def get_latest_value(self) -> None:
            return None

    monkeypatch.setattr(appender_mod, "get_writer", lambda: _EmptyWriter())

    app = AuditAppender()
    await app._seed_chain_head()
    assert app._chain_head is None
