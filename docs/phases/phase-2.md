# Phase 2 — openclaw-core skeleton + event bus + audit ledger

> **Goal:** Stand up the spine. Nothing useful for end-users yet, but every message in the system flows through a logged, signed, cryptographically anchored pipeline.
> **Anchors:** ADR-001 D2 / D3 / D4 (FastAPI core, NATS JetStream bus, immudb ledger), ADR-003 (entire ADR — audit envelope, two-phase append, redaction, attestation), ADR-002 D6/D7/D13 (mTLS + per-call JWTs + transit signing keys live).
> **Estimated wall-clock:** 4–5 working days.
> **Output:** Any HTTP request to core appears in the audit viewer within 1 s with full request/response. `oc audit verify <yesterday>` returns OK. Empty bus subscribers can be plugged into `oc.*` and they get signed envelopes.
> **Parallel work eligible during this phase:** Phase 9 RN scaffold (`docs/phases/phase-9-prep.md`, future) — RN dev has no Phase 2 dependency.

---

## Prerequisites (Phase 1 must be done)

Don't start until **all** of these are true:

- [ ] Vault is unsealed and admin works via AppRole.
- [ ] `pki_int/` is issuing 24-hour Ed25519 mTLS leafs.
- [ ] Transit keys live: `core-jwt`, `audit-appender`, `attestation-2026-q2`. **Add now if missing:** `audit-service` (for `sig_service` per ADR-003 D2), `audit-pii/v3` (for encrypt-path redaction per ADR-003 D6).
- [ ] Both YubiKeys enrolled; full login/refresh/revoke loop verified.
- [ ] `tests/phase-1-smoke.sh` all green.
- [ ] Existing Postgres (the Ashboard one) is reachable from the openclaw containers' network namespace.
- [ ] Existing Grafana + Prometheus reachable (they live in the Ashboard monitoring stack).

If any box is unchecked, fix it in Phase 1 before starting Phase 2.

---

## Phase 2 task list

### 2.1 Provision the Postgres schema for openclaw  ✅ executed 2026-05-23

**Script:** [`infra/scripts/06-apply-postgres-schema.sh`](../../infra/scripts/06-apply-postgres-schema.sh)
**Schema:** [`infra/postgres/openclaw-schema.sql`](../../infra/postgres/openclaw-schema.sql)

The script auto-detects whichever Postgres is running on the host (docker container OR system service — connects via `sudo -u postgres psql` for the latter). For this deployment it found the system Postgres 18 from `postgresql@18-main.service` on port 5432 (Ashboard's docker postgres couldn't bind because the system one already had the port). Applied DDL + rotated `openclaw_app` password into `kv/openclaw/postgres/app`. Final state: schema `openclaw`, role `openclaw_app`, table `openclaw.lookup` with 9 seed rows.


**Why:** The audit projection (ADR-003 D5) and a few core tables (sessions, WebAuthn credentials, AppRole bindings) need a Postgres home. Sharing the Ashboard instance is intentional (ADR-001 D1) but separation at the schema level is required.

**Steps:**
- Connect to the existing Postgres as a superuser.
- `CREATE SCHEMA openclaw AUTHORIZATION openclaw_app;`
- `CREATE ROLE openclaw_app LOGIN PASSWORD :vault_managed;` (the password lives in `kv/openclaw/postgres/app`, set by Vault now and rotated quarterly).
- `GRANT USAGE ON SCHEMA openclaw TO openclaw_app;` — no `ashboard` schema access.
- Set `search_path = openclaw, public` for the new role.
- Add the role's connection string to `kv/openclaw/postgres/app` so vault-agent can render it.

**Verify:** `psql` as `openclaw_app` can `CREATE TABLE openclaw.foo (...)` and **cannot** `SELECT FROM ashboard.households`.

