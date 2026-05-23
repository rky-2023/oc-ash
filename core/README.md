# core/

**openclaw-core** — the central FastAPI service.

Responsibilities:
- HTTP/mTLS edge into the cluster (behind Caddy).
- Mints short-lived service JWTs for MCP servers and agents.
- Hosts the **MCP proxy** (auth → policy → log → forward → log → redact → return).
- Hosts the **A2A router** and orchestrator entrypoints.
- Owns the **audit appender** middleware that mirrors every message to NATS + immudb.

**Stack:** Python 3.13, FastAPI (async), Pydantic v2, SQLModel, asyncpg, nats-py, opa-python-client.

**Status:** Phase 2 scaffold landed. `app/main.py`, `app/config.py`, and `app/health.py` are functional placeholders. Phase 2 tasks 2.5–2.14 fill in the rest.

## Local development

```sh
cd core/
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
uvicorn app.main:app --reload --port 8000
```

See [`docs/DEVELOPMENT.md`](../docs/DEVELOPMENT.md) for the full development setup (SSH commit signing, branch/PR workflow, docker compose).

## Layout

```
core/
  app/
    __init__.py        package marker (sets __version__)
    main.py            FastAPI entrypoint + lifespan hooks
    config.py          Pydantic-Settings, env + vault-agent-rendered secrets
    health.py          /health (liveness) + /health/deps (readiness placeholder)
    # Phase 2 will add:
    # audit/           append-only middleware + envelope signer + redaction
    # mcp_proxy.py     6-step MCP request pipeline
    # a2a_router.py    agent-to-agent dispatcher
    # auth/            WebAuthn + JWT mint/verify (some already from Phase 1 task 1.8)
    # db/              SQLModel models, migrations
  tests/
    test_health.py     scaffold smoke
  pyproject.toml
  Dockerfile
  .dockerignore
  .python-version
```
