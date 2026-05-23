"""FastAPI middleware that emits one audit envelope per request.

Per ADR-003 D3 (two-phase append): we publish to NATS FIRST (durable
bus), and the appender service mirrors to immudb LATER. This middleware
is responsible only for the publish step. Subjects: `oc.event.core.*`.

The middleware is non-blocking on the request — if NATS publishing
fails (bus down, paranoid mode, etc.) we log loudly but DO NOT fail
the user's request. The point of the audit pipeline is to RECORD
truth, not to gatekeep traffic.

For each request we emit TWO envelopes: one with action=request
(before the handler runs) and one with action=response (after).
They share a conv_id so they can be re-correlated in the viewer.
"""

from __future__ import annotations

import hashlib
import time
import uuid

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.audit.envelope import Action, Actor, ActorKind, AuditEnvelope, Direction
from app.audit.signer import sign_envelope
from app.bus.nats_client import get_bus

log = structlog.get_logger(__name__)

# Paths that should NOT be audited (avoid recursion / noise).
SKIP_PATHS = ("/health", "/static", "/favicon.ico")


def _hash_bytes(b: bytes | None) -> str | None:
    if b is None:
        return None
    return "sha256:" + hashlib.sha256(b).hexdigest()


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Skip noise paths
        path = request.url.path
        if any(path.startswith(p) for p in SKIP_PATHS):
            return await call_next(request)

        conv_id = uuid.uuid4().hex
        actor = self._derive_actor(request)
        req_subject = f"oc.event.core.request.{request.method.lower()}"
        resp_subject = f"oc.event.core.response.{request.method.lower()}"
        started = time.monotonic()

        # ── REQUEST envelope ──────────────────────────────────────────
        body_bytes = await self._safe_read_body(request)
        req_env = AuditEnvelope.new(
            subject=req_subject,
            actor=actor,
            action=Action.REQUEST,
            direction=Direction.INGRESS,
            conv_id=conv_id,
            request_hash=_hash_bytes(body_bytes),
            redacted_payload={
                "method": request.method,
                "path": path,
                "query": str(request.url.query)[:1024] if request.url.query else None,
                "remote": request.client.host if request.client else None,
                "headers_count": len(request.headers),
            },
        )
        sign_envelope(req_env, slot="service")
        await self._publish_safe(req_subject, req_env)

        # ── Run the actual handler ────────────────────────────────────
        response = await call_next(request)

        # ── RESPONSE envelope ─────────────────────────────────────────
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        resp_env = AuditEnvelope.new(
            subject=resp_subject,
            actor=actor,
            action=Action.RESPONSE,
            direction=Direction.EGRESS,
            conv_id=conv_id,
            redacted_payload={
                "method": request.method,
                "path": path,
                "status_code": response.status_code,
                "elapsed_ms": elapsed_ms,
            },
        )
        sign_envelope(resp_env, slot="service")
        await self._publish_safe(resp_subject, resp_env)

        # Stamp the conv_id back into the response so clients can
        # quote it when filing audit-trace bugs.
        response.headers["X-OC-Conv-Id"] = conv_id

        return response

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _derive_actor(request: Request) -> Actor:
        # If the route already authenticated, we'd see a token on
        # request.state.user (set by a future dependency). For now,
        # any /auth/* request is anonymous; everything else is too.
        # Phase 2 task 2.5b wires the JWT verifier to populate
        # request.state.user.
        return Actor(
            kind=ActorKind.USER,
            id=request.client.host if request.client else "unknown",
        )

    @staticmethod
    async def _safe_read_body(request: Request) -> bytes | None:
        """Read + cache the request body so the handler still sees it.

        Starlette's Request.body() reads once and caches; calling it
        here is safe because subsequent calls in the handler see the
        cached bytes.
        """
        try:
            return await request.body() if request.method in {"POST", "PUT", "PATCH"} else None
        except Exception as e:
            log.warning("audit.body_read_failed", err=str(e))
            return None

    @staticmethod
    async def _publish_safe(subject: str, env: AuditEnvelope) -> None:
        """Publish without ever failing the caller. Log loudly on error."""
        try:
            bus = get_bus()
            await bus.publish(subject, env.to_wire_bytes(), msg_id=env.ulid)
        except Exception as e:
            log.warning(
                "audit.publish_failed",
                subject=subject,
                ulid=env.ulid,
                err=str(e),
                err_type=type(e).__name__,
            )
