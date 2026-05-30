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


class EncryptedBlob(BaseModel):
    """A field that was kept but encrypted under a Vault transit key (ADR-003 D6)."""

    model_config = ConfigDict(extra="forbid")

    field: str
    key_id: str
    ciphertext: str  # base64


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

    def to_canonical_bytes(self, exclude_prev_hash: bool = False) -> bytes:
        """JSON-serialize this envelope MINUS its signature fields, in a
        deterministic byte-for-byte form. This is what signatures cover.

        Used by signer.sign_envelope() (input to HMAC/sign) and by
        verifier (recompute and compare).

        `exclude_prev_hash=True` additionally drops `prev_hash`: the
        originating service signs BEFORE the appender assigns the chain
        position, so sig_service must not cover prev_hash (otherwise the
        appender's later mutation invalidates it). The appender signature
        and the chain hash DO cover prev_hash (default False).
        """
        excluded = {"sig_service", "sig_appender"}
        if exclude_prev_hash:
            excluded = excluded | {"prev_hash"}
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
