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

from app.audit.appender import get_appender
from app.audit.immudb_writer import get_writer
from app.audit.middleware import AuditMiddleware
from app.audit.projector import get_projector
from app.auth.router import router as auth_router
from app.bus.nats_client import get_bus
from app.config import settings
from app.health import router as health_router
from app.views.audit import close_pool as close_audit_pool
from app.views.audit import router as audit_router

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info(
        "openclaw-core starting",
        version="0.1.0",
        env=settings.env,
        log_level=settings.log_level,
    )

    # NATS connection — non-fatal if it can't connect at startup;
    # publish-safe in the middleware logs and continues. Re-connect
    # is handled by nats-py's built-in reconnect loop (-1 = forever).
    bus = get_bus()
    try:
        await bus.connect()
    except Exception as e:
        log.warning(
            "nats.connect.failed_at_startup",
            err=str(e),
            note="audit publishes will be no-ops until NATS reachable",
        )
    app.state.bus = bus

    # immudb writer + audit-appender. Both are optional — if immudb is
    # unreachable or credentials aren't set, we log loudly and continue
    # without the audit-write half of the pipeline (the front-end NATS
    # publish still works).
    writer = get_writer()
    appender = get_appender()
    if settings.enable_audit_appender and settings.immudb_password:
        try:
            await writer.connect()
            await appender.start()
        except Exception as e:
            log.warning(
                "audit_appender.start_failed",
                err=str(e),
                note="envelopes will sit on NATS until appender comes up",
            )
    else:
        log.info(
            "audit_appender.disabled",
            enable=settings.enable_audit_appender,
            has_password=bool(settings.immudb_password),
        )
    app.state.writer = writer
    app.state.appender = appender

    # Audit projector — reads immudb, writes to Postgres.
    projector = get_projector()
    pg_dsn = settings.effective_postgres_dsn
    if settings.enable_audit_projector and pg_dsn:
        try:
            await projector.start(pg_dsn, poll_seconds=settings.projector_poll_seconds)
        except Exception as e:
            log.warning(
                "audit_projector.start_failed",
                err=str(e),
                note="projection will be unavailable until projector starts",
            )
    else:
        log.info(
            "audit_projector.disabled",
            enable=settings.enable_audit_projector,
            has_dsn=bool(pg_dsn),
        )
    app.state.projector = projector

    # oc-ingest worker — gh-poller + Claude-hook receiver (Phase 3). Off by
    # default; enable on the host with OC_ENABLE_INGEST_WORKER=true.
    ingest_worker = None
    if settings.enable_ingest_worker:
        from app.ingest.worker import get_ingest_worker

        ingest_worker = get_ingest_worker()
        try:
            await ingest_worker.start()
        except Exception as e:
            log.warning("ingest_worker.start_failed", err=str(e))
    else:
        log.info("ingest_worker.disabled", enable=settings.enable_ingest_worker)
    app.state.ingest_worker = ingest_worker

    yield

    log.info("openclaw-core shutting down")
    if ingest_worker is not None:
        await ingest_worker.stop()
    await projector.stop()
    await appender.stop()
    await writer.close()
    await bus.close()
    await close_audit_pool()


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
    # Audit middleware sits OUTSIDE the routers so it observes every
    # request, including 404s and validation errors. Skip-list inside
    # the middleware excludes /health and /static.
    app.add_middleware(AuditMiddleware)

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(audit_router)

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
