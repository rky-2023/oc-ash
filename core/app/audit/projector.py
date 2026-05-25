"""Audit projector — reads immudb, writes to Postgres.

Phase 2 task 2.9. Implements ADR-003 D5: the Postgres projection is a
denormalized cache of the immudb WORM ledger. immudb is source of truth;
Postgres is the fast-query layer used by the viewer and the oc CLI.

Architecture (MVP):
  Runs as a background task inside openclaw-core (same process as the
  appender). Polls immudb every OC_PROJECTOR_POLL_SECONDS seconds for
  new entries, verifies both signatures + prev_hash chain, writes to
  Postgres atomically (entry insert + checkpoint update in one txn).

  Phase 11 hardening: extract into its own container with a dedicated
  AppRole (read-only on immudb, write on openclaw.audit_*).

Integrity policy:
  - Bad sig_service OR bad sig_appender  → project the row with
    sig_*_valid=false and has_integrity_issues=true on the conversation.
    The projector does NOT halt on bad sigs (a halted projector would
    block the viewer entirely; the viewer shows the flag instead).
  - Broken prev_hash chain                → same treatment; chain_valid=false.
  - Parse error (envelope can't be decoded) → log + skip; the ULID is
    recorded in the checkpoint so we never re-try a corrupt entry.

Checkpoint:
  The `openclaw.audit_checkpoint` singleton row holds `last_ulid`. Each
  cycle scans immudb for keys >= last_ulid (exclusive) so entries are
  processed exactly once. Checkpoint advances inside the same Postgres
  transaction as the entry inserts — if the txn rolls back, the cycle
  retries the same batch next poll.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import asyncpg
import structlog

from app.audit.envelope import AuditEnvelope
from app.audit.signer import verify_envelope

log = structlog.get_logger(__name__)

_projector: AuditProjector | None = None


def get_projector() -> AuditProjector:
    global _projector
    if _projector is None:
        _projector = AuditProjector()
    return _projector


class AuditProjector:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._pool: asyncpg.Pool | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self, dsn: str, poll_seconds: int = 5) -> None:
        if self._task is not None:
            return
        self._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(poll_seconds), name="audit-projector"
        )
        log.info("projector.started", poll_seconds=poll_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=15)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None
        if self._pool:
            await self._pool.close()
            self._pool = None
        log.info("projector.stopped")

    # ── Main loop ────────────────────────────────────────────────────────

    async def _run(self, poll_seconds: int) -> None:
        while not self._stop.is_set():
            try:
                projected = await self._cycle()
                if projected:
                    log.info("projector.cycle", projected=projected)
            except Exception as e:
                log.error("projector.cycle_error", err=str(e), err_type=type(e).__name__)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=float(poll_seconds))
            except asyncio.TimeoutError:
                pass

    async def _cycle(self) -> int:
        from app.audit.immudb_writer import get_writer

        writer = get_writer()
        if not writer.is_connected:
            return 0

        assert self._pool is not None

        # 1. Read checkpoint
        checkpoint = await self._pool.fetchval(
            "SELECT last_ulid FROM openclaw.audit_checkpoint WHERE id = 1"
        )

        # 2. Scan immudb for entries newer than the checkpoint
        def _scan() -> list[tuple[bytes, bytes]]:
            pairs: list[tuple[bytes, bytes]] = []
            seek = (checkpoint or "").encode("ascii") if checkpoint else b""
            try:
                entries = writer._client.scan(seek, 0, 200, False)
            except TypeError:
                try:
                    entries = writer._client.scan(seek, b"", 200, False)
                except Exception:
                    entries = []
            for e in entries or []:
                k = getattr(e, "key", None)
                v = getattr(e, "value", None)
                if k is not None and v is not None:
                    pairs.append((k, v))
            return pairs

        raw_pairs = await asyncio.to_thread(_scan)

        # Skip the checkpoint entry itself (scan is inclusive on seekKey)
        if checkpoint and raw_pairs and raw_pairs[0][0] == checkpoint.encode("ascii"):
            raw_pairs = raw_pairs[1:]

        if not raw_pairs:
            return 0

        # 3. Process and verify each envelope
        rows: list[dict[str, Any]] = []
        last_ulid: str | None = None
        last_prev_hash: str | None = None

        for key_bytes, val_bytes in raw_pairs:
            ulid = key_bytes.decode("ascii", errors="replace")
            try:
                env = AuditEnvelope.model_validate_json(val_bytes)
            except Exception as e:
                log.warning("projector.parse_error", ulid=ulid, err=str(e))
                last_ulid = ulid  # advance past corrupt entry
                continue

            sig_svc_ok = await verify_envelope(env, slot="service")
            sig_app_ok = await verify_envelope(env, slot="appender")
            chain_ok = (
                env.prev_hash == last_prev_hash
                if last_prev_hash is not None
                else True  # first entry in this batch — trust it
            )
            last_prev_hash = env.canonical_sha256()

            has_issues = not (sig_svc_ok and sig_app_ok and chain_ok)
            if has_issues:
                log.warning(
                    "projector.integrity_issue",
                    ulid=ulid,
                    sig_svc=sig_svc_ok,
                    sig_app=sig_app_ok,
                    chain=chain_ok,
                )

            rows.append(
                {
                    "ulid": env.ulid,
                    "ts": env.ts,
                    "subject": env.subject,
                    "conv_id": env.conv_id,
                    "actor_kind": env.actor.kind.value,
                    "actor_id": env.actor.id,
                    "action": env.action.value,
                    "direction": env.direction.value,
                    "request_hash": env.request_hash,
                    "response_hash": env.response_hash,
                    "redacted_payload": json.dumps(env.redacted_payload)
                    if env.redacted_payload
                    else None,
                    "prev_hash": env.prev_hash,
                    "sig_service_valid": sig_svc_ok,
                    "sig_appender_valid": sig_app_ok,
                    "chain_valid": chain_ok,
                    "raw_envelope": val_bytes.decode("utf-8", errors="replace"),
                    "has_issues": has_issues,
                    "policy": env.policy,
                }
            )
            last_ulid = env.ulid

        if not rows:
            return 0

        # 4. Write to Postgres atomically with checkpoint advance
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for row in rows:
                    await conn.execute(
                        """
                        INSERT INTO openclaw.audit_entries
                          (ulid, ts, subject, conv_id, actor_kind, actor_id,
                           action, direction, request_hash, response_hash,
                           redacted_payload, prev_hash,
                           sig_service_valid, sig_appender_valid, chain_valid,
                           raw_envelope)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                        ON CONFLICT (ulid) DO NOTHING
                        """,
                        row["ulid"],
                        row["ts"],
                        row["subject"],
                        row["conv_id"],
                        row["actor_kind"],
                        row["actor_id"],
                        row["action"],
                        row["direction"],
                        row["request_hash"],
                        row["response_hash"],
                        row["redacted_payload"],
                        row["prev_hash"],
                        row["sig_service_valid"],
                        row["sig_appender_valid"],
                        row["chain_valid"],
                        row["raw_envelope"],
                    )
                    if row["conv_id"]:
                        await conn.execute(
                            """
                            INSERT INTO openclaw.audit_conversations
                              (conv_id, first_ulid, last_ulid, first_ts, last_ts,
                               subject_prefix, entry_count, has_integrity_issues)
                            VALUES ($1,$2,$2,$3,$3,$4,1,$5)
                            ON CONFLICT (conv_id) DO UPDATE SET
                              last_ulid  = EXCLUDED.last_ulid,
                              last_ts    = EXCLUDED.last_ts,
                              entry_count = openclaw.audit_conversations.entry_count + 1,
                              has_integrity_issues = openclaw.audit_conversations.has_integrity_issues
                                            OR EXCLUDED.has_integrity_issues,
                              updated_at = now()
                            """,
                            row["conv_id"],
                            row["ulid"],
                            row["ts"],
                            row["subject"].rsplit(".", 2)[0],
                            row["has_issues"],
                        )
                    if row["policy"]:
                        p = row["policy"]
                        await conn.execute(
                            """
                            INSERT INTO openclaw.audit_policy_decisions
                              (ulid, decision, rule, ts)
                            VALUES ($1,$2,$3,$4)
                            """,
                            row["ulid"],
                            p.decision,
                            p.rule,
                            row["ts"],
                        )

                # Advance checkpoint
                await conn.execute(
                    """
                    UPDATE openclaw.audit_checkpoint
                       SET last_ulid = $1, last_ts = now(), updated_at = now()
                     WHERE id = 1
                    """,
                    last_ulid,
                )

        return len(rows)
