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
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.auth.router import router as auth_router
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
    app.include_router(auth_router)

    # Serve the minimal HTML/JS pages used for WebAuthn enrollment + login.
    # Phase 1 task 1.8 ships these inline with the API service; Phase 9
    # introduces a proper Next.js UI on a separate origin.
    web_dir = Path(__file__).parent / "web"
    if web_dir.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(web_dir / "static")),
            name="static",
        )

        @app.get("/", include_in_schema=False)
        async def _root() -> FileResponse:
            return FileResponse(web_dir / "index.html")

        @app.get("/register", include_in_schema=False)
        async def _register_page() -> FileResponse:
            return FileResponse(web_dir / "register.html")

        @app.get("/login", include_in_schema=False)
        async def _login_page() -> FileResponse:
            return FileResponse(web_dir / "login.html")

    return app


app = create_app()
