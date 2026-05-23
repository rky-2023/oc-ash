"""Health endpoints.

/health      — liveness; returns 200 as long as the FastAPI process is up.
/health/deps — readiness; checks downstream connectivity.

Phase 2 task 2.5 fills in the deps checks against NATS, immudb, Postgres,
and Vault. For now this returns a structured placeholder so the docker
compose healthcheck and the Phase 2 smoke tests can validate the shape.
"""

from fastapi import APIRouter, Response, status

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
    """
    Phase 2 task 2.5 will replace these placeholders with actual ping checks
    against NATS (`SUB oc.health.ping`), immudb (`db.health()`), Postgres
    (`SELECT 1`), and Vault (`sys/health`). Until then, this endpoint reports
    structure but not real status.
    """
    deps_status = {
        "nats": "not-yet-wired",
        "immudb": "not-yet-wired",
        "postgres": "not-yet-wired",
        "vault": "not-yet-wired",
        "opa": "not-yet-wired",
    }

    # Until all deps are wired we still return 200 — the scaffold is
    # intentionally "live but not ready." Phase 2 task 2.5 flips this to
    # 503 if any required dep is down.
    response.status_code = status.HTTP_200_OK
    return {
        "ok": True,
        "deps": deps_status,
        "note": "Phase 2 task 2.5 wires real readiness checks; scaffold stage",
    }
