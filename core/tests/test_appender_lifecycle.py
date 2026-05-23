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
