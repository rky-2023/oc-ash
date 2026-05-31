"""Poller checkpoints, persisted in openclaw.lookup.

Each (repo, endpoint) keeps its `{"seen": {...}}` map under
`gh-poller.checkpoints.<owner>.<repo>.<entity>` so polling resumes across
restarts without re-emitting. The lookup table also drives feature flags and
config, so we only ever touch our own keys here.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = settings.effective_postgres_dsn
        if not dsn:
            raise RuntimeError("OC_POSTGRES_DSN not set — gh-poller checkpoints need Postgres")
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    return _pool


async def load_checkpoint(key: str) -> dict[str, Any] | None:
    pool = await _get_pool()
    val = await pool.fetchval("SELECT value FROM openclaw.lookup WHERE key = $1", key)
    if val is None:
        return None
    return json.loads(val) if isinstance(val, str) else val


async def save_checkpoint(key: str, value: dict[str, Any]) -> None:
    pool = await _get_pool()
    await pool.execute(
        """
        INSERT INTO openclaw.lookup (key, value, description)
        VALUES ($1, $2::jsonb, 'gh-poller checkpoint')
        ON CONFLICT (key) DO UPDATE
          SET value = EXCLUDED.value, updated_at = now()
        """,
        key,
        json.dumps(value),
    )


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