**Also create the `openclaw.lookup` table** (per ADR-002 D12 — low-value config that doesn't belong in Vault):

```sql
CREATE TABLE openclaw.lookup (
  key         TEXT PRIMARY KEY,            -- e.g., 'oauth.google.calendar.client_id'
  value       JSONB NOT NULL,
  description TEXT,
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by  TEXT NOT NULL                -- 'rky' or service name; audit-relevant but not secret
);

COMMENT ON TABLE openclaw.lookup IS
  'Public-by-design config: OAuth client IDs, GitHub App IDs, FCM project IDs, '
  'WebAuthn RP config, redaction policy hashes. Anything NOT public goes in Vault.';
```

Populate the seed values referenced by Phase 1 task 1.5 here:
- `oauth.google.calendar.client_id`
- `oauth.google.gmail.client_id`
- `github-app.openclaw-bot.app_id`
- `github-app.openclaw-bot.installation_ids` (JSONB array)
- `fcm.project_id`
- `webauthn.rp_id`, `webauthn.rp_name`
- `redaction-policy.v3.sha256`

---

### 2.2 Provision the dm-crypt volume for immudb

**Why:** Same logic as Vault's volume — wrapped/encrypted data at rest, no clear-text bytes accessible to a host backup tool.

**Steps:**

The dm-crypt setup is already covered by Phase 1 task 1.1 (`infra/scripts/00-create-luks-volumes.sh` creates the immudb LUKS-on-file container at the same time as Vault's). Confirm:

- `mountpoint /mnt/openclaw/immudb` returns success.
- `cryptsetup status oc-immudb-luks` shows the mapping active.

If you bootstrapped before the snap-Docker mount-path fix and your volumes are at `/var/lib/openclaw/<svc>`, run `migrate-mount-paths-to-mnt.sh` first.

**Verify:** `cryptsetup status oc-immudb-luks` active; `lsblk` shows the LUKS layer at `/dev/mapper/oc-immudb-luks` → ext4 → `/mnt/openclaw/immudb`.

---

### 2.3 Install and run immudb  ✅ executed 2026-05-23

**Script:** [`infra/scripts/07-immudb-bootstrap.sh`](../../infra/scripts/07-immudb-bootstrap.sh)

The script (post-fixes) is `expect`-driven because the codenotary/immudb image is **distroless** (no `/bin/sh`, no `chown`, no coreutils) AND `immuadmin login` accepts passwords only via interactive TTY prompt (no `--password` flag, no env var). Pre-chown of `/mnt/openclaw/immudb` to uid 3322 happens on the host before container start. Forced first-login password change handled inline. Final state: database `openclaw_audit`, users `appender` (`readwrite`) + `projector` (`read`), all 3 passwords in `kv/openclaw/immudb/{admin,appender,projector}`.

**Gotchas hit + recorded in the script:**
- snap-Docker has its own `/tmp` namespace — `docker cp` from host `/tmp` failed
- distroless image: no `sh` for `sh -c` wrappers, no `chown` for permission fixes
- `immuadmin` prompt: `read` not `readonly` for the readonly permission name
- `vault kv put PATH -` doesn't mean stdin (needs `@-` + JSON); we use direct args


**Why:** The WORM audit ledger (ADR-003 D1).

**Steps:**
- Pull the official immudb container image. Pin to a specific digest (`@sha256:...`), not a tag, per ADR-001's supply-chain stance.
- Compose service:
  - User: dedicated `immudb` user (uid > 10000).
  - Mounts: `/mnt/openclaw/immudb/` → `/var/lib/immudb`.
  - Network: internal docker network only; no port published to host.
  - Args: `--dir /var/lib/immudb --signingKey /run/secrets/immudb-signing.key` (immudb has native server-side state signing).
- Bootstrap: change the default admin password immediately, store new password in `kv/openclaw/immudb/admin`.
- Create database `openclaw_audit`.
- Create user `appender` with R/W on `openclaw_audit` only; password in `kv/openclaw/immudb/appender`.
- Create user `projector` with R-only on `openclaw_audit`; password in `kv/openclaw/immudb/projector`.

**Verify:** `immuclient` from inside the docker network can connect, write a test entry, read it back with a proof; same client from outside the docker network is refused.

---

### 2.4 Install and run NATS JetStream  ✅ executed 2026-05-23

**Script:** [`infra/scripts/08-nats-bootstrap.sh`](../../infra/scripts/08-nats-bootstrap.sh)
**Stream config:** [`infra/nats/streams.yaml`](../../infra/nats/streams.yaml)

Pre-chown of `/mnt/openclaw/nats` to uid 1000. NATS server image is also minimal — no `nats` client CLI, no shell. Stream creation uses the **`natsio/nats-box`** companion image (which has the `nats` CLI) running on the same docker network. Readiness probe via `docker inspect` (not shell). Final state: 5 streams created — OC_EVENT (7d), OC_A2A (30d), OC_MCP (30d), OC_NOTIFY (7d), OC_HEALTH (1h).

**JWT operator auth deferred** — the NATS container is internal-network-only via the `oc-internal` docker network. Acceptable v1; Phase 11 hardening adds operator JWTs.


**Why:** The bus (ADR-001 D3). Every `oc.*` message flows here first.

**Steps:**
- Pull the official `nats` container image, pinned by digest.
- Compose service:
  - User: dedicated `nats` user.
  - Storage: `/mnt/openclaw/nats/` (its own dm-crypt volume from Phase 1 task 1.1).
  - JetStream enabled, file-based storage, **AES-256-GCM at rest** (`server.tls` not for client conn — internal-only — but `jetstream.encryption` on).
  - Bind to internal docker network only.
- Operator-level credentials: NATS has its own JWT system; generate operator + account + user JWTs offline, store private keys in Vault, distribute public bits to NATS config. Future ADR-005 may unify NATS auth with Vault PKI; for v2.0 use NATS-native creds.
- Streams to create (declarative config in `infra/nats/streams.yaml`):

  | Stream name | Subjects | Retention | Max-age | Dedupe window |
  |---|---|---|---|---|
  | `OC_EVENT` | `oc.event.>` | limits | 7 d | 1 m |
  | `OC_A2A`   | `oc.a2a.>`   | limits | 30 d | 1 m |
  | `OC_MCP`   | `oc.mcp.>`   | limits | 30 d | 1 m |
  | `OC_NOTIFY`| `oc.notify.>`| limits | 7 d | 1 m |
  | `OC_HEALTH`| `oc.health.>`| limits | 1 h | none |

- Per-account access: each downstream service gets a NATS user with publish/subscribe permissions limited to the subjects it needs.

**Verify:** `nats stream ls` shows all five streams. `nats pub oc.health.ping ok` from inside the network works; same `nats pub` from outside the network fails.

---

### 2.5 Scaffold openclaw-core — bus wiring + audit envelope  🟡 partial 2026-05-23

**Status:** the bus + envelope foundation is shipped. immudb writer, Vault transit signing, Postgres connection, and mTLS deferred to follow-up PRs (each focused).

**What landed in this PR:**

| File | Purpose |
|---|---|
| `core/app/audit/envelope.py` | Pydantic v2 model implementing the ADR-003 D2 schema (ulid, ts, subject, conv_id, actor, action, direction, hashes, policy, redacted_payload, encrypted_blobs, prev_hash, sig_service, sig_appender). Canonical serialization for signing + a hash helper for chaining. |
| `core/app/audit/signer.py` | Process-local HMAC-SHA256 signer (same MVP trade-off as auth/sessions.py). Per-envelope `service` / `appender` slots. Vault transit `transit/sign/audit-service` is the next-PR target. |
| `core/app/bus/nats_client.py` | Async NATS connection + JetStream context. Single-singleton via `get_bus()`. Auto-reconnect forever. `publish(subject, bytes, msg_id=)` uses JS ack so envelopes land in the right stream. |
| `core/app/audit/middleware.py` | FastAPI middleware emits a request envelope BEFORE and a response envelope AFTER each handler. Skip-list for /health and /static. Non-blocking on publish failure (logs warning, request continues). Stamps `X-OC-Conv-Id` on every response. |
| `core/app/main.py` | Lifespan opens NATS at startup, closes on shutdown. `AuditMiddleware` added before routers. |
| `core/app/health.py` | `/health/deps` now reports real NATS status (ok / reconnecting / down). |
| `core/tests/test_envelope.py` | 8 tests — round-trip, canonical determinism, sign/verify both slots, tamper detection, prev_hash chain. |
| `core/tests/test_audit_middleware.py` | 4 tests — skip-list, conv-id header on non-skipped responses, 404 still audited, POST body passes through middleware to handler. |

**Important property the design preserves:** if NATS is down, requests still succeed — they just don't get audited. Per ADR-003 D4 the appender-side has a paranoid-mode halt at >24h immudb lag, but the FRONT-end (this middleware) never gates traffic on audit health. Audit is "record truth, don't gate traffic."

**Next code-heavy PR (Phase 2 task 2.5b + 2.6):** swap signer to Vault transit, render secrets via vault-agent sidecar, add the `audit-appender` standalone service that subscribes to `oc.event.>` and writes to immudb.

**Why:** The FastAPI service that owns the audit appender, the MCP proxy (Phase 4), the A2A router (Phase 4), and the HTTP edge handlers. Phase 1 stood up a minimal version for WebAuthn; Phase 2 extends it.

**Steps:**
- Initialize the Python project in `core/`:
  - `pyproject.toml` with `python = "^3.13"`.
  - Pinned-by-hash deps via `pip-tools` or `uv`. Key deps: `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `sqlmodel`, `asyncpg`, `nats-py`, `immudb-py`, `httpx`, `python-jose[cryptography]`, `webauthn`, `opa-python-client`, `prometheus-client`, `structlog`.
  - Lockfile committed.
- Project layout:

  ```
  core/
    app/
      main.py              FastAPI entrypoint
      config.py            Pydantic-Settings, all from Vault via env
      auth/                (already exists from Phase 1)
        webauthn.py
        jwt.py
      db/
        models.py          SQLModel definitions
        session.py         asyncpg + sessionmaker
        migrations/        Alembic
      bus/
        nats_client.py     publish + subscribe wrappers
      audit/
        envelope.py        Pydantic model for ADR-003 D2 schema
        appender.py        signer + immudb writer
        redaction.py       OPA client wrapper
      observability/
        metrics.py         prometheus-client
        tracing.py         OTEL hooks (no exporter yet)
        log.py             structlog config
    tests/
    Dockerfile
  ```
- Configuration is pulled from env, which is rendered by vault-agent from `kv/openclaw/core/*` paths. No secret in compose, no secret in code.
- `main.py` starts: opens NATS connection, opens immudb connection, runs Alembic migrations on boot, listens on mTLS-only.

**Verify:** `docker compose up core` starts; `/health` returns 200 with mTLS + JWT; `/health/deps` reports NATS-ok, immudb-ok, Postgres-ok.

---

### 2.6 Vault transit signing for audit envelopes  ✅ 2026-05-24

**Why:** ADR-002 D12 / ADR-003 D2. Replace the process-local HMAC key (invalidated on restart) with Vault transit Ed25519 keys that persist across restarts and auto-rotate monthly.

**What changed:**

| File | Change |
|---|---|
| `core/app/audit/signer.py` | Full rewrite. `sign_envelope` / `verify_envelope` are now async. Vault transit used when `OC_VAULT_TOKEN` is set and `OC_VAULT_SIGNING_ENABLED=true` (default). HMAC fallback when `vault_signing_enabled=false` (unit tests). Handles both `ed25519-transit:vault:v1:*` and legacy `hmac-sha256:*` sig formats so old envelopes remain verifiable during the transition. |
| `core/app/audit/middleware.py` | `sign_envelope(...)` → `await sign_envelope(...)` (×2). |
| `core/app/audit/appender.py` | Same; also `await verify_envelope(...)`. |
| `core/app/cli/audit_cmds.py` | `verify_envelope(...)` → `await verify_envelope(...)` in `_run_verify`. |
| `core/app/config.py` | Added `vault_token`, `vault_transit_key_service` (`audit-service`), `vault_transit_key_appender` (`audit-appender`), `vault_signing_enabled`. |
| `core/scripts/run-with-vault-creds.sh` | Exports `OC_VAULT_TOKEN` (previously unset after fetching immudb creds). |
| `core/scripts/oc-with-vault-creds.sh` | Same — exports `OC_VAULT_TOKEN` for `oc audit verify`. |
| `infra/scripts/09-vault-transit-audit-keys.sh` | One-time bootstrap: creates `transit/keys/audit-service` and `transit/keys/audit-appender` (Ed25519, auto-rotate 30d, min-decrypt-version=1). |
| `core/tests/conftest.py` | Sets `OC_VAULT_SIGNING_ENABLED=false` + `OC_ENABLE_AUDIT_APPENDER=false` for the whole test suite. |
| `core/tests/test_signer.py` | 7 unit tests for the HMAC fallback path (sign/verify round-trip, tamper detection, slot independence, error cases). |

**Signature wire format:**
- `ed25519-transit:vault:v1:<base64>` — live Vault (production)
- `hmac-sha256:<hex>` — HMAC fallback (tests / no-Vault dev)

Both formats are recognised by `verify_envelope` so old envelopes stay verifiable.

**Bootstrap (run once before starting core):**
```sh
./infra/scripts/09-vault-transit-audit-keys.sh
```
Then restart core via `run-with-vault-creds.sh` — it now exports `OC_VAULT_TOKEN`.

**Token lifetime:** AppRole TTL is 15 min (configured in task 1.4). Restart core to refresh. Vault-agent sidecar (task 1.12) will replace this with auto-renewal.

---

### 2.6-original Build the audit envelope model + signer

**Why:** ADR-003 D2 defines the exact envelope. Pin it now; everything else assumes it.

**Steps:**
- `core/app/audit/envelope.py`: Pydantic model exactly matching ADR-003 D2 fields. Strict mode (`model_config = ConfigDict(strict=True, extra="forbid")`).
- Canonicalization: serialize to JSON with `sort_keys=True, separators=(",", ":")`, no whitespace, no float precision drift. Compute SHA-256 of canonical bytes for `prev_hash` chaining.
- `prev_hash` source: read the latest entry's full-envelope hash from immudb on each append; cache it in memory and update atomically.
- Signing:
  - `sig_service` — Vault transit `transit/sign/audit-service` (Ed25519).
  - `sig_appender` — Vault transit `transit/sign/audit-appender` (different key, different policy).
  - Both signatures cover the entire envelope minus the signature fields themselves (sign-then-attach pattern).
- Verification helper that takes an envelope and returns `(valid: bool, reasons: list[str])` — used by the projector and by `oc audit verify`.

**Verify:** Unit tests: round-trip serialize/deserialize is deterministic; tampered byte produces signature failure; `prev_hash` chain breaks if any entry is replaced.

---

### 2.7 Build the redaction pipeline (pre-append)

**Why:** ADR-003 D6 — redaction happens **before** the envelope is signed and persisted. The pipeline must be deterministic and version-pinned.

**Steps:**
- Stand up OPA as a sidecar container or co-located process. Load `policy/redaction.rego` from a signed bundle path.
- Redaction policy v1 (starter set):
  - **Drop** rules: any field matching `(?i)(api[_-]?key|secret|token|password|authorization|cookie)` keys; values with high Shannon entropy + length ≥ 20.
  - **Encrypt** rules: fields named `body`, `subject` under `oc.event.mail.*`; `description`, `summary` under `oc.event.calendar.*`; file contents under `oc.mcp.fs-asher.*`.
  - **Keep** rules: everything else by default; explicit keep-list for `actor`, `subject`, `ts`, `conv_id`, etc.
- `core/app/audit/redaction.py` wraps the OPA call:
  - Input: raw payload + envelope metadata.
  - Output: `(redacted_payload, encrypted_blobs[], policy_decision_log)`.
  - Encrypt path: per-message AES-256-GCM, key wrapped by Vault transit `audit-pii/v3`, ciphertext + wrapped DEK stored in `encrypted_blobs[]`.
- Version pin: every envelope records `policy.redaction_version = "<sha256 of redaction.rego>"`. Rotation = bump version, restart appender; old envelopes keep their old version.

**Verify:**
- Drop test: payload `{"api_key": "sk_live_..."}` produces `redacted_payload = {"api_key": "<redacted:secret>"}` and no `encrypted_blobs` entry. Original value is **not** recoverable from the envelope (verify by string-search).
- Encrypt test: payload `{"body": "..."}` under `oc.event.mail.*` produces `redacted_payload.body = "<encrypted:vault://transit/audit-pii/v3>"` and a corresponding `encrypted_blobs` entry; decryption with valid auth returns original.
- Version test: changing the rego file changes the recorded `redaction_version`.

---

### 2.8 Build the audit-appender service  🟡 MVP in-process 2026-05-23

**Status:** the appender runs as a background task INSIDE the openclaw-core process for MVP. Per ADR-003 D3 the canonical design is a separate service — that split happens in Phase 11 hardening (or earlier if needed). The `appender.py` module's interface is intentionally narrow so the in-process → separate-container refactor is a straightforward extraction.

**Files:**

| File | What |
|---|---|
| `core/app/audit/immudb_writer.py` | Async wrapper around immudb-py (sync client → asyncio.to_thread). Single connection, thread-locked. `set_envelope(key, value)` returns the immudb tx id. `get_latest_key()` for prev_hash chain seeding on restart. |
| `core/app/audit/appender.py` | `AuditAppender` background task. Subscribes to `oc.event.>` / `oc.a2a.>` / `oc.mcp.>` / `oc.notify.>` via durable JetStream consumers (deliberately NOT `oc.health.>` — those are pings). Per message: parse → verify sig_service → fill prev_hash from in-memory chain head → sign sig_appender → write to immudb → update chain head → ACK NATS. NACK on immudb write failures (NATS will redeliver). |
| `core/app/main.py` | Lifespan brings up writer + appender if `settings.enable_audit_appender` is true AND `OC_IMMUDB_PASSWORD` is set. Otherwise both stay no-ops; front-end NATS publishes still work. |
| `core/app/config.py` | New env settings: `OC_IMMUDB_USER` (default `appender`), `OC_IMMUDB_PASSWORD`, `OC_IMMUDB_DATABASE` (default `openclaw_audit`), `OC_ENABLE_AUDIT_APPENDER`. |
| `core/scripts/run-with-vault-creds.sh` | Wrapper: prompts for openclaw-admin AppRole, fetches `kv/openclaw/immudb/appender` password from Vault, exports env, execs uvicorn. MVP stand-in for the vault-agent sidecar of Phase 2 task 2.5b. |
| `core/tests/test_appender_lifecycle.py` | 3 sanity tests: stop-without-start is no-op, subject list matches ADR-001 D3 taxonomy, envelope canonical sha256 returns `sha256:<64-hex>`. End-to-end "envelope lands in immudb" lives in integration tests (future). |

**Running it:**

```sh
cd /home/asher/openclaw/core
./scripts/run-with-vault-creds.sh
# Prompts for openclaw-admin Role ID + Secret ID once.
# Fetches immudb appender password from Vault.
# Launches uvicorn with all required env.
```

Hit any non-skip-listed endpoint (`curl https://...:8000/auth/health`); two envelopes should land on `oc.event.core.request.get` + `oc.event.core.response.get`, then propagate through the appender into immudb. Verify with:

```sh
docker exec -it oc-immudb immuadmin login immudb        # admin pw from Vault
docker exec oc-immudb immuadmin database use openclaw_audit
# Within the immuclient REPL, scan for keys — they'll be ULIDs.
```

**Deferred** (Phase 2 follow-ups):
- Swap signer to Vault `transit/sign/audit-{service,appender}` so signatures persist across core restarts.
- Extract appender into its own container per ADR-003 D3.
- Implement paranoid-mode trigger when immudb lag >24h (ADR-003 D4).
- Postgres projection (task 2.9) reads from immudb to populate `openclaw.audit_*` tables for the viewer.

**Why:** Phase B of the two-phase append (ADR-003 D3). NATS already gives durability; this service gives cryptographic anchoring.

**Steps:**
- New service `core/app/audit/appender.py`, run as its own process / its own compose service for isolation. Has its own AppRole, its own mTLS identity, its own minimal Vault scope (read on `kv/openclaw/audit/*`, sign on `transit/sign/audit-appender`).
- NATS durable consumer named `audit-appender`, subscribes to `oc.>` (everything).
- For each message:
  1. Build envelope (envelope.py).
  2. Redact (redaction.py).
  3. Sign (signer in envelope.py).
  4. Write to immudb `openclaw_audit/entries` keyed by ULID.
  5. ACK the NATS message **only after** the immudb commit succeeds.
- Metrics exposed on `/metrics`:
  - `oc_audit_append_total` counter (labels: `subject`, `outcome`)
  - `oc_audit_append_latency_seconds` histogram (target p95 < 50 ms per ADR-003 D11)
  - `oc_audit_lag_seconds` gauge (NATS publish ts vs. immudb commit ts)
  - `oc_audit_paranoid_mode` gauge (0/1)
- **Paranoid mode** trigger (ADR-003 D4): if `oc_audit_lag_seconds > 86400` sustained for 60 s, set the gauge to 1 and publish to `oc.health.paranoid`. The MCP proxy (Phase 4) refuses tool calls when this is set. Clearing requires `oc audit unwedge --confirm`.

**Verify:**
- Publish a test message to `oc.event.test`. Within 500 ms it appears in immudb with both signatures valid and the lag metric < 0.5 s.
- Kill the appender mid-stream; restart; it resumes from its durable consumer position with no duplicates and no gaps. Test with a synthetic 1000-message burst.

---

### 2.9 Build the audit-projector service  🟡 MVP 2026-05-24

**Why:** Postgres projection for fast viewer reads (ADR-003 D5). immudb is the source of truth; this is the O(matches) query layer used by `oc audit replay` / `verify` and the viewer.

**Files:**

| File | Purpose |
|---|---|
| `core/app/db/migrations/001_audit_projection.sql` | DDL: `audit_entries`, `audit_conversations`, `audit_policy_decisions`, `audit_checkpoint`. All idempotent. |
| `core/app/audit/projector.py` | `AuditProjector` background task. Polls immudb every `OC_PROJECTOR_POLL_SECONDS` seconds; verifies both sigs + prev_hash; writes to Postgres atomically (entry + checkpoint in one txn). |
| `core/app/config.py` | Added `postgres_dsn`, `enable_audit_projector`, `projector_poll_seconds`, `effective_postgres_dsn` property. |
| `core/app/main.py` | Wired projector into lifespan; starts after appender. |
| `core/pyproject.toml` | Added `asyncpg>=0.30`. |
| `infra/scripts/10-apply-audit-projection-schema.sh` | One-time bootstrap: applies the migration to Postgres. |
| `core/tests/test_projector.py` | Structural unit tests (no live DB). |

**Bootstrap (run once):**
```sh
./infra/scripts/10-apply-audit-projection-schema.sh
```
Then set `OC_POSTGRES_DSN` in `run-with-vault-creds.sh` (see next step below) and restart core.

**Postgres DSN in run-with-vault-creds.sh (manual for now):**
Add after the immudb fetch:
```bash
PG_DSN=$(docker exec -e VAULT_TOKEN="$TOK" oc-vault \
  vault kv get -field=url kv/openclaw/postgres/app)
export OC_POSTGRES_DSN="$PG_DSN"
```
(Phase 2 task 2.5b's vault-agent sidecar will automate this.)

**Integrity policy:** Bad sigs or chain breaks are projected with `sig_*_valid=false` — the projector does NOT halt. The viewer surfaces the flag; `oc audit verify` counts them.

**Deferred:**
- ✅ `oc audit replay --fast` and `oc audit verify --fast` use the Postgres projection index (O(day) not O(all)); implemented in `core/app/cli/audit_cmds.py`.
- Projector extraction into its own container — Phase 11 hardening.

---

### 2.9-original Build the audit-projector service

**Why:** Postgres projection for fast viewer reads (ADR-003 D5). immudb is the source of truth; this is denormalized cache.

**Steps:**
- New service `core/app/audit/projector.py` (separate process), AppRole with `kv/openclaw/audit/projector/*` read + Postgres write.
- Subscribes to immudb's stream scan (`db.streamScan` API) starting from a checkpoint stored in `openclaw.audit_checkpoint`.
- For each immudb entry:
  - Parse envelope.
  - Verify both signatures + prev_hash chain. Mismatch → log error, halt projection (do not pollute the projection with bad data).
  - Insert into `openclaw.audit_entries`, upsert `openclaw.audit_conversations`, insert into `openclaw.audit_policy_decisions` if applicable.
  - Idempotent on ULID (primary key).
  - Advance checkpoint atomically with the insert (single transaction).
- Migration files for the three tables in `core/app/db/migrations/`.

**Verify:**
- Publish a message via the appender; check it lands in Postgres within 2 s.
- Drop all `audit_*` tables; restart projector with checkpoint reset; full replay completes from immudb without errors. Document this procedure in the runbook for Phase 12.

---

### 2.10 Build the audit viewer (Next.js, read-only)  🟡 MVP 2026-05-24

**Files:**

| File | Purpose |
|---|---|
| `core/app/views/audit.py` | FastAPI JSON API — 5 endpoints (entries list/detail, conversations list/detail, verify). All reads from Postgres. Lazy asyncpg pool, closed in lifespan. |
| `core/app/main.py` | Wired `audit_router` + `close_audit_pool` into lifespan. |
| `core/scripts/run-with-vault-creds.sh` | Fetches `kv/openclaw/postgres/app` password, constructs and exports `OC_POSTGRES_DSN` so the projector starts. |
| `audit/viewer/` | Next.js 14 App Router (TypeScript). |
| `audit/viewer/lib/api.ts` | Typed fetch wrappers for all 5 API endpoints. |
| `audit/viewer/lib/types.ts` | TypeScript types matching FastAPI response models. |
| `audit/viewer/app/page.tsx` | `/` — recent 200 entries, paginated, sig badges. |
| `audit/viewer/app/conv/[conv_id]/page.tsx` | `/conv/:id` — full conversation ladder diagram. |
| `audit/viewer/app/entry/[ulid]/page.tsx` | `/entry/:ulid` — single entry detail + integrity status. |
| `audit/viewer/app/verify/page.tsx` | `/verify` — server-side chain verification with date picker. |

**API endpoints (on core FastAPI):**
```
GET /api/audit/entries?limit&offset&subject  → EntryRow[]
GET /api/audit/entries/{ulid}                → EntryRow (with raw_envelope)
GET /api/audit/conversations?limit&offset    → ConversationRow[]
GET /api/audit/conversations/{conv_id}       → EntryRow[]
GET /api/audit/verify?date                   → VerifyResult
```

**Run the viewer:**
```sh
cd audit/viewer
cp .env.example .env.local
# Edit .env.local: OC_API_BASE=https://<tailnet-host>:8000
pnpm install   # or npm install
pnpm dev       # http://localhost:3000
```

**Deferred:**
- mTLS gate (Phase 11 hardening — the tailnet already provides network-level isolation).
- "Reveal" button on encrypted blobs — needs Phase 1 task 1.9 YubiKey enrollment first.
- ✅ `oc audit replay --fast` / `oc audit verify --fast` wired to Postgres projection.

---

### 2.10-original Build the audit viewer (Next.js, read-only)

**Why:** Human eyes on the audit log. Two views per ADR-001 D4 / PLAN.md Phase 2: structured + narrative.

**Steps:**
- `audit/viewer/` — new Next.js 14 app, TypeScript, App Router. Pull deps with pnpm + frozen lockfile.
- Routes:
  - `/` — recent activity (last 200 entries, paginated).
  - `/conv/[conv_id]` — full conversation, two tabs: **Structured** (JSON tree, collapsible) and **Narrative** (ladder diagram per PLAN.md Phase 2 example).
  - `/entry/[ulid]` — single entry detail, signature verification status, raw envelope.
  - `/verify` — operator UI for `oc audit verify` (runs server-side, returns green/red).
- All data reads via a tiny `core/app/views/audit.py` JSON API (the viewer never touches immudb or Postgres directly).
- mTLS-gated; only reachable on the tailnet.
- "Reveal" buttons on encrypted blobs trigger a fresh WebAuthn challenge (YubiKey touch) before decrypting — Phase 1's WebAuthn flow extended with an `acts_as = "reveal"` claim that auto-expires in 30 s.

**Verify:**
- Push 50 mixed test messages; visit `/`; all 50 visible.
- Open a multi-message conversation; toggle Structured / Narrative; both views render correctly.
- Reveal an encrypted blob: requires touch; subsequent reveals within 30 s do not re-prompt; after 30 s do prompt again.

---

### 2.11 Build the `oc` CLI (audit subcommands)  🟡 MVP 2026-05-24

**Why:** Operator-friendly verification + replay (ADR-003 D8 + D10). Lives in `core/app/cli/` and is exposed as a console script — installable via `pip install -e .` from the core venv.

**Status:** three core subcommands shipped — `tail`, `replay`, `verify`. Deferred subcommands (`unwedge`, `projection rebuild`, `auth login` caching) wait on the features they manage.

**Files:**

| File | Purpose |
|---|---|
| `core/app/cli/__init__.py` | Module-level docstring listing env contract. |
| `core/app/cli/main.py` | Click entrypoint registered as `[project.scripts] oc = "app.cli.main:cli"`. |
| `core/app/cli/audit_cmds.py` | `audit` subcommand group: `tail` / `replay` / `verify`. |
| `core/app/cli/_render.py` | Pretty-printers — one-line tail summary + ladder diagram for replay (per ADR-003 D10). |
| `core/scripts/oc-with-vault-creds.sh` | Wrapper that fetches `kv/openclaw/immudb/projector` via the openclaw-admin AppRole and execs `oc` with env injected. Skip if `OC_IMMUDB_PASSWORD` already set. |
| `core/tests/test_cli.py` | 4 Click smoke tests — top-level help, audit-group help, version, bad-date input validation. |

**Subcommands:**

- `oc audit tail [--subject PATTERN] [--json]` — async NATS subscription. Default pattern `oc.>` captures everything. One-line summary by default; full envelope JSON with `--json`.
- `oc audit replay <conv_id> [--json]` — scans immudb for envelopes with the given conv_id; renders the ADR-003 D10 ladder. `--json` switches to raw envelopes.
- `oc audit verify [--date YYYY-MM-DD]` — iterates the day's envelopes, recomputes both signature slots + walks the prev_hash chain. Exit 0 on pass, 1 on signature/chain breaks, 2 on bad input.

**Run it after merge:**

```sh
cd /home/asher/openclaw/core
source .venv/bin/activate
pip install -e .                          # picks up the new oc entrypoint

# One-shot wrapper that fetches projector password from Vault
./scripts/oc-with-vault-creds.sh audit tail
# Hit your core endpoint in another terminal → envelopes scroll past here

./scripts/oc-with-vault-creds.sh audit replay <conv_id>
./scripts/oc-with-vault-creds.sh audit verify --date 2026-05-23
```

**Performance note:** `replay` and `verify` currently iterate **all** envelopes in immudb (filtering in Python). Phase 2 task 2.9's audit-projector adds the Postgres index that makes these O(matches) instead of O(total). For dev-scale ledgers (thousands of entries) the brute-force iteration is fine.

**Deferred:**
- `oc audit unwedge --confirm` — waits on paranoid-mode wiring.
- `oc audit projection rebuild` — waits on Phase 2 task 2.9 (projector).
- `oc auth login` token caching — currently env-var-driven via the wrapper.

**Verify (against original acceptance):**
- `oc audit tail` shows messages as they're appended. ✓
- `oc audit verify <yesterday>` works after ≥24 h of operation. (Attestation-comparison part waits on tasks 2.12 + 2.13.)
- `oc audit replay` outputs structured ladder; matches viewer's narrative tab once task 2.10 ships.

---

### 2.12 Create the `openclaw-attestations` GitHub repo and the App credential  ✅ executed 2026-05-25

**Why:** ADR-003 D7 needs an external publish target. The attestation publisher (next task) needs a credential to push.

**Steps:**
- **[hands-on]** Create `rky-2023/openclaw-attestations` on GitHub. Public. No README, no license at creation (publisher will add a README and `verify.py`).
- **[hands-on]** Create a GitHub App `openclaw-bot`:
  - Owner: `rky-2023`.
  - Webhook: disabled (we're not consuming GH events in Phase 2; that's Phase 3 + Phase 7).
  - Permissions: **contents:write** on `openclaw-attestations` only for now. (Phase 7 will install the same app on `oc-ash` with additional permissions.)
  - Private key: generate, download. **Don't put it in git.**
- Import the private key into Vault transit (sign-only) as `transit/keys/github-app-openclaw-bot`:

  ```
  vault transit import-version transit/keys/github-app-openclaw-bot key=@bot.private-key.pem type=rsa-2048
  ```

  Then `shred -u bot.private-key.pem` — the key now exists only in Vault, sign-only.
- Store the App ID and installation ID in `kv/openclaw/github-app/openclaw-bot`.

**Verify:** `vault read transit/keys/github-app-openclaw-bot` shows the key version; export attempt returns 403.

---

### 2.13 Build the attestation-publisher job  ✅ executed 2026-05-26 — first attestation committed to openclaw-attestations (commit b026a55)

**Why:** The daily public Merkle root (ADR-003 D7) is what makes the audit ledger externally tamper-evident. Without this, the rest of Phase 2 is just an expensive log.

**Steps:**
- New service / systemd timer `attestation-publisher`, runs daily at 03:00 local.
- Procedure:
  1. Query immudb for all entries with `ts` between 00:00 and 23:59:59 the previous day.
  2. Recompute the Merkle root over those entries (use immudb's consistency proof + verify independently with our own hash chain).
  3. Build the attestation document per ADR-003 D7 schema (version, date, entries count, first/last ULID, merkle_root, prev_day_merkle, policy_hash, signed_by, signature).
  4. Sign with `transit/sign/attestation-2026-q2`.
  5. Authenticate to GitHub:
     - Mint a JWT signed by Vault transit `github-app-openclaw-bot` (10-minute TTL per GitHub spec).
     - Exchange for an installation access token (1-hour TTL).
  6. Clone the attestations repo to a tmpfs path; add `attestations/YYYY/MM/DD.json`; commit with a structured message; push.
  7. On first run: also publish `README.md` (verification instructions), `verify/verify.py`, and `keys/attestation-2026-q2.pub`.
  8. Tear down tmpfs.
- On failure: retry with exponential backoff for 24 h. After 24 h of failures, escalate (Android notification once Phase 8 ships; until then, log loud + email if you have a local SMTP setup).

**Verify:**
- Run manually for a backfill day; check the resulting JSON file in the attestations repo.
- Walk one full day's entries by hand for a small dataset; confirm Merkle root matches.
- `verify/verify.py` runs successfully against the day's attestation with no openclaw dependencies installed.

---

### 2.14 Wire up the audit appender into `core` itself

**Why:** Up to here, the appender consumes from NATS. We also want core's *own* request/response cycle to publish to NATS so it gets audited.

**Steps:**
- Add FastAPI middleware in `core/app/main.py` that:
  - Before each request: assigns a request-scoped ULID, captures method/path/headers (after redaction filter on Authorization, etc.).
  - After each response: publishes one `oc.event.core.request` and one `oc.event.core.response` to NATS, paired by ULID.
  - Failure to publish is non-blocking on the request itself (NATS is up or paranoid mode applies); the request completes regardless.
- The middleware lives in `core/app/audit/middleware.py` and is the same module that downstream MCP proxies (Phase 4) and ingesters (Phase 3) will reuse.

**Verify:** Hit `/health` 10 times; see 20 entries in immudb within 1 s; the viewer's `/` page shows them.

---

### 2.15 Smoke test the whole spine + document

**Why:** Exit criteria validation.

**Tests:**

| # | Action | Expected |
|---|---|---|
| 1 | `curl https://core.<tailnet>.ts.net/health` (with mTLS + JWT) | 200; entry appears in immudb within 500 ms; viewer shows it within 1 s. |
| 2 | Kill `audit-appender` mid-load; restart | No duplicates; no gaps; metrics report catch-up. |
| 3 | Drop Postgres `audit_*` schema; run `oc audit projection rebuild` | Full replay; final row count matches immudb. |
| 4 | Modify a row in `audit_entries` manually; run `oc audit verify <date>` | Returns non-zero with "projection diverges from immudb" — the projection is a cache, the source of truth differs. |
| 5 | Tamper with an immudb entry (if even possible with admin creds) | `oc audit verify` returns non-zero with "Merkle root mismatch". |
| 6 | Run `attestation-publisher` manually for today | New file `attestations/YYYY/MM/DD.json` in the attestations repo; `verify/verify.py` returns 0. |
| 7 | Disconnect immudb; observe `oc_audit_lag_seconds` climb past 60 s; reconnect | Alert fired; no message loss after reconnect; paranoid mode did NOT trigger (it's 24 h, not 60 s). |

Add the test script under `tests/phase-2-smoke.sh`. Commit it.

Update PLAN.md change log noting Phase 2 done. Add to `docs/RUNBOOK.md` (stub): "How to rebuild the Postgres projection from immudb" (you wrote this in 2.9; just link the procedure).

---

## Phase 2 exit criteria (all must be true)

- [ ] An HTTP request to core appears in the audit viewer within 1 s, with full request and response visible.
- [ ] `oc audit verify <yesterday>` returns OK.
- [ ] The attestations repo has at least one published daily attestation; `verify/verify.py` returns 0 against it.
- [ ] `audit-appender` survives a kill-restart with zero data loss and zero duplicates.
- [ ] `audit-projector` can be rebuilt from immudb with `oc audit projection rebuild` and the final row count matches.
- [ ] Tampering with an immudb entry (or simulating it via test) is detected by `oc audit verify`.
- [ ] Phase 2 smoke (`tests/phase-2-smoke.sh`) all green.

---

## Rollback / panic procedures

- **Bad migration in `openclaw.*` schema:** Alembic `downgrade -1`. The audit tables are projection only — losing them means re-running `oc audit projection rebuild` from immudb, not data loss.
- **Bad redaction policy that fails open (leaks a secret):** Pause the appender (`systemctl stop audit-appender`). Identify the affected ULID range from the projection. Decide per-entry: delete the entry from immudb (immudb supports deletion with a tombstone but it breaks WORM — only do this if the secret is truly compromised and you accept the chain-break) or rotate the secret and leave the entry. Document the choice in `docs/RUNBOOK.md`. Fix the rego, version-bump it, restart appender.
- **immudb storage corruption:** Restore from the most recent nightly export in MinIO + replay any missed messages from NATS (they're still in the stream within retention).
- **NATS storage corruption:** Worse — NATS is the durable buffer. Restore from snapshot; lose any in-flight messages that hadn't been ACKed. Acceptable because the appender ACKs only after immudb commit; everything in immudb is preserved.
- **Wipe-safe-ness:** Phase 2 is *no longer* wipe-safe past 2.13 — once attestations are published externally, wiping immudb breaks the chain visible to the public. From 2.13 onward, treat the system as production for purposes of destructive ops.

---

## What goes into git, what doesn't

| Goes in git | Stays out of git |
|---|---|
| `core/` source code | Vault tokens, secret_ids, AppRole secret_ids |
| `policy/redaction.rego` + tests | Encrypted PII blobs (live only in immudb) |
| Alembic migrations | Postgres connection strings (vault-agent renders at runtime) |
| `audit/viewer/` Next.js source | `node_modules/`, `.next/` build output |
| `audit/cli/` source + pinned lockfile | The pyinstaller-produced binary (build artifact) |
| `infra/nats/streams.yaml` declarative config | NATS user nkey seeds |
| `infra/compose/*.yml` (with secret refs, not values) | Rendered docker `secrets:` contents |
| `tests/phase-2-smoke.sh` | Any test fixture containing real OAuth tokens |
| This runbook | Actual immudb admin password (in Vault), GitHub App private key (in Vault transit) |

---

## What Phase 2 deliberately does *not* do

To avoid scope creep, Phase 2 stops short of:

- **MCP proxy** — Phase 4. The bus and audit pipeline are ready for it but not implemented.
- **A2A router** — Phase 4. Same reason.
- **Event ingesters** (inotify, git, claude hooks) — Phase 3.
- **Outbound notifications** — Phase 8.
- **HA / multi-node immudb or NATS** — future ADR.
- **OPA bundle distribution + hot reload** — Phase 4 needs this for `policy/mcp.rego`; for Phase 2's `redaction.rego` a restart is acceptable since it changes rarely and is version-pinned per envelope anyway.

---

## Change log

- **2026-05-23 (v1)** — Drafted after Phase 1 runbook. Will be revised once execution begins and real-world friction surfaces.
- **2026-05-23 (v1.1)** — Task 2.1 extended to create the `openclaw.lookup` table (per ADR-002 D12, the Postgres counterpart to Vault for low-value config). Seed values referenced by Phase 1 task 1.5 populated here.
- **2026-05-23 (v1.2)** — Mount paths in tasks 2.2 / 2.3 / 2.4 updated from `/var/lib/openclaw/<svc>` to `/mnt/openclaw/<svc>` (snap-Docker confinement; LUKS images remain at `/var/lib/openclaw/luks/`). Task 2.2 reframed: dm-crypt setup is now done by Phase 1 task 1.1's script which covers all 4 services at once.
- **2026-05-23 (v1.3)** — Tasks 2.1 / 2.3 / 2.4 backed by `infra/scripts/06-apply-postgres-schema.sh` / `07-immudb-bootstrap.sh` / `08-nats-bootstrap.sh`. Schema DDL at `infra/postgres/openclaw-schema.sql`; NATS streams declaratively in `infra/nats/streams.yaml`. All three idempotent. All passwords random + stored in Vault `kv/openclaw/{postgres,immudb}/*`.
- **2026-05-23 (v1.4)** — Tasks 2.1 / 2.3 / 2.4 executed end-to-end. Several non-trivial fixes shipped to the scripts along the way:
  - 06: support system Postgres (`sudo -u postgres psql`) in addition to docker — the operator's system pg 18 holds port 5432.
  - 07: expect-driven immuadmin invocations (no flag/env, distroless image). Pre-chown bind target. Permission name `read` not `readonly`. Removed all `sh -c` wrappers.
  - 08: switched from `docker exec oc-nats nats stream add` (no CLI in server image) to `docker run --rm --network=oc-internal natsio/nats-box nats stream add`.
  - New helper `upload-immudb-creds-to-vault.sh` for when 07 runs without Vault wiring (e.g., from a Bash tool with no AppRole creds).
  - 8 small fix commits in the PR document each gotcha for future bootstraps.
