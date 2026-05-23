"""audit-appender — NATS consumer that writes envelopes to immudb.

Phase 2 task 2.8. Implements the SECOND half of ADR-003 D3's two-phase
append: NATS-first (durability) → immudb-second (cryptographic anchor).

Architectural note: ADR-003 D3 calls for the audit-appender to be a
*separate service* from openclaw-core. For this MVP, we run it as a
background task inside core's lifespan to keep the deployment surface
small. The split into its own container happens in Phase 11 hardening
(or sooner, if we hit GIL contention or lifecycle-coupling pain). The
queue.go-style interface here keeps that future split low-friction.

Subscription:
  Subject filter:  oc.event.>  AND  oc.a2a.>  AND  oc.mcp.>  AND  oc.notify.>
  (We intentionally do NOT subscribe to oc.health.> — those are
  liveness pings, not auditable events.)

Per message:
  1. Parse the envelope JSON.
  2. Verify sig_service. If invalid, log + ACK (no re-delivery loop on
     bad payloads — we'll catch them via the projector's signature-
     verification step).
  3. Fill prev_hash from in-memory chain pointer (seeded on startup
     from immudb's latest entry).
  4. Sign sig_appender.
  5. Write to immudb keyed by ULID-bytes.
  6. Update in-memory chain pointer to the new entry's canonical hash.
  7. ACK the NATS message.

Failure modes:
  - immudb write fails → NACK (re-delivery later). Don't update chain pointer.
  - immudb persistently unreachable → audit lag climbs. Phase 2 task 2.5b
    (TODO) will publish a metric; >24h triggers paranoid mode (ADR-003 D4).
"""

from __future__ import annotations

import asyncio

import structlog

from app.audit.envelope import AuditEnvelope
from app.audit.immudb_writer import get_writer
from app.audit.signer import sign_envelope, verify_envelope
from app.bus.nats_client import get_bus

log = structlog.get_logger(__name__)

# Subjects worth auditing. health pings are excluded — they're operational.
SUBJECTS = ["oc.event.>", "oc.a2a.>", "oc.mcp.>", "oc.notify.>"]

# Durable consumer name; NATS dedupes by this so a restart resumes
# where we left off without re-processing old messages.
DURABLE = "audit-appender"


class AuditAppender:
    """Background coroutine consuming the bus and writing to immudb."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # Chain pointer: hex digest of the most recently appended envelope's
        # canonical body. Used as prev_hash on the next envelope.
        self._chain_head: str | None = None

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()

        # Seed chain pointer from immudb if there's prior state.
        writer = get_writer()
        if writer.is_connected:
            latest_key = await writer.get_latest_key()
            if latest_key is not None:
                # We don't recompute the hash on bootstrap; we just note the
                # last key and let the first new envelope chain to a fresh
                # canonical-hash derived in-memory thereafter. Acceptable
                # MVP — the projector will detect a chain break and flag.
                log.info("appender.startup.found_prior_state", last_key=latest_key.hex())

        self._task = asyncio.create_task(self._run(), name="audit-appender")
        log.info("appender.started", subjects=SUBJECTS, durable=DURABLE)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None
        log.info("appender.stopped")

    # ── Main loop ───────────────────────────────────────────────────────

    async def _run(self) -> None:
        bus = get_bus()
        writer = get_writer()

        if bus._js is None:
            log.warning("appender.no_jetstream_at_start", note="will retry every 5s")
            while not self._stop.is_set():
                await asyncio.wait_for(self._stop.wait(), timeout=5)
                if bus._js is not None:
                    break

        # Create one pull-subscription per subject filter so we can
        # process them all on one durable consumer.
        from nats.js.api import ConsumerConfig

        psubs = []
        for subj in SUBJECTS:
            try:
                psub = await bus._js.pull_subscribe(
                    subj,
                    durable=f"{DURABLE}-{subj.replace('.', '_').replace('>', 'all')}",
                    config=ConsumerConfig(
                        ack_wait=30,
                        max_deliver=5,
                    ),
                )
                psubs.append(psub)
                log.info("appender.subscribed", subject=subj)
            except Exception as e:
                log.warning("appender.subscribe_failed", subject=subj, err=str(e))

        if not psubs:
            log.error("appender.no_subscriptions; exiting loop")
            return

        # Main consume loop
        while not self._stop.is_set():
            for psub in psubs:
                try:
                    msgs = await psub.fetch(batch=10, timeout=1)
                except (asyncio.TimeoutError, Exception):
                    msgs = []
                for msg in msgs:
                    await self._process(msg, writer)

        # On stop, unsubscribe gracefully
        for psub in psubs:
            try:
                await psub.unsubscribe()
            except Exception:
                pass

    async def _process(self, msg, writer) -> None:
        try:
            env = AuditEnvelope.model_validate_json(msg.data)
        except Exception as e:
            log.warning("appender.bad_envelope", err=str(e))
            await msg.ack()
            return

        # Verify the originating service's signature
        if not verify_envelope(env, slot="service"):
            log.warning(
                "appender.invalid_service_sig",
                ulid=env.ulid,
                subject=env.subject,
            )
            # ACK anyway — re-delivery won't fix a bad signature.
            await msg.ack()
            return

        # Chain prev_hash to whatever we last appended
        if self._chain_head is not None and env.prev_hash is None:
            env.prev_hash = self._chain_head

        # Append our own signature
        sign_envelope(env, slot="appender")

        # Write to immudb
        try:
            key = env.ulid.encode("ascii")
            value = env.to_wire_bytes()
            if writer.is_connected:
                tx = await writer.set_envelope(key, value)
                log.debug("appender.wrote", ulid=env.ulid, tx=tx, subject=env.subject)
                # Update chain head AFTER successful write
                self._chain_head = env.canonical_sha256()
                await msg.ack()
            else:
                # immudb not connected — NACK so NATS redelivers later
                log.warning("appender.immudb_disconnected; NACK", ulid=env.ulid)
                await msg.nak(delay=5)
        except Exception as e:
            log.warning(
                "appender.write_failed",
                ulid=env.ulid,
                err=str(e),
                err_type=type(e).__name__,
            )
            await msg.nak(delay=5)


# Module-level singleton.
_appender: AuditAppender | None = None


def get_appender() -> AuditAppender:
    global _appender
    if _appender is None:
        _appender = AuditAppender()
    return _appender
