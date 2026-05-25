"""Unit tests for the audit envelope signer (HMAC fallback path).

These tests run with OC_VAULT_SIGNING_ENABLED=false (set in conftest.py)
so they don't need a live Vault instance.
"""

from __future__ import annotations

import asyncio
import copy

import pytest

from app.audit.envelope import Action, Actor, ActorKind, AuditEnvelope, Direction
from app.audit.signer import _reset_vault_client, sign_envelope, verify_envelope


def _make_env() -> AuditEnvelope:
    return AuditEnvelope.new(
        subject="oc.event.test.request",
        actor=Actor(kind=ActorKind.SERVICE, id="test"),
        action=Action.REQUEST,
        direction=Direction.INGRESS,
        conv_id="abc123",
    )


def test_sign_and_verify_service_slot() -> None:
    env = _make_env()
    asyncio.run(sign_envelope(env, slot="service"))
    assert env.sig_service
    assert env.sig_service.startswith("hmac-sha256:")
    assert asyncio.run(verify_envelope(env, slot="service"))


def test_sign_and_verify_appender_slot() -> None:
    env = _make_env()
    asyncio.run(sign_envelope(env, slot="appender"))
    assert env.sig_appender
    assert asyncio.run(verify_envelope(env, slot="appender"))


def test_tampered_payload_fails_verify() -> None:
    env = _make_env()
    asyncio.run(sign_envelope(env, slot="service"))
    # Mutate after signing → canonical bytes differ → sig invalid
    env.subject = "oc.event.test.tampered"
    assert not asyncio.run(verify_envelope(env, slot="service"))


def test_slots_are_independent() -> None:
    env = _make_env()
    asyncio.run(sign_envelope(env, slot="service"))
    asyncio.run(sign_envelope(env, slot="appender"))
    # Each slot verifies with its own HMAC — they use the same key but
    # cover the full canonical body (which doesn't include the sigs).
    assert asyncio.run(verify_envelope(env, slot="service"))
    assert asyncio.run(verify_envelope(env, slot="appender"))


def test_missing_sig_returns_false() -> None:
    env = _make_env()
    # Neither slot signed yet
    assert not asyncio.run(verify_envelope(env, slot="service"))
    assert not asyncio.run(verify_envelope(env, slot="appender"))


def test_unknown_slot_raises() -> None:
    env = _make_env()
    with pytest.raises(ValueError, match="Unknown signature slot"):
        asyncio.run(sign_envelope(env, slot="bogus"))


def test_unknown_sig_format_returns_false() -> None:
    env = _make_env()
    env.sig_service = "totally-unknown:deadbeef"
    assert not asyncio.run(verify_envelope(env, slot="service"))
