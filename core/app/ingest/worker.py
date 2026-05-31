"""oc-ingest worker lifecycle: runs the gh-poller and the Claude-hook
receiver concurrently inside core (MVP).

Both share the emit path (filter → sign → publish). Gated by
settings.enable_ingest_worker so it's off unless explicitly enabled.
"""

from __future__ import annotations

import structlog

from app.ingest import checkpoint
from app.ingest.hooks import ClaudeHookReceiver
from app.ingest.poller import GitHubPoller

log = structlog.get_logger(__name__)


class IngestWorker:
    def __init__(self) -> None:
        self._poller = GitHubPoller()
        self._hooks = ClaudeHookReceiver()

    async def start(self) -> None:
        await self._hooks.start()
        await self._poller.start()
        log.info("ingest.worker.started")

    async def stop(self) -> None:
        await self._poller.stop()
        await self._hooks.stop()
        await checkpoint.close()
        log.info("ingest.worker.stopped")


_worker: IngestWorker | None = None


def get_ingest_worker() -> IngestWorker:
    global _worker
    if _worker is None:
        _worker = IngestWorker()
    return _worker
