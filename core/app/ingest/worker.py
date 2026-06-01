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
        # Start each source independently — a socket-permission problem on the
        # hook receiver must not stop the gh-poller, and vice versa.
        try:
            await self._hooks.start()
        except Exception as e:  # noqa: BLE001
            log.warning("ingest.worker.hooks_start_failed", err=str(e),
                        note="hook receiver disabled; set OC_INGEST_SOCKET to a writable path")
        try:
            await self._poller.start()
        except Exception as e:  # noqa: BLE001
            log.warning("ingest.worker.poller_start_failed", err=str(e))
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
