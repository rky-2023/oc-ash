"""SQLite storage for WebAuthn credentials + in-flight challenges.

Single SQLite file at the path configured by `settings.auth_db_path`
(default /var/lib/openclaw-core/auth.db, persisted via docker volume).

Schema is created idempotently on first connection. Migrations would
add ALTERs here; for v1 we just CREATE TABLE IF NOT EXISTS.

Concurrency: SQLite serializes writes via its own locking. FastAPI's
default thread-pool for sync handlers means concurrent reads work
fine. For openclaw scale (single operator) the WAL mode + per-request
connections are more than sufficient.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.config import settings

# Module-level lock around connect/create so concurrent first-callers
# don't race the schema bootstrap.
_init_lock = threading.Lock()
_initialised = False


_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        TEXT PRIMARY KEY,         -- random 16-byte ULID-like id
    username       TEXT UNIQUE NOT NULL,
    display_name   TEXT NOT NULL,
    created_at     TEXT NOT NULL             -- ISO8601 UTC
);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    credential_id           BLOB PRIMARY KEY,  -- raw credential id bytes
    user_id                 TEXT NOT NULL,
    public_key              BLOB NOT NULL,     -- COSE-encoded public key
    sign_count              INTEGER NOT NULL DEFAULT 0,
    transports              TEXT,              -- comma-separated, e.g. "internal,hybrid"
    aaguid                  TEXT,              -- 16-byte authenticator GUID, hex
    nickname                TEXT,
    backed_up               INTEGER NOT NULL DEFAULT 0,
    backup_eligible         INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT NOT NULL,
    last_used_at            TEXT,
    revoked_at              TEXT,
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_creds_user ON webauthn_credentials(user_id);

CREATE TABLE IF NOT EXISTS pending_challenges (
    challenge_id   TEXT PRIMARY KEY,           -- session-cookie-bound id
    kind           TEXT NOT NULL,              -- 'register' | 'login'
    user_id        TEXT,                       -- null for usernameless flows
    username       TEXT,
    challenge      BLOB NOT NULL,              -- random bytes
    expires_at     TEXT NOT NULL               -- ISO8601 UTC; rows older than this are sweepable
);

CREATE INDEX IF NOT EXISTS idx_chal_expires ON pending_challenges(expires_at);

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
"""


def _ensure_initialised() -> None:
    global _initialised
    if _initialised:
        return
    with _init_lock:
        if _initialised:
            return
        path = Path(settings.auth_db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(path)) as conn:
            conn.executescript(_SCHEMA)
            conn.commit()
        _initialised = True


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Open a per-call connection. Use as a context manager.

    Example:
        with get_conn() as conn:
            cursor = conn.execute("SELECT 1")
    """
    _ensure_initialised()
    conn = sqlite3.connect(str(settings.auth_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sweep_expired_challenges() -> int:
    """Delete challenges past their expires_at. Called opportunistically
    from challenge-create paths so the table doesn't grow unbounded.

    Returns the number of rows removed.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM pending_challenges WHERE expires_at < datetime('now')"
        )
        return cur.rowcount
