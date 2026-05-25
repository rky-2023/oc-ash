"""Health endpoints.

/health      — liveness; returns 200 as long as the FastAPI process is up.
/health/deps — readiness; checks downstream connectivity.

Phase 2 task 2.5 fills in the deps checks against NATS, immudb, Postgres,
and Vault. For now this returns a structured placeholder so the docker
compose healthcheck and the Phase 2 smoke tests can validate the shape.
"""

from fastapi import APIRouter, Response, status

from app.audit.immudb_writer import get_writer
from app.bus.nats_client import get_bus

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "service": "openclaw-core",
        "version": "0.1.0",
    }


@router.get("/health/deps", summary="Readiness probe — downstream dependencies")
async def health_deps(response: Response) -> dict[str, object]:
    """Real readiness probes for everything we currently connect to.

    NATS:    queried via the connection's is_connected flag
    immudb:  not yet — appender service owns this in a follow-up PR
    postgres / vault / opa: also follow-ups

    Returns 200 with status detail; flips to 503 if a REQUIRED dep is
    down. For now NATS is the only required dep — if NATS is
    unreachable, audit publishing degrades (no requests are blocked,
    but envelopes are dropped — see middleware._publish_safe).
    """
    bus = get_bus()
    writer = get_writer()
    nats_status = await bus.health_check()
    immudb_status = "ok" if writer.is_connected else "disconnected"

    deps_status = {
        "nats": nats_status,
        "immudb": immudb_status,
        "postgres": "not-yet-wired",
        "vault": "not-yet-wired",
        "opa": "not-yet-wired",
    }

    # `down` for NATS isn't fatal — middleware still serves requests,
    # just doesn't audit. So we return 200 with status==degraded rather
    # than 503. Flip to 503 once a dep becomes load-bearing.
    response.status_code = status.HTTP_200_OK
    return {
        "ok": True,
        "deps": deps_status,
        "summary": "ok" if nats_status == "ok" else "degraded",
    }
