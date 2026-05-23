# core/

**openclaw-core** — the central FastAPI service.

Responsibilities:
- HTTP/mTLS edge into the cluster (behind Caddy).
- Mints short-lived service JWTs for MCP servers and agents.
- Hosts the **MCP proxy** (auth → policy → log → forward → log → redact → return).
- Hosts the **A2A router** and orchestrator entrypoints.
- Owns the **audit appender** middleware that mirrors every message to NATS + immudb.

**Stack:** Python 3.13, FastAPI (async), Pydantic v2, SQLModel, asyncpg, nats-py, opa-python-client.

**Status:** scaffold (Phase 2 will populate `app/`).

Layout (planned):
```
core/
  app/
    main.py            FastAPI entrypoint
    audit.py           append-only middleware
    mcp_proxy.py       6-step MCP request pipeline
    a2a_router.py      agent-to-agent dispatcher
    policy.py          OPA client wrapper
    auth/              WebAuthn + JWT mint/verify
    db/                SQLModel models, migrations
  tests/
  pyproject.toml
  Dockerfile
```
