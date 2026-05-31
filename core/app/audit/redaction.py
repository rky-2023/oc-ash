"""Audit redaction pipeline (ADR-003 D6, Phase 2 task 2.7).

Runs in the audit-appender, AFTER sig_service is verified and BEFORE the
envelope is chained, appender-signed, and written to the immudb WORM ledger.
This ordering is the whole point of D6: a redactable audit log cannot be
append-only, so redaction must happen before the bytes become immutable.

Each top-level field of `redacted_payload` gets one of three outcomes, the
bulk decided by OPA (`policy/redaction.rego`, version-pinned per envelope):

  drop    → "<redacted:secret>"; original value is gone forever.
  encrypt → value sealed with a per-message AES-256-GCM DEK that is itself
            wrapped by Vault transit `audit-pii-v3`; field shows
            "<encrypted:vault://transit/audit-pii-v3>" and an EncryptedBlob is
            appended. Reveal is YubiKey-gated (ADR-002 D5).
  keep    → stored verbatim.

Two safety nets beyond OPA:
  - Shannon-entropy drop: rego has no log() builtin, so the high-entropy
    heuristic from task 2.7 ("entropy + length >= 20") lives here in code.
  - Fail-closed: if OPA is unreachable, ALL fields are dropped rather than
    risk writing raw secrets into an immutable ledger. Never fail open.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
from functools import lru_cache
from typing import Any

import httpx
import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.audit.envelope import AuditEnvelope, EncryptedBlob, PolicyDecision
from app.config import settings

log = structlog.get_logger(__name__)

DROP_PLACEHOLDER = "<redacted:secret>"
OPA_UNAVAILABLE_PLACEHOLDER = "<redacted:opa-unavailable>"

# High-entropy heuristic: a string at least this long whose Shannon entropy
# (bits/char) meets the threshold is treated as a secret and dropped, even if
# its key name didn't match the rego secret pattern.
_ENTROPY_MIN_LEN = 20
_ENTROPY_BITS_PER_CHAR = 3.5

# OPA call budget — redaction is on the append hot path but must not hang it.
_OPA_TIMEOUT_SECONDS = 2.0


# ── Policy version pin ───────────────────────────────────────────────


@lru_cache(maxsize=1)
def _policy_version() -> str:
    """sha256 of redaction.rego, so every envelope records which policy
    version made its redaction decision (ADR-003 D6)."""
    try:
        data = settings.redaction_policy_path.read_bytes()
        return "sha256:" + hashlib.sha256(data).hexdigest()
    except Exception as e:
        log.warning("redaction.policy_version_unreadable", path=str(settings.redaction_policy_path), err=str(e))
        return "sha256:unknown"


def _stamp_policy(existing: PolicyDecision | None, version: str) -> PolicyDecision:
    if existing is not None:
        existing.redaction_version = version
        return existing
    return PolicyDecision(decision="n/a", redaction_version=version)


# ── Entropy net ──────────────────────────────────────────────────────


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _is_high_entropy(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) >= _ENTROPY_MIN_LEN
        and _shannon_entropy(value) >= _ENTROPY_BITS_PER_CHAR
    )


# ── OPA query ────────────────────────────────────────────────────────


async def _query_opa(payload: dict[str, Any], subject: str, action: str) -> dict[str, str]:
    """Return {field: "drop"|"encrypt"|"keep"} from the redaction policy.

    Raises on transport/HTTP error so the caller can fail closed.
    """
    url = f"{settings.opa_url.rstrip('/')}/v1/data/openclaw/redaction/decisions"
    body = {"input": {"subject": subject, "action": action, "payload": payload}}
    async with httpx.AsyncClient(timeout=_OPA_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        return resp.json().get("result", {}) or {}


# ── Encrypt path (envelope encryption: AES-256-GCM + Vault-wrapped DEK) ─


def _encrypt_sync(field: str, plaintext: bytes) -> EncryptedBlob:
    """Generate a per-message DEK via Vault transit, AES-256-GCM seal the
    value locally, and store the Vault-wrapped DEK. Synchronous (Vault +
    crypto); call via asyncio.to_thread."""
    # Imported here to reuse the signer's configured Vault client (TLS verify,
    # token) without a circular import at module load.
    from app.audit.signer import _get_vault_client

    client = _get_vault_client()
    if client is None:
        raise RuntimeError("no Vault client for PII encryption")

    key = settings.vault_transit_key_pii
    dk = client.secrets.transit.generate_data_key(
        name=key, key_type="plaintext", mount_point="transit"
    )
    dek = base64.b64decode(dk["data"]["plaintext"])
    wrapped_dek = dk["data"]["ciphertext"]  # "vault:v1:<base64>"
    nonce = secrets.token_bytes(12)
    ct = AESGCM(dek).encrypt(nonce, plaintext, None)
    return EncryptedBlob(
        field=field,
        key_id=key,
        ciphertext=base64.b64encode(ct).decode("ascii"),
        wrapped_dek=wrapped_dek,
        nonce=base64.b64encode(nonce).decode("ascii"),
    )


async def _encrypt_field(field: str, value: Any) -> EncryptedBlob | None:
    import asyncio

    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    try:
        return await asyncio.to_thread(_encrypt_sync, field, raw.encode("utf-8"))
    except Exception as e:
        log.error("redaction.encrypt_failed", field=field, err=str(e), note="failing closed → drop")
        return None


# ── Public API ───────────────────────────────────────────────────────


async def redact(env: AuditEnvelope) -> AuditEnvelope:
    """Redact env.redacted_payload in place per policy; stamp the policy
    version; populate encrypted_blobs. Returns the same envelope."""
    version = _policy_version()
    payload = env.redacted_payload

    if not payload:
        env.policy = _stamp_policy(env.policy, version)
        return env

    # Test/dev short-circuit: pin the version but make no OPA/Vault calls.
    if not settings.redaction_enabled:
        env.policy = _stamp_policy(env.policy, version)
        return env

    try:
        decisions = await _query_opa(payload, env.subject, env.action.value)
    except Exception as e:
        log.error(
            "redaction.opa_unavailable",
            err=str(e),
            note="fail-closed: dropping all payload fields rather than persist raw",
        )
        env.redacted_payload = {k: OPA_UNAVAILABLE_PLACEHOLDER for k in payload}
        env.encrypted_blobs = []
        env.policy = _stamp_policy(env.policy, version)
        return env

    new_payload: dict[str, Any] = {}
    blobs: list[EncryptedBlob] = []
    for field, value in payload.items():
        decision = decisions.get(field, "keep")
        # Entropy net: a "keep" field that looks like a secret is dropped.
        if decision == "keep" and _is_high_entropy(value):
            decision = "drop"

        if decision == "drop":
            new_payload[field] = DROP_PLACEHOLDER
        elif decision == "encrypt":
            blob = await _encrypt_field(field, value)
            if blob is None:
                new_payload[field] = DROP_PLACEHOLDER  # fail closed
            else:
                blobs.append(blob)
                new_payload[field] = f"<encrypted:vault://transit/{settings.vault_transit_key_pii}>"
        else:
            new_payload[field] = value

    env.redacted_payload = new_payload
    env.encrypted_blobs = blobs
    env.policy = _stamp_policy(env.policy, version)
    return env
