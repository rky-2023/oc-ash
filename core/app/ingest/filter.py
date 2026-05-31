"""OPA ingest filter (Phase 3 task 3.5 consumer).

Queries `data.openclaw.ingest.decision` to decide keep vs drop BEFORE we
build/sign/publish an envelope. Unlike redaction (which fails CLOSED — a
secret must never reach the WORM ledger), noise filtering fails **OPEN**:
if OPA is unreachable we keep the event, because dropping a real event to
save log noise is the worse error. The ledger is a record.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

_TIMEOUT_SECONDS = 2.0


async def should_keep(source: str, **fields: Any) -> bool:
    """Return True if the event should be published.

    `source` is one of fswatch|git|claude|gh-poller; `fields` are the
    source-specific inputs the policy reads (path, hook, body, changed…).
    """
    url = f"{settings.opa_url.rstrip('/')}/v1/data/openclaw/ingest/decision"
    body = {"input": {"source": source, **fields}}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            decision = resp.json().get("result")
    except Exception as e:  # noqa: BLE001 — fail-open is the whole point
        log.warning("ingest.filter.opa_unavailable", source=source, err=str(e))
        return True
    keep = decision != "drop"
    if not keep:
        log.debug("ingest.filter.dropped", source=source)
    return keep
