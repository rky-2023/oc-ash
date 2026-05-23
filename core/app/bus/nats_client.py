"""Async NATS client with a single-connection lifecycle.

Held on `app.state.nats` after lifespan startup; closed in shutdown.
Publishers obtain it via the dependency `get_nats()`.

JetStream-aware: publishes are sent through the JS context so they
get persisted to the appropriate stream (OC_EVENT, OC_A2A, etc.)
matching their subject pattern. Streams must already exist —
created by infra/scripts/08-nats-bootstrap.sh, see ADR-001 D3.
"""

from __future__ import annotations

import asyncio
import structlog
from nats.aio.client import Client as NATSClient
from nats.js.client import JetStreamContext

from app.config import settings

log = structlog.get_logger(__name__)


class NATSBus:
    """Thin wrapper around a NATS client + JetStream context."""

    def __init__(self) -> None:
        self._nc: NATSClient | None = None
        self._js: JetStreamContext | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            if self._nc is not None and self._nc.is_connected:
                return
            log.info("nats.connect.begin", url=settings.nats_url)
            nc = NATSClient()
            await nc.connect(
                servers=[settings.nats_url],
                name="openclaw-core",
                max_reconnect_attempts=-1,        # retry forever
                reconnect_time_wait=2,
                ping_interval=20,
                allow_reconnect=True,
            )
            self._nc = nc
            self._js = nc.jetstream()
            log.info("nats.connect.ok", url=settings.nats_url, connected_url=nc.connected_url.netloc)

    async def close(self) -> None:
        async with self._lock:
            if self._nc is not None:
                log.info("nats.close")
                await self._nc.close()
                self._nc = None
                self._js = None

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    async def publish(self, subject: str, payload: bytes, *, msg_id: str | None = None) -> None:
        """Publish a single message via JetStream, with optional msg_id for dedupe."""
        if self._js is None:
            raise RuntimeError("NATS not connected — did lifespan.startup run?")
        headers = {"Nats-Msg-Id": msg_id} if msg_id else None
        ack = await self._js.publish(subject, payload, headers=headers)
        log.debug("nats.publish.ok", subject=subject, stream=ack.stream, seq=ack.seq)

    async def health_check(self) -> str:
        """Return a one-word status for /health/deps."""
        if self._nc is None:
            return "down"
        if not self._nc.is_connected:
            return "reconnecting"
        return "ok"


# Module-level singleton. main.py's lifespan creates/connects it once.
_bus: NATSBus | None = None


def get_bus() -> NATSBus:
    """Accessor for non-FastAPI code that needs the bus."""
    global _bus
    if _bus is None:
        _bus = NATSBus()
    return _bus
