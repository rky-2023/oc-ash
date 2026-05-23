"""Audit envelope signing.

MVP scope (Phase 2 task 2.5): process-local HMAC-SHA256 with a
randomly-generated 32-byte key. Restart invalidates all signatures —
acceptable for MVP because:
  - There's no historical state to lose (Phase 2 task 2.13's
    attestation publisher isn't online yet, so nothing has been
    externally witnessed).
  - The next-PR target is to swap in Vault transit signing
    (transit/sign/audit-service per ADR-002 D12), at which point
    the signing key persists across restarts AND auto-rotates monthly.

Same trade-off pattern as core/app/auth/sessions.py — local key for
MVP, Vault transit for real.

Verification: present so callers can sanity-check signatures
round-trip during development. Real cross-service verification
(by the audit-projector validating sig_service + sig_appender)
arrives in Phase 2 task 2.9.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from app.audit.envelope import AuditEnvelope

_KEY: bytes = secrets.token_bytes(32)
_ALG = "hmac-sha256"


def _hmac_hex(payload: bytes) -> str:
    return hmac.new(_KEY, payload, hashlib.sha256).hexdigest()


def sign_envelope(env: AuditEnvelope, slot: str = "service") -> AuditEnvelope:
    """Sign and attach the signature in the requested slot.

    slot: "service" → fills sig_service
          "appender" → fills sig_appender

    Returns the (mutated) envelope.
    """
    sig = f"{_ALG}:{_hmac_hex(env.to_canonical_bytes())}"
    if slot == "service":
        env.sig_service = sig
    elif slot == "appender":
        env.sig_appender = sig
    else:
        raise ValueError(f"Unknown signature slot: {slot!r}")
    return env


def verify_envelope(env: AuditEnvelope, slot: str = "service") -> bool:
    """Check the signature in the given slot against the canonical body."""
    raw = env.sig_service if slot == "service" else env.sig_appender if slot == "appender" else None
    if raw is None:
        return False
    try:
        alg, hex_sig = raw.split(":", 1)
    except ValueError:
        return False
    if alg != _ALG:
        return False
    expected = _hmac_hex(env.to_canonical_bytes())
    return hmac.compare_digest(expected, hex_sig)
