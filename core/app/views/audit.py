"""Read-only JSON API for the audit viewer (Phase 2 task 2.10).

Endpoints:
  GET /api/audit/entries               recent entries, paginated
  GET /api/audit/entries/{ulid}        single entry + sig status
  GET /api/audit/conversations         recent conversations
  GET /api/audit/conversations/{conv_id}  all entries for a conversation
  GET /api/audit/verify                verify chain for a date (server-side)

All reads hit Postgres (openclaw.audit_* tables populated by the
projector). The viewer never touches immudb directly.

Connection: one asyncpg pool per process, initialised lazily on first
request. The pool is closed in core's lifespan shutdown.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import asyncpg
import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import settings

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/audit", tags=["audit"])

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    dsn = settings.effective_postgres_dsn
    if not dsn:
        raise HTTPException(503, "Postgres DSN not configured (OC_POSTGRES_DSN)")
    _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ── Response models ──────────────────────────────────────────────────

class EntryRow(BaseModel):
    ulid: str
    ts: dt.datetime
    subject: str
    conv_id: str | None
    actor_kind: str
    actor_id: str
    action: str
    direction: str
    sig_service_valid: bool | None
    sig_appender_valid: bool | None
    chain_valid: bool | None
    redacted_payload: Any | None
    raw_envelope: Any | None = None  # only in single-entry responses


class ConversationRow(BaseModel):
    conv_id: str
    first_ulid: str
    last_ulid: str
    first_ts: dt.datetime
    last_ts: dt.datetime
    subject_prefix: str
    entry_count: int
    has_integrity_issues: bool


class VerifyResult(BaseModel):
    date: str
    total: int
    sig_service_ok: int
    sig_appender_ok: int
    chain_ok: int
    status: str  # "PASS" | "FAIL" | "NO_DATA"


# ── Endpoints ────────────────────────────────────────────────────────

@router.get("/entries", response_model=list[EntryRow])
async def list_entries(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    subject: str | None = Query(None),
) -> list[EntryRow]:
    pool = await _get_pool()
    if subject:
        rows = await pool.fetch(
            """
            SELECT ulid, ts, subject, conv_id, actor_kind, actor_id,
                   action, direction, sig_service_valid, sig_appender_valid,
                   chain_valid, redacted_payload
              FROM openclaw.audit_entries
             WHERE subject LIKE $3
             ORDER BY ts DESC
             LIMIT $1 OFFSET $2
            """,
            limit, offset, subject + "%",
        )
    else:
        rows = await pool.fetch(
            """
            SELECT ulid, ts, subject, conv_id, actor_kind, actor_id,
                   action, direction, sig_service_valid, sig_appender_valid,
                   chain_valid, redacted_payload
              FROM openclaw.audit_entries
             ORDER BY ts DESC
             LIMIT $1 OFFSET $2
            """,
            limit, offset,
        )
    return [EntryRow(**dict(r)) for r in rows]


@router.get("/entries/{ulid}", response_model=EntryRow)
async def get_entry(ulid: str) -> EntryRow:
    pool = await _get_pool()
    row = await pool.fetchrow(
        """
        SELECT ulid, ts, subject, conv_id, actor_kind, actor_id,
               action, direction, sig_service_valid, sig_appender_valid,
               chain_valid, redacted_payload, raw_envelope
          FROM openclaw.audit_entries
         WHERE ulid = $1
        """,
        ulid,
    )
    if row is None:
        raise HTTPException(404, f"Entry {ulid!r} not found")
    return EntryRow(**dict(row))


@router.get("/conversations", response_model=list[ConversationRow])
async def list_conversations(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[ConversationRow]:
    pool = await _get_pool()
    rows = await pool.fetch(
        """
        SELECT conv_id, first_ulid, last_ulid, first_ts, last_ts,
               subject_prefix, entry_count, has_integrity_issues
          FROM openclaw.audit_conversations
         ORDER BY last_ts DESC
         LIMIT $1 OFFSET $2
        """,
        limit, offset,
    )
    return [ConversationRow(**dict(r)) for r in rows]


@router.get("/conversations/{conv_id}", response_model=list[EntryRow])
async def get_conversation(conv_id: str) -> list[EntryRow]:
    pool = await _get_pool()
    rows = await pool.fetch(
        """
        SELECT ulid, ts, subject, conv_id, actor_kind, actor_id,
               action, direction, sig_service_valid, sig_appender_valid,
               chain_valid, redacted_payload
          FROM openclaw.audit_entries
         WHERE conv_id = $1
         ORDER BY ulid ASC
        """,
        conv_id,
    )
    if not rows:
        raise HTTPException(404, f"Conversation {conv_id!r} not found")
    return [EntryRow(**dict(r)) for r in rows]


@router.get("/verify", response_model=VerifyResult)
async def verify_date(
    date: str | None = Query(None, description="ISO date YYYY-MM-DD, default today UTC"),
) -> VerifyResult:
    if date is None:
        target = dt.date.today().isoformat()
    else:
        try:
            dt.date.fromisoformat(date)
            target = date
        except ValueError:
            raise HTTPException(400, f"Invalid date {date!r} — use YYYY-MM-DD")

    pool = await _get_pool()
    row = await pool.fetchrow(
        """
        SELECT
          COUNT(*)                                          AS total,
          COUNT(*) FILTER (WHERE sig_service_valid  = true) AS sig_service_ok,
          COUNT(*) FILTER (WHERE sig_appender_valid = true) AS sig_appender_ok,
          COUNT(*) FILTER (WHERE chain_valid        = true) AS chain_ok
          FROM openclaw.audit_entries
         WHERE ts::date = $1::date
        """,
        target,
    )
    total = row["total"]
    if total == 0:
        return VerifyResult(
            date=target, total=0,
            sig_service_ok=0, sig_appender_ok=0, chain_ok=0,
            status="NO_DATA",
        )
    all_ok = (
        row["sig_service_ok"] == total
        and row["sig_appender_ok"] == total
        and row["chain_ok"] == total
    )
    return VerifyResult(
        date=target,
        total=total,
        sig_service_ok=row["sig_service_ok"],
        sig_appender_ok=row["sig_appender_ok"],
        chain_ok=row["chain_ok"],
        status="PASS" if all_ok else "FAIL",
    )
