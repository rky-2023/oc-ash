"""Audit envelope signing.

Production: Vault transit Ed25519 keys (ADR-002 D12).
  sig_service  → transit/sign/audit-service   (stamped by core middleware)
  sig_appender → transit/sign/audit-appender  (stamped by audit-appender)

Fallback (OC_VAULT_SIGNING_ENABLED=false): process-local HMAC-SHA256.
The fallback exists only for unit tests. A live deployment with an
empty OC_VAULT_TOKEN will log loudly and write an empty sig; the
projector will flag those envelopes during integrity sweeps.

Signature wire format on the envelope:
  "ed25519-transit:<vault:v1:BASE64>"   — Vault transit Ed25519
  "hmac-sha256:<HEX>"                   — HMAC fallback (tests only)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import hvac as _hvac_t

from app.audit.envelope import AuditEnvelope

log = structlog.get_logger(__name__)

# ── HMAC fallback ────────────────────────────────────────────────────
# Regenerated on each process start; intentionally ephemeral.
_HMAC_KEY: bytes = secrets.token_bytes(32)
_HMAC_ALG = "hmac-sha256"


def _hmac_sign(payload: bytes) -> str:
    return hmac.new(_HMAC_KEY, payload, hashlib.sha256).hexdigest()


def _hmac_verify(payload: bytes, hex_sig: str) -> bool:
    return hmac.compare_digest(_hmac_sign(payload), hex_sig)


# ── Vault transit helpers (sync, called via asyncio.to_thread) ───────


def _vault_sign_sync(client: "_hvac_t.Client", key_name: str, payload: bytes) -> str:
    result = client.secrets.transit.sign_data(
        name=key_name,
        hash_input=base64.b64encode(payload).decode("ascii"),
        mount_point="transit",
    )
    return result["data"]["signature"]  # "vault:v1:<base64>"


def _vault_verify_sync(
    client: "_hvac_t.Client", key_name: str, payload: bytes, vault_sig: str
) -> bool:
    try:
        result = client.secrets.transit.verify_signed_data(
            name=key_name,
            hash_input=base64.b64encode(payload).decode("ascii"),
            signature=vault_sig,
            mount_point="transit",
        )
        return bool(result["data"]["valid"])
    except Exception as e:
        log.warning("vault.transit.verify_error", key=key_name, err=str(e))
        return False


# ── Singleton Vault client ───────────────────────────────────────────
# Created lazily on first sign/verify call. One client per process.
# The token comes from OC_VAULT_TOKEN (set by run-with-vault-creds.sh).
# Token expiry → sign calls fail loudly; restart service to refresh.
# Vault-agent sidecar (task 1.12) handles renewal in production.

_vault_client: "_hvac_t.Client | None" = None


def _get_vault_client() -> "_hvac_t.Client | None":
    global _vault_client
    if _vault_client is not None:
        return _vault_client
    from app.config import settings
    if not settings.vault_signing_enabled or not settings.vault_token:
        return None
    import hvac
    _vault_client = hvac.Client(url=settings.vault_addr, token=settings.vault_token)
    return _vault_client


def _reset_vault_client() -> None:
    """Force re-init on next call. Used in tests that swap settings."""
    global _vault_client
    _vault_client = None


def _slot_key(slot: str) -> str:
    from app.config import settings
    if slot == "service":
        return settings.vault_transit_key_service
    if slot == "appender":
        return settings.vault_transit_key_appender
    raise ValueError(f"Unknown signature slot: {slot!r}")


# ── Public async API ─────────────────────────────────────────────────


async def sign_envelope(env: AuditEnvelope, slot: str = "service") -> AuditEnvelope:
    """Sign and attach the signature in the requested slot.

    Writes sig_service or sig_appender on the envelope in-place and
    returns it. An empty string is written if Vault signing fails so
    the envelope is still emitted (with a flagged integrity state).
    """
    payload = env.to_canonical_bytes()
    client = _get_vault_client()

    if client is not None:
        key = _slot_key(slot)
        try:
            vault_sig = await asyncio.to_thread(_vault_sign_sync, client, key, payload)
            sig = f"ed25519-transit:{vault_sig}"
        except Exception as e:
            log.error(
                "vault.transit.sign_failed",
                slot=slot,
                key=key,
                err=str(e),
                note="envelope will carry empty sig; projector will flag it",
            )
            sig = ""
    else:
        sig = f"{_HMAC_ALG}:{_hmac_sign(payload)}"

    if slot == "service":
        env.sig_service = sig
    elif slot == "appender":
        env.sig_appender = sig
    return env


async def verify_envelope(env: AuditEnvelope, slot: str = "service") -> bool:
    """Verify the signature in the given slot. Returns False on any failure."""
    raw = (
        env.sig_service if slot == "service"
        else env.sig_appender if slot == "appender"
        else None
    )
    if not raw:
        return False

    payload = env.to_canonical_bytes()

    if raw.startswith("ed25519-transit:"):
        vault_sig = raw[len("ed25519-transit:"):]
        client = _get_vault_client()
        if client is None:
            log.warning("vault.transit.verify_skipped", slot=slot, reason="no vault client")
            return False
        key = _slot_key(slot)
        return await asyncio.to_thread(_vault_verify_sync, client, key, payload, vault_sig)

    if raw.startswith(f"{_HMAC_ALG}:"):
        _, hex_sig = raw.split(":", 1)
        return _hmac_verify(payload, hex_sig)

    log.warning("vault.transit.unknown_sig_format", slot=slot, prefix=raw[:20])
    return False
