"""Audit envelope — Pydantic model per ADR-003 D2.

Every message worth auditing in openclaw flows through this shape.
Each envelope is:
  - ULID-keyed for sortable ordering
  - Chained via prev_hash to its predecessor in immudb (defense in depth
    beyond immudb's own Merkle tree)
  - Double-signed: sig_service (from the originating service) and
    sig_appender (from the audit appender that writes to immudb)
  - Self-describing via subject (NATS subject pattern oc.<kind>.<…>)

For canonical serialization (used as the body that gets hashed and
signed), we use JSON with sort_keys=True, no whitespace. This is the
ONLY format the signature covers — anything else is for display.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import ulid
from pydantic import BaseModel, ConfigDict, Field


class ActorKind(str, Enum):
    """Who or what is the origin of this event."""

    USER = "user"
    AGENT = "agent"
    MCP = "mcp"
    SERVICE = "service"
    GITHUB = "github"
    EXTERNAL = "external"


class Action(str, Enum):
    """What kind of operation this envelope describes."""

    REQUEST = "request"
    RESPONSE = "response"
    EVENT = "event"
    DECISION = "decision"


class Direction(str, Enum):
    """Travel direction relative to the originating service."""

    INGRESS = "ingress"
    EGRESS = "egress"
    INTERNAL = "internal"


class Actor(BaseModel):
    """Identity of the originating party."""

    model_config = ConfigDict(extra="forbid")

    kind: ActorKind
    id: str = Field(min_length=1, max_length=256)
    jwt_jti: str | None = Field(default=None, description="JTI of the request's JWT, if any.")


class PolicyDecision(BaseModel):
    """Result of an OPA policy evaluation (Phase 4)."""

    model_config = ConfigDict(extra="forbid")

    decision: str = Field(description='"allow" | "deny" | "n/a"')
    rule: str | None = None
    input_hash: str | None = None
    # sha256 of the redaction.rego that made the redaction decision, so we
    # can prove which policy version produced this envelope (ADR-003 D6).
    redaction_version: str | None = None


class EncryptedBlob(BaseModel):
    """A field that was kept but encrypted under a Vault transit key (ADR-003 D6).

    Envelope encryption: the field value is sealed with a per-message AES-256-GCM
    data-encryption key (DEK); that DEK is itself wrapped by the Vault transit key
    named in `key_id`. Reveal = unwrap the DEK via Vault (YubiKey-gated, ADR-002 D5)
    then AES-GCM-decrypt `ciphertext` with `nonce`.
    """

    model_config = ConfigDict(extra="forbid")

    field: str
    key_id: str  # Vault transit key that wrapped the DEK, e.g. "audit-pii-v3"
    ciphertext: str  # base64(AES-256-GCM ciphertext + tag)
    wrapped_dek: str  # Vault-wrapped DEK, "vault:v1:<base64>"
    nonce: str  # base64(96-bit GCM nonce)


# Fields the audit-appender assigns or mutates AFTER the originating service
# has signed (chain position + redaction outputs). sig_service is computed
# with these excluded; sig_appender and the chain hash cover them. See
# to_canonical_bytes() and ADR-003 D3/D6.
_APPENDER_OWNED_FIELDS = {"prev_hash", "redacted_payload", "encrypted_blobs", "policy"}


class AuditEnvelope(BaseModel):
    """The signed, immutable record of one auditable event.

    All fields except the two signatures are covered by both signatures.
    Signatures are added by `signer.sign_envelope()` and validated by
    `signer.verify_envelope()`.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # ── Identity / timing ──────────────────────────────────────────────
    ulid: str = Field(description="Lexicographically sortable id; also encodes the timestamp.")
    ts: datetime = Field(description="UTC ISO8601 with ms precision.")
    subject: str = Field(min_length=1, description="NATS subject pattern, e.g., oc.event.core.request")

    # ── Context ────────────────────────────────────────────────────────
    conv_id: str | None = Field(default=None, description="Conversation id if part of an A2A flow.")
    actor: Actor
    action: Action
    direction: Direction

    # ── Hashes ─────────────────────────────────────────────────────────
    request_hash: str | None = Field(default=None, description='"sha256:<hex>" of canonical request body.')
    response_hash: str | None = Field(default=None, description='"sha256:<hex>" of canonical response body.')

    # ── Policy + payload ───────────────────────────────────────────────
    policy: PolicyDecision | None = None
    redacted_payload: dict[str, Any] | None = None
    encrypted_blobs: list[EncryptedBlob] = Field(default_factory=list)

    # ── Chain ──────────────────────────────────────────────────────────
    prev_hash: str | None = Field(default=None, description="Hash of the previous envelope's canonical body.")

    # ── Signatures ─────────────────────────────────────────────────────
    sig_service: str | None = Field(default=None, description='"<alg>:<hex>" signature by originating service.')
    sig_appender: str | None = Field(default=None, description='"<alg>:<hex>" signature by audit appender.')

    # ── Helpers ────────────────────────────────────────────────────────
    @classmethod
    def new(
        cls,
        *,
        subject: str,
        actor: Actor,
        action: Action,
        direction: Direction,
        conv_id: str | None = None,
        request_hash: str | None = None,
        response_hash: str | None = None,
        policy: PolicyDecision | None = None,
        redacted_payload: dict[str, Any] | None = None,
        encrypted_blobs: list[EncryptedBlob] | None = None,
        prev_hash: str | None = None,
        ts: datetime | None = None,
    ) -> "AuditEnvelope":
        """Construct a fresh, unsigned envelope with a new ULID."""
        now = ts or datetime.now(timezone.utc)
        return cls(
            # python-ulid API (ulid.ULID); from_datetime keeps the ULID's
            # encoded timestamp consistent with the `ts` field below.
            ulid=str(ulid.ULID.from_datetime(now)),
            ts=now,
            subject=subject,
            conv_id=conv_id,
            actor=actor,
            action=action,
            direction=direction,
            request_hash=request_hash,
            response_hash=response_hash,
            policy=policy,
            redacted_payload=redacted_payload,
            encrypted_blobs=encrypted_blobs or [],
            prev_hash=prev_hash,
        )

    def to_canonical_bytes(self, slot: str | None = None) -> bytes:
        """JSON-serialize this envelope MINUS its signature fields, in a
        deterministic byte-for-byte form. This is what signatures cover.

        Used by signer.sign_envelope() (input to HMAC/sign) and by
        verifier (recompute and compare).

        `slot="service"` additionally drops the fields the appender
        assigns or mutates AFTER the originating service signs: the chain
        position (`prev_hash`) and the redaction outputs (`redacted_payload`,
        `encrypted_blobs`, `policy`). The service signs pre-chain,
        pre-redaction (ADR-003 D3/D6), so sig_service must not cover them —
        otherwise the appender's later mutation invalidates it.

        `slot="appender"` / `slot=None` covers the full final form (only the
        two signature fields are excluded). The chain hash (`canonical_sha256`)
        uses this full basis, so the prev_hash chain anchors the redacted entry.
        """
        excluded = {"sig_service", "sig_appender"}
        if slot == "service":
            excluded = excluded | _APPENDER_OWNED_FIELDS
        d = self.model_dump(mode="json", exclude=excluded)
        # canonical: sorted keys, compact separators, ms precision in `ts`
        return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def to_wire_bytes(self) -> bytes:
        """JSON serialization for NATS publish — includes signatures."""
        d = self.model_dump(mode="json")
        return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def canonical_sha256(self) -> str:
        """Hex digest of to_canonical_bytes() — used to chain prev_hash."""
        return "sha256:" + hashlib.sha256(self.to_canonical_bytes()).hexdigest()
