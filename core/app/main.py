"""openclaw-core FastAPI entrypoint.

Phase 2 scaffold. The Phase 2 runbook tasks 2.5–2.14 fill this in with:
- audit envelope middleware (task 2.6, 2.14)
- MCP proxy (task 2.7 onward — partly Phase 4)
- A2A router (Phase 4)
- WebAuthn endpoints (Phase 1 task 1.8)
- NATS connection lifecycle
- immudb connection lifecycle
- Postgres lookup connection lifecycle

Right now it serves /health and /health/deps so the docker compose
healthcheck passes and the dev loop is usable.
"""

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import structlog
from fastapi import FastAPI

from app.config import settings
from app.health import router as health_router

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info(
        "openclaw-core starting",
        version="0.1.0",
        env=settings.env,
        log_level=settings.log_level,
    )
    # Phase 2 task 2.5: open NATS client and stash on app.state.
    # Phase 2 task 2.5: open immudb client and stash on app.state.
    # Phase 2 task 2.5: read secrets from settings.secrets_dir.
    # Phase 2 task 2.6: prepare audit envelope signer (Vault transit client).
    yield
    log.info("openclaw-core shutting down")
    # Phase 2 task 2.5: close NATS / immudb / Postgres connections cleanly.


def create_app() -> FastAPI:
    app = FastAPI(
        title="openclaw-core",
        version="0.1.0",
        lifespan=lifespan,
        # Hide docs in production per ADR-002 spirit (least info disclosure).
        docs_url=None if settings.env == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if settings.env == "production" else "/openapi.json",
    )
    app.include_router(health_router)
    return app


app = create_app()
