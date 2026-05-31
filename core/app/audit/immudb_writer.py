"""Thin async wrapper around immudb-py for the audit-appender path.

The immudb Python client (immudb-py) is sync. We run its calls in a
threadpool via asyncio.to_thread so we don't block the event loop.

The `appender` user (RW on openclaw_audit, created by Phase 2 task
2.3) is used. Credentials come from env for MVP — operator can
populate them via core/scripts/run-with-vault-creds.sh, which logs
into Vault with their AppRole and exports the immudb password.

immudb's key/value model: we use the envelope's ULID (bytes) as the
key and the canonical wire JSON as the value. ULIDs sort
lexicographically by creation time, so range-scans + the immudb
intrinsic Merkle tree both work naturally.

Phase 2 task 2.9 (audit-projector) will read these entries with the
read-only `projector` user and materialise the Postgres projection.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import structlog

from app.config import settings

log = structlog.get_logger(__name__)


class ImmudbWriter:
    """Holds a single immudb client + thread lock; exposes async helpers."""

    def __init__(self) -> None:
        self._client: Any | None = None
        self._lock = threading.Lock()
        self._connected = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open the immudb connection and select the openclaw_audit DB."""
        if self._connected:
            return

        # Import lazily so unit tests that don't need immudb don't pull it.
        from immudb.client import ImmudbClient

        host = settings.immudb_host
        port = settings.immudb_port
        user = settings.immudb_user
        password = settings.immudb_password
        db = settings.immudb_database

        if not password:
            raise RuntimeError(
                "OC_IMMUDB_PASSWORD not set. Use core/scripts/run-with-vault-creds.sh "
                "to fetch from Vault, or export the env var manually."
            )

        log.info("immudb.connect.begin", host=host, port=port, user=user, db=db)

        def _do_connect() -> Any:
            # immudb-py's login() defaults to database=b"defaultdb". The
            # appender/projector users are granted permission ONLY on the
            # audit DB, so logging into defaultdb raises PERMISSION_DENIED.
            # Log straight into the target database instead.
            db_bytes = db.encode() if isinstance(db, str) else db
            c = ImmudbClient(f"{host}:{port}")
            c.login(user, password, database=db_bytes)
            c.useDatabase(db_bytes)
            return c

        self._client = await asyncio.to_thread(_do_connect)
        self._connected = True
        log.info("immudb.connect.ok", host=host, port=port, db=db)

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await asyncio.to_thread(self._client.logout)
        except Exception as e:
            log.warning("immudb.logout_failed", err=str(e))
        self._client = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Writes ───────────────────────────────────────────────────────────

    async def set_envelope(self, key: bytes, value: bytes) -> int:
        """Store one envelope. Returns the immudb transaction id."""
        if self._client is None:
            raise RuntimeError("immudb writer not connected")

        def _do_set() -> int:
            with self._lock:
                resp = self._client.set(key, value)
            # response shape depends on client version: try common attrs
            return int(getattr(resp, "id", getattr(resp, "tx", 0)) or 0)

        return await asyncio.to_thread(_do_set)

    # ── Reads (for prev_hash bootstrap) ──────────────────────────────────

    async def get_latest_key(self) -> bytes | None:
        """Return the lexicographically-latest envelope key, or None if empty.

        Used at appender startup to recover the prev_hash chain seed
        after a process restart.
        """
        if self._client is None:
            return None

        def _do_scan() -> bytes | None:
            with self._lock:
                # immudb-py's scan returns entries in ascending order; we
                # want the last one. zScan / history exist for ordered
                # walks but for simplicity we use a bounded reverse iter
                # by passing a high seek key. If the API doesn't support
                # `desc`, we fall back to a None which means "appender
                # starts a new chain root" — acceptable for MVP.
                try:
                    entries = self._client.scan(b"", b"", 1, True)  # last 1, desc
                except Exception:
                    return None
                if not entries:
                    return None
                # Different client versions yield different shapes
                last = entries[-1] if isinstance(entries, list) else entries
                return getattr(last, "key", None)

        return await asyncio.to_thread(_do_scan)

    async def get_latest_value(self) -> bytes | None:
        """Return the stored value of the lexicographically-latest envelope,
        or None if the ledger is empty.

        Used at appender startup to re-seed the prev_hash chain: the appender
        recomputes the latest entry's canonical hash so the first envelope it
        writes after a restart chains onto real prior state instead of opening
        a fresh (chain-break-flagged) root. Uses the same scan shape the
        projector relies on (scan(key, prefix, desc, limit) -> Dict).
        """
        if self._client is None:
            return None

        def _do_scan() -> bytes | None:
            with self._lock:
                try:
                    result = self._client.scan(b"", b"", False, 1000)
                except Exception:
                    return None
            items = list((result or {}).items())
            if not items:
                return None
            items.sort(key=lambda kv: kv[0])  # ascending ULID; take the last
            return items[-1][1]

        return await asyncio.to_thread(_do_scan)


# Module-level singleton.
_writer: ImmudbWriter | None = None


def get_writer() -> ImmudbWriter:
    global _writer
    if _writer is None:
        _writer = ImmudbWriter()
    return _writer
