# app.ingest — oc-ingest worker (Phase 3)

Turns external activity into signed `oc.event.*` envelopes on NATS, where the
Phase 2 audit pipeline anchors them. **MVP placement:** runs in-process inside
core (reusing `app.audit.envelope` / `app.audit.signer` / `app.bus`), like the
appender/projector. Splitting into a standalone service with its own
AppRole/mTLS is Phase 11 (ADR-003 D3).

## Sources (one shared publish path)

```
gh-poller (poller.py) ─┐
                       ├─▶ emit.emit_event ─▶ ingest.rego filter ─▶ AuditEnvelope
claude hook ──POST──▶  │       (keep/drop)        (fail-open)        + sig_service
  oc-claude-hook.sh    │                                                  │
  → hooks.py (socket) ─┘                                          NATS (JetStream, msg_id=ulid)
                                                                          │
                                          appender → redaction → immudb → projector → Postgres
```

- **`emit.py`** — the choke point: OPA ingest-filter → build envelope → sign sig_service → publish.
- **`filter.py`** — queries `data.openclaw.ingest.decision`; **fail-open** (keep) if OPA is down.
- **`poller.py`** — GitHub App polling (ADR-004 D1). Pure `diff()` engine (unit-tested); first poll of an endpoint is a baseline (seeds checkpoint, emits nothing).
- **`github.py`** — App JWT → installation token (reuses the attestation publisher's KV-PEM path; see note in-file re: the doc's transit-sign variant).
- **`checkpoint.py`** — per-(repo,endpoint) checkpoints in `openclaw.lookup`.
- **`hooks.py`** — unix-socket receiver for Claude Code hooks + per-session manifest.
- **`worker.py`** — lifecycle; started from `app.main` when `OC_ENABLE_INGEST_WORKER=true`.

## Enabling (host)

```bash
export OC_ENABLE_INGEST_WORKER=true
# needs: OC_VAULT_TOKEN (sign), OC_NATS_URL, OC_OPA_URL, OC_POSTGRES_DSN,
#        OC_GITHUB_APP_ID / OC_GITHUB_INSTALLATION_ID / OC_GITHUB_APP_PRIVATE_KEY
# optional: OC_INGEST_GH_REPOS='[{"owner":"rky-2023","repo":"oc-ash"}]'
#           OC_INGEST_SOCKET=/run/openclaw/ingest.sock
```

Claude hooks: `python ingest/claude-hooks/install-claude-hooks.py --apply` (review the dry-run first).

## Tested

`core/tests/test_ingest_{filter,poller_diff,hooks}.py` — 18 tests covering the
filter fail-open, the diff/baseline/change/trim/subject logic, and hook
payload mapping. Live publish (signing + NATS + GitHub) is operator-gated.
