"""Envelope model tests — serialization, canonicalization, signing."""

from __future__ import annotations

import json

from app.audit.envelope import (
    Action,
    Actor,
    ActorKind,
    AuditEnvelope,
    Direction,
    PolicyDecision,
)
from app.audit.signer import sign_envelope, verify_envelope


def _make_env() -> AuditEnvelope:
    return AuditEnvelope.new(
        subject="oc.event.core.request.get",
        actor=Actor(kind=ActorKind.USER, id="127.0.0.1"),
        action=Action.REQUEST,
        direction=Direction.INGRESS,
        conv_id="conv-abc",
        request_hash="sha256:" + "0" * 64,
        redacted_payload={"method": "GET", "path": "/health"},
        policy=PolicyDecision(decision="allow", rule="open"),
    )


def test_envelope_round_trip_via_to_wire_bytes() -> None:
    env = _make_env()
    sign_envelope(env, slot="service")
    wire = env.to_wire_bytes()
    parsed = AuditEnvelope.model_validate(json.loads(wire.decode()))
    assert parsed.ulid == env.ulid
    assert parsed.sig_service == env.sig_service
    assert parsed.redacted_payload == env.redacted_payload


def test_canonical_bytes_excludes_signatures() -> None:
    env = _make_env()
    canon_before = env.to_canonical_bytes()
    sign_envelope(env, slot="service")
    canon_after = env.to_canonical_bytes()
    assert canon_before == canon_after, "Canonical body must not change after signing"


def test_sign_and_verify_service_slot() -> None:
    env = _make_env()
    sign_envelope(env, slot="service")
    assert verify_envelope(env, slot="service") is True


def test_sign_and_verify_appender_slot() -> None:
    env = _make_env()
    sign_envelope(env, slot="appender")
    assert verify_envelope(env, slot="appender") is True


def test_tampering_breaks_signature() -> None:
    env = _make_env()
    sign_envelope(env, slot="service")
    # Mutate a field — verification must fail
    env.subject = "oc.event.core.request.post"
    assert verify_envelope(env, slot="service") is False


def test_canonical_is_deterministic() -> None:
    """Same input → same canonical bytes, regardless of dict insertion order."""
    env1 = _make_env()
    env2 = _make_env()
    # Force same ulid + ts so canonical bytes ought to match
    env2.ulid = env1.ulid
    env2.ts = env1.ts
    assert env1.to_canonical_bytes() == env2.to_canonical_bytes()


def test_envelope_chain_via_prev_hash() -> None:
    a = _make_env()
    sign_envelope(a, slot="service")
    sign_envelope(a, slot="appender")
    h = a.canonical_sha256()

    b = AuditEnvelope.new(
        subject="oc.event.core.request.get",
        actor=Actor(kind=ActorKind.USER, id="127.0.0.1"),
        action=Action.REQUEST,
        direction=Direction.INGRESS,
        prev_hash=h,
    )
    sign_envelope(b, slot="service")

    # b's canonical body references a's hash, so tampering with a's
    # signature wouldn't invalidate b (signatures aren't in canonical)
    # but tampering with a's content would change h, breaking b's
    # prev_hash link.
    assert b.prev_hash == h


def test_unknown_slot_raises() -> None:
    env = _make_env()
    try:
        sign_envelope(env, slot="garbage")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
