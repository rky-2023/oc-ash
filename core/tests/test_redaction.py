"""Unit tests for the audit redaction pipeline (ADR-003 D6, task 2.7).

OPA and Vault are mocked so the suite needs neither a live policy engine nor
a live Vault — but the real crypto (AES-256-GCM) and the real drop/keep/
entropy/fail-closed logic are exercised.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.audit import redaction
from app.audit.envelope import Action, Actor, ActorKind, AuditEnvelope, Direction
from app.audit.redaction import (
    DROP_PLACEHOLDER,
    OPA_UNAVAILABLE_PLACEHOLDER,
    redact,
)

# DEK the fake Vault always hands out — lets tests decrypt the blob.
_FAKE_DEK = b"\x11" * 32


def _make_env(subject: str, payload: dict) -> AuditEnvelope:
    return AuditEnvelope.new(
        subject=subject,
        actor=Actor(kind=ActorKind.SERVICE, id="test"),
        action=Action.EVENT,
        direction=Direction.INTERNAL,
        conv_id="conv-1",
        redacted_payload=payload,
    )


class _FakeTransit:
    def generate_data_key(self, name, key_type, mount_point):  # noqa: ANN001
        return {
            "data": {
                "plaintext": base64.b64encode(_FAKE_DEK).decode(),
                "ciphertext": "vault:v1:ZmFrZS13cmFwcGVk",
            }
        }


class _FakeVault:
    class secrets:  # noqa: N801
        transit = _FakeTransit()


def _patch_opa(monkeypatch, decisions: dict[str, str]):
    async def _fake(payload, subject, action):  # noqa: ANN001
        return decisions

    monkeypatch.setattr(redaction, "_query_opa", _fake)


def _patch_vault(monkeypatch):
    # _encrypt_sync imports _get_vault_client from signer lazily.
    import app.audit.signer as signer

    monkeypatch.setattr(signer, "_get_vault_client", lambda: _FakeVault())


# ── Drop ─────────────────────────────────────────────────────────────


def test_drop_secret_is_unrecoverable(monkeypatch):
    _patch_opa(monkeypatch, {"api_key": "drop"})
    env = _make_env("oc.event.core.request.post", {"api_key": "sk_live_TOPSECRET"})
    asyncio.run(redact(env))
    assert env.redacted_payload["api_key"] == DROP_PLACEHOLDER
    # The original value must not survive ANYWHERE in the envelope.
    assert "sk_live_TOPSECRET" not in env.to_wire_bytes().decode()
    assert env.encrypted_blobs == []


# ── Encrypt ──────────────────────────────────────────────────────────


def test_encrypt_seals_and_round_trips(monkeypatch):
    _patch_opa(monkeypatch, {"body": "encrypt"})
    _patch_vault(monkeypatch)
    env = _make_env("oc.event.mail.received", {"body": "dear bob, the password is hunter2"})
    asyncio.run(redact(env))

    # Placeholder in the payload, real ciphertext in the blob.
    assert env.redacted_payload["body"].startswith("<encrypted:vault://transit/")
    assert len(env.encrypted_blobs) == 1
    blob = env.encrypted_blobs[0]
    assert blob.field == "body"
    assert "hunter2" not in env.to_wire_bytes().decode()

    # The plaintext is recoverable with the DEK + nonce (proves it's real GCM).
    pt = AESGCM(_FAKE_DEK).decrypt(
        base64.b64decode(blob.nonce), base64.b64decode(blob.ciphertext), None
    )
    assert pt.decode() == "dear bob, the password is hunter2"


# ── Keep ─────────────────────────────────────────────────────────────


def test_keep_preserves_value(monkeypatch):
    _patch_opa(monkeypatch, {"method": "keep", "path": "keep"})
    env = _make_env("oc.event.core.request.get", {"method": "GET", "path": "/health"})
    asyncio.run(redact(env))
    assert env.redacted_payload == {"method": "GET", "path": "/health"}
    assert env.encrypted_blobs == []


# ── Entropy net ──────────────────────────────────────────────────────


def test_entropy_net_drops_high_entropy_keep(monkeypatch):
    # OPA says keep, but the value is a long high-entropy blob → dropped in code.
    secret = "aZ9xQ2wE7rT4yU1iO6pL3kJ8hG5fD0sA"  # 32 chars, many distinct
    _patch_opa(monkeypatch, {"blob": "keep"})
    env = _make_env("oc.event.core.request.post", {"blob": secret})
    asyncio.run(redact(env))
    assert env.redacted_payload["blob"] == DROP_PLACEHOLDER
    assert secret not in env.to_wire_bytes().decode()


def test_entropy_net_keeps_short_lowentropy(monkeypatch):
    _patch_opa(monkeypatch, {"note": "keep"})
    env = _make_env("oc.event.core.request.post", {"note": "all good"})
    asyncio.run(redact(env))
    assert env.redacted_payload["note"] == "all good"


def test_telemetry_subject_skips_entropy_net(monkeypatch):
    # A git telemetry subject keeps a high-entropy SHA — the entropy net must
    # NOT fire there (it's metadata, not a secret).
    sha = "9f1c2a7e4b6d8c0f3a5e7b9d1c3f5a7e9b1d3f5a"  # 40-char hex
    _patch_opa(monkeypatch, {"sha": "keep"})
    env = _make_env("oc.event.git.openclaw.post-commit", {"sha": sha})
    asyncio.run(redact(env))
    assert env.redacted_payload["sha"] == sha
    assert env.encrypted_blobs == []


def test_nontelemetry_subject_still_drops_high_entropy(monkeypatch):
    # Same high-entropy value on a NON-telemetry subject is still dropped.
    sha = "9f1c2a7e4b6d8c0f3a5e7b9d1c3f5a7e9b1d3f5a"
    _patch_opa(monkeypatch, {"sha": "keep"})
    env = _make_env("oc.event.core.request.post", {"sha": sha})
    asyncio.run(redact(env))
    assert env.redacted_payload["sha"] == DROP_PLACEHOLDER


def test_telemetry_subject_still_drops_named_secret(monkeypatch):
    # Defense-in-depth: a field OPA classified "drop" is still dropped even on
    # a telemetry subject (the entropy skip only affects the keep→drop net).
    _patch_opa(monkeypatch, {"api_key": "drop", "sha": "keep"})
    env = _make_env(
        "oc.event.claude.PreToolUse",
        {"api_key": "sk_live_LEAK", "sha": "9f1c2a7e4b6d8c0f3a5e7b9d1c3f5a7e9b1d3f5a"},
    )
    asyncio.run(redact(env))
    assert env.redacted_payload["api_key"] == DROP_PLACEHOLDER
    assert "sk_live_LEAK" not in env.to_wire_bytes().decode()


# ── Fail-closed ──────────────────────────────────────────────────────


def test_fail_closed_when_opa_unavailable(monkeypatch):
    async def _boom(payload, subject, action):  # noqa: ANN001
        raise RuntimeError("opa down")

    monkeypatch.setattr(redaction, "_query_opa", _boom)
    env = _make_env("oc.event.core.request.post", {"a": "1", "b": "2"})
    asyncio.run(redact(env))
    # Every field dropped — never persist raw on OPA failure.
    assert env.redacted_payload == {"a": OPA_UNAVAILABLE_PLACEHOLDER, "b": OPA_UNAVAILABLE_PLACEHOLDER}
    assert env.encrypted_blobs == []


def test_encrypt_fail_closed_drops(monkeypatch):
    # OPA says encrypt, but Vault is unavailable → drop, not raw persist.
    _patch_opa(monkeypatch, {"body": "encrypt"})
    import app.audit.signer as signer

    monkeypatch.setattr(signer, "_get_vault_client", lambda: None)
    env = _make_env("oc.event.mail.received", {"body": "leak me"})
    asyncio.run(redact(env))
    assert env.redacted_payload["body"] == DROP_PLACEHOLDER
    assert "leak me" not in env.to_wire_bytes().decode()


# ── Version pin ──────────────────────────────────────────────────────


def test_redaction_version_is_pinned(monkeypatch):
    # Point at the real repo rego so we get a concrete hash, not "unknown".
    rego = Path(__file__).resolve().parents[2] / "policy" / "redaction.rego"
    monkeypatch.setattr(redaction.settings, "redaction_policy_path", rego)
    redaction._policy_version.cache_clear()
    _patch_opa(monkeypatch, {"method": "keep"})
    env = _make_env("oc.event.core.request.get", {"method": "GET"})
    asyncio.run(redact(env))
    assert env.policy is not None
    assert env.policy.redaction_version is not None
    assert env.policy.redaction_version.startswith("sha256:")
    assert env.policy.redaction_version != "sha256:unknown"
    redaction._policy_version.cache_clear()


def test_empty_payload_still_pins_version(monkeypatch):
    _patch_opa(monkeypatch, {})
    env = _make_env("oc.event.core.request.get", {})
    asyncio.run(redact(env))
    assert env.policy is not None
    assert env.policy.redaction_version is not None
