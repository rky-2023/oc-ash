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

### 2.1 Provision the Postgres schema for openclaw

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
- Allocate a partition or LUKS-on-loop file. 50 GB is comfortable; immudb will compact older data.
- `cryptsetup luksFormat`, passphrase to your password manager.
- Mount at `/var/lib/openclaw/immudb/` with `noexec,nosuid,nodev`.
- `systemd` mount unit ordered before the immudb service.

**Verify:** `cryptsetup status immudb-data` active; `lsblk` shows the LUKS layer.

---

### 2.3 Install and run immudb

**Why:** The WORM audit ledger (ADR-003 D1).

**Steps:**
- Pull the official immudb container image. Pin to a specific digest (`@sha256:...`), not a tag, per ADR-001's supply-chain stance.
- Compose service:
  - User: dedicated `immudb` user (uid > 10000).
  - Mounts: `/var/lib/openclaw/immudb/` → `/var/lib/immudb`.
  - Network: internal docker network only; no port published to host.
  - Args: `--dir /var/lib/immudb --signingKey /run/secrets/immudb-signing.key` (immudb has native server-side state signing).
- Bootstrap: change the default admin password immediately, store new password in `kv/openclaw/immudb/admin`.
- Create database `openclaw_audit`.
- Create user `appender` with R/W on `openclaw_audit` only; password in `kv/openclaw/immudb/appender`.
- Create user `projector` with R-only on `openclaw_audit`; password in `kv/openclaw/immudb/projector`.

**Verify:** `immuclient` from inside the docker network can connect, write a test entry, read it back with a proof; same client from outside the docker network is refused.

---

### 2.4 Install and run NATS JetStream

**Why:** The bus (ADR-001 D3). Every `oc.*` message flows here first.

**Steps:**
- Pull the official `nats` container image, pinned by digest.
- Compose service:
  - User: dedicated `nats` user.
  - Storage: `/var/lib/openclaw/nats/` (separate dm-crypt or share with immudb's volume — your call; separate is cleaner).
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

### 2.5 Scaffold openclaw-core (the Python service)

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

### 2.6 Build the audit envelope model + signer

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

### 2.8 Build the audit-appender service

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

### 2.9 Build the audit-projector service

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

### 2.10 Build the audit viewer (Next.js, read-only)

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

### 2.11 Build the `oc` CLI (audit subcommands)

**Why:** Operator-friendly verification + replay (ADR-003 D8 + D10). Lives in `audit/cli/` so it can be installed independently.

**Steps:**
- New Python package `oc-cli` (Click or Typer based). Single binary via `pyinstaller` or `pex`.
- Subcommands:
  - `oc audit tail [--subject <pattern>]` — live tail from NATS (read-only NATS user).
  - `oc audit replay <conv_id> [--narrative | --structured]` — reconstruct A2A conversation from immudb. Outputs to stdout.
  - `oc audit verify <date>` — recompute Merkle root for the day, compare to local immudb, compare to published attestation (if attestations repo cloned locally). Returns 0 if all match, non-zero with details otherwise.
  - `oc audit unwedge --confirm` — clears paranoid mode. Requires fresh WebAuthn assertion.
  - `oc audit projection rebuild` — drops + replays the Postgres projection from immudb.
- All subcommands require a valid local Vault token (cli runs as the operator, not as a service); the CLI itself does not hold long-lived credentials.

**Verify:**
- `oc audit tail` shows messages as they're appended.
- `oc audit verify <yesterday>` is OK after at least 24 hours of operation.
- `oc audit replay` reproduces the same content visible in the viewer's narrative tab.

---

### 2.12 Create the `openclaw-attestations` GitHub repo and the App credential

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

### 2.13 Build the attestation-publisher job

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
