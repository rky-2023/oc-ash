"""Shared ingest publish path.

Every ingested event (gh-poller, claude hooks) funnels through emit_event:
  1. OPA ingest filter (keep/drop) — fail-open.
  2. Build an AuditEnvelope (subject + actor + payload).
  3. Sign sig_service (Vault transit) — the appender verifies this slot.
  4. Publish via JetStream with the ULID as msg_id (dedupe).

The appender then redacts (ADR-003 D6), signs sig_appender, chains, and
writes to immudb; the projector materialises it to Postgres. So ingested
payloads are subject to the same redaction guarantees as everything else.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.audit.envelope import Action, Actor, ActorKind, AuditEnvelope, Direction
from app.audit.signer import sign_envelope
from app.bus.nats_client import get_bus
from app.ingest.filter import should_keep

log = structlog.get_logger(__name__)


async def emit_event(
    *,
    subject: str,
    source: str,
    actor_id: str,
    payload: dict[str, Any],
    filter_fields: dict[str, Any] | None = None,
    conv_id: str | None = None,
) -> bool:
    """Filter, build, sign, and publish one ingested event.

    Returns True if published, False if dropped by the filter. Signing or
    publishing failures raise — the caller decides whether to retry (the
    poller backs off; the hook receiver returns 5xx so the hook can log).
    """
    if not await should_keep(source, **(filter_fields or {})):
        return False

    env = AuditEnvelope.new(
        subject=subject,
        actor=Actor(kind=ActorKind.SERVICE, id=actor_id),
        action=Action.EVENT,
        direction=Direction.INGRESS,
        conv_id=conv_id,
        redacted_payload=payload,
    )
    await sign_envelope(env, slot="service")

    bus = get_bus()
    await bus.publish(subject, env.to_wire_bytes(), msg_id=env.ulid)
    log.debug("ingest.emit.published", subject=subject, source=source, ulid=env.ulid)
    return True
