# openclaw — Phase-wise Build Plan

> **Status:** Draft v1 — 2026-05-23
> **Owner:** rky
> **Working directory:** `/home/asher/openclaw/`
> **Related:** Ashboard unification plan at `/home/asher/new-docs/PLAN.md`

---

## Honest framing up front

No internet-connected commodity server is unhackable by a determined nation-state. Anyone claiming otherwise is selling something. What this plan realistically achieves is **"raise compromise cost above well-funded criminal groups and most non-targeted nation-state activity"** — a posture most enterprises don't reach. The plan aims for that bar honestly rather than promising magic.

---

## Mission

A self-hosted personal agent fabric on `/home/asher` that:

1. Bidirectionally syncs Google Calendar + Gmail.
2. Pushes notifications to an Android app for repo / codebase activity.
3. Routes work through MCP servers and an A2A controller with fully-auditable context exchange.
4. Auto-opens GitHub PRs for any codebase changes it makes.
5. Is hardened to a "tier-1 commercial" threat model.

---

## Locked decisions (from initial scoping)

| Question | Decision |
|---|---|
| Mobile platform | Android-first, React Native, FCM push |
| Auth/access model | Single user (rky), hardware-key gated (WebAuthn + YubiKey FIDO2) |
| Integration with Ashboard | Separate service, **shared infra** (reuses Postgres / Redis / Mosquitto / Grafana / Prometheus) |
| Repo-action monitoring scope | All four sources: inotify on `/home/asher/*`, local git hooks, GitHub webhooks, Claude Code session hooks |

---

## Non-negotiable principles

1. **Default deny.** Every component starts with zero permission; capabilities are added per-action, signed, short-lived.
2. **Everything signed, everything logged.** WORM (write-once-read-many) hash-chained audit log of every MCP / A2A message and every filesystem mutation, append-only, externally co-signed.
3. **Single-user, hardware-key-gated.** No password fallbacks; YubiKey + WebAuthn + device attestation.
4. **Modular bus, dumb edges.** A central event bus (NATS JetStream) with thin adapters; swap any adapter without touching core.
5. **Session-scoped Claude.** Every Claude Code session lives under one project dir; a manifest tracks its mutations; nothing crosses.

---

## Architecture (one-glance)

```
                       ┌─────────── Android RN app (FCM) ───────────┐
                       │                                              │
                  [WireGuard tunnel ─ split-tunnel ─ no public ingress except webhook stub]
                       │
┌──────────────────────┴──────────────────────────────────────────────────────┐
│  openclaw-edge   (Caddy w/ mTLS, WAF, ratelimit, WebAuthn challenge mux)    │
└──────────────────────┬──────────────────────────────────────────────────────┘
                       │
┌──────────────────────┴──────────────────────────────────────────────────────┐
│  openclaw-core (FastAPI, async) ── policy gate (OPA) ── audit appender      │
│         │                                                                    │
│         ├── NATS JetStream (event bus, persistent, AES at rest)             │
│         │                                                                    │
│         ├── Agent Orchestrator (A2A controller)                             │
│         │     │                                                              │
│         │     ├─→ MCP: google-calendar     (sandboxed via gVisor)           │
│         │     ├─→ MCP: gmail               (sandboxed)                       │
│         │     ├─→ MCP: github              (sandboxed)                       │
│         │     ├─→ MCP: fs-asher            (sandboxed, read-mostly)         │
│         │     └─→ MCP: notifier-fcm        (sandboxed)                       │
│         │                                                                    │
│         ├── Event Ingestion                                                 │
│         │     ├─ inotify watcher → /home/asher/**                           │
│         │     ├─ git post-commit/post-receive hooks                         │
│         │     ├─ GitHub webhook receiver (HMAC-validated)                   │
│         │     └─ Claude Code hooks (PreToolUse/PostToolUse/Stop/SubagentStop)│
│         │                                                                    │
│         └── PR Automator (GitHub App, fine-scoped, key in Vault)            │
│                                                                              │
│  Storage: Postgres (existing) · Redis (existing) · MinIO (encrypted blobs)  │
│  Secrets: Vault (new, autounseal via Tang+Clevis)                           │
│  Audit:   immudb (cryptographic ledger) + nightly external co-sign          │
│  Obs:     Grafana/Prometheus (existing) + Loki + Falco + Wazuh              │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Reused from Ashboard:** Postgres, Redis, Mosquitto, Grafana, Prometheus.
**New components:** NATS JetStream, Vault, immudb, MinIO, Loki, Falco, Wazuh, Tang/Clevis, gVisor, Caddy edge.

---

## Phase 0 — Threat model, repo layout, decisions of record (1–2 days)

**Goal.** Write down what we're defending against and how the codebase is organized *before* writing code.

**Deliverables.**

- `/home/asher/openclaw/THREAT_MODEL.md` — STRIDE table over each component; named adversary tiers:
  - T1: unauthenticated internet rando
  - T2: phished session token
  - T3: lost YubiKey
  - T4: root on the box
  - T5: targeted advanced
- `/home/asher/openclaw/ADR/` — Architecture Decision Records (one file per choice: FastAPI vs Django, NATS vs RabbitMQ, immudb vs S3-WORM, etc.). ADRs are the durable record; PRs reference them.
- Repo skeleton:

  ```
  /home/asher/openclaw/
    core/            (FastAPI service)
    mcp/             (one subdir per MCP server)
    agents/          (A2A controllers, planners)
    edge/            (Caddy config, WireGuard config)
    ingest/          (inotify, git-hooks, gh-webhook, claude-hooks)
    notifier/        (FCM bridge)
    mobile/          (React Native app)
    infra/           (docker-compose.openclaw.yml, terraform/, ansible/)
    audit/           (immudb client, log readers, replay tool)
    policy/          (OPA rego bundles)
    docs/            (THREAT_MODEL.md, ADR/, RUNBOOK.md)
    .github/         (workflows, CODEOWNERS, branch-protection.yaml)
  ```

- Branch protection: `main` requires signed commits, 1 review (from PR bot's reviewer alias = you on a different device), passing CI, no force-push.

**Exit criteria.** ADR-001 (architecture), ADR-002 (auth), ADR-003 (audit) merged.

---

## Phase 1 — Secrets foundation + identity (3–4 days)

> **Implementation runbook:** [`docs/phases/phase-1.md`](./docs/phases/phase-1.md) — 15-step task list, exit criteria, rollback procedures.

**Goal.** No secret ever sits in an env file or git. Identity is *only* hardware-backed.

**Components.**

- **Vault** (single-node, **manual Shamir-only unseal** — no Tang. 3-of-5 shares held on durable, geographically separated media: 1× metal seed plate (Cryptosteel/Trezor Steel) + 2× paper in separate locations + 2× discretionary (encrypted USB acceptable as secondary). Reboot is hands-on by design — see ADR-001 R1 for rationale.).
- **WebAuthn + YubiKey FIDO2** for login. Two YubiKeys enrolled (primary + spare in safe). No TOTP, no SMS, no email recovery.
- **mTLS** between every internal service. Internal CA in Vault; certs auto-renew every 24h via `vault-agent`.
- **Short-lived service tokens.** Each MCP server gets a 15-minute JWT minted by core, signed by Vault transit. No long-lived API keys baked in containers.
- **Per-device attestation.** Android app does Play Integrity attestation on every push registration; server refuses tokens from rooted/emulated devices.

**Concrete tasks.**

1. Install Vault; initialize with 3-of-5 Shamir; distribute shares to durable media (metal plate + paper + optional encrypted USB) across ≥3 sites; rehearse a cold unseal from those shares.
2. Bootstrap internal CA, issue first server cert.
3. WebAuthn registration flow (FastAPI + `webauthn` lib); enroll both YubiKeys.
4. Login flow: passkey → 5-minute access JWT + 24-hour refresh, refresh requires YubiKey touch.
5. Migrate any existing dev secrets into Vault KV-v2 paths.

**Exit criteria.** A `curl` to `core` without a valid mTLS cert AND a YubiKey-signed JWT returns 401. You can log into the (stub) web UI with only the YubiKey.

---

## Phase 2 — openclaw-core skeleton + event bus + audit ledger (4–5 days)

> **Implementation runbook:** [`docs/phases/phase-2.md`](./docs/phases/phase-2.md) — 15-step task list, exit criteria, rollback procedures.

**Goal.** The spine. Nothing useful yet, but every message flows through a logged, signed pipeline.

**Components.**

- **openclaw-core** (FastAPI, Python 3.13, async). Pydantic v2 models. SQLModel over the existing Postgres (new schema `openclaw`).
- **NATS JetStream** as the event bus. Subjects:
  - `oc.event.fs.>` — filesystem events
  - `oc.event.git.>` — git events
  - `oc.event.gh.>` — GitHub webhook events
  - `oc.event.claude.>` — Claude hook events
  - `oc.event.mail.>`, `oc.event.calendar.>` — Google sources
  - `oc.a2a.>` — agent-to-agent messages
  - `oc.mcp.>` — MCP request/response pairs
  - `oc.notify.>` — outbound notifications
- **immudb** as the WORM audit ledger. Every message on `oc.*` is mirrored into immudb with a Merkle proof. Nightly job exports the day's root hash and signs it with an offline key (or commits it to a public Git repo to make tampering observable).
- **Audit reader UI** — a thin Next.js page (lives in `audit/viewer/`) that renders, for any time range or any A2A conversation, a readable transcript:

  ```
  ┌─ 2026-05-23 14:02:11.341  conv:abc123  agent: planner → mcp:gcal
  │  method: tools/call  tool: create_event
  │  args: { title: "Dentist", start: 2026-06-04T10:00, ... }
  │  policy: allowed by rule "calendar.write.self"
  │
  ├─ 2026-05-23 14:02:11.812  mcp:gcal → agent: planner
  │  result: ok  event_id: "9k2j..."  remote_etag: "W/\"..."\
  │  side_effects: gcal_api_call(POST /events) 187ms
  └────
  ```

  Two views: structured (JSON tree) and narrative (human-readable transcript above).

**Concrete tasks.**

1. `infra/docker-compose.openclaw.yml` — NATS, immudb, MinIO, core, edge. Bring up cleanly.
2. `core/app/audit.py` — middleware that envelopes every inbound/outbound message, ID-stamps it (ULID), HMAC-signs with a service key, ships to NATS subject + immudb.
3. `audit/viewer/` — Next.js app reading from a Postgres projection of immudb (immudb is the source of truth; Postgres is a denormalized read model for fast UI).
4. CLI: `oc audit tail`, `oc audit replay <conversation_id>`, `oc audit verify <date>` (re-checks Merkle proof).

**Exit criteria.** Any HTTP request to core appears in the viewer within 1s with full request/response, and `oc audit verify` returns OK for yesterday's ledger root.

---

## Phase 3 — Event ingestion (all four sources) (4–5 days)

> **Implementation runbook:** [`docs/phases/phase-3.md`](./docs/phases/phase-3.md) — 7-step task list, exit criteria, rollback procedures.
> **Related ADR:** [`docs/ADR/ADR-004-public-ingress.md`](./docs/ADR/ADR-004-public-ingress.md) — defer-and-poll for GitHub.

**Goal.** Every interesting thing that happens under `/home/asher/` lands on the bus.

**Components.**

- **inotify watcher** (`ingest/fswatch/`) — Rust binary using `notify` crate. Watches `/home/asher/` recursively with an allowlist (skip `.git/objects`, `node_modules`, `__pycache__`, `*.pyc`, etc.). Coalesces bursts (1s debounce per path). Emits `oc.event.fs.<repo>.<kind>`.
- **git hooks** — installed into every repo under `/home/asher/*` via a shared `core.hooksPath` pointing at `ingest/githooks/`. Hooks: `post-commit`, `post-merge`, `post-checkout`, `post-rewrite`, `pre-push`. Each posts a signed payload to core over Unix socket.
- **GitHub events** — **deferred until a public-ingress ADR lands** (see ADR-001 R4). Interim path: **polling** via the `openclaw-bot` GitHub App with a 30–60 s cadence; same `oc.event.gh.>` subjects on the bus, so downstream consumers don't care. The `ingest/gh-webhook/` design (Caddy + HMAC + IP allowlist + Vault-rotated secret) is preserved on paper for when public ingress is wired up.
- **Claude Code hooks** — settings.json hooks (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `Notification`) all `curl` a Unix socket on core. Payload includes `session_id`, `cwd`, tool name, args/results.

**Concrete tasks.**

1. Write fswatch (Rust, ~300 LOC). Systemd unit, runs as a dedicated `oc-fswatch` user with read-only access to `/home/asher/`.
2. Bootstrap script that walks `/home/asher/*` and installs hooksPath in every `.git/config`.
3. Caddy site for `gh.openclaw.<your-domain>` — TLS via internal ACME, HMAC validator middleware.
4. `~/.claude/settings.json` (or project-scoped `.claude/settings.json`) updated with hooks. Use the `update-config` skill for this step.
5. Filters/policies in `policy/ingest.rego` so we don't drown in noise (e.g., suppress events from MinIO data dirs).

**Exit criteria.** `touch /home/asher/foo.txt`, a commit in `Ashboard/`, a PR opened on GitHub, and a Claude tool call all appear in the audit viewer correctly classified.

---

## Phase 4 — MCP & A2A logging substrate + policy gate (3–4 days)

**Goal.** Lock down how agents talk to each other and to MCP servers. Every byte is logged and policy-checked.

**Design.**

- **A2A protocol** — adopt Google's emerging A2A spec shape: JSON-RPC over HTTPS with `task` / `artifact` / `message` primitives. Each agent has a public agent-card describing capabilities.
- **MCP wrapping** — every MCP server runs inside its own gVisor sandbox (`runsc`), bound to a Unix socket. Core proxies MCP traffic and is the only thing that talks to the actual MCP process. The proxy:
  1. Authenticates the calling agent (mTLS client cert).
  2. Asks OPA `policy/mcp.rego` whether this call is allowed (tool name, arg shape, time-of-day, rate).
  3. Logs request to immudb.
  4. Forwards to MCP.
  5. Logs response, redacts secrets matching `policy/redaction.rego`.
  6. Returns to caller.
- **Context budgeting.** Every A2A conversation has a `context_budget` (tokens + wall-clock + tool-call count). Exceeding it triggers an audit event and (configurably) halts the conversation. This is the safety net against runaway agent loops.

**Concrete tasks.**

1. `core/app/mcp_proxy.py` — the proxy with the 6-step pipeline above.
2. `policy/mcp.rego` — starter policies; default deny.
3. `agents/a2a_router.py` — task dispatcher; persists conversations as `conversation_id → ordered[messages]` in Postgres (also mirrored to immudb).
4. Extend the audit viewer with a "conversations" tab — pick a conversation, see the full ladder diagram.

**Exit criteria.** A toy planner agent can invoke a toy MCP "echo" server only via the proxy; direct connections refused; the entire round trip is reconstructable in the viewer.

---

## Phase 5 — Google Calendar MCP server + bidirectional sync (3–4 days)

**Goal.** Calendar in sync; agents can read/write events under policy.

**Approach.**

- **OAuth** — Google Cloud Console project, OAuth client with calendar scope, refresh token stored only in Vault. Consent re-prompts annually.
- **MCP server `mcp/google-calendar/`** (Python). Tools: `list_events`, `get_event`, `create_event`, `update_event`, `delete_event`, `watch_calendar`.
- **Sync engine** — uses Google's incremental sync (`syncToken`) + push notifications (Google's `watch` webhook hitting our edge). Local mirror in Postgres `openclaw.calendar_events`. Two-way diff with conflict resolution = "remote wins, log local-overrides".
- **Push channel renewal** cron every 6 days (Google watches expire at 7).

**Notifications.** Calendar event changes emit `oc.event.calendar.created/updated/deleted` → routed to notifier.

**Exit criteria.** Create event in Google web → appears in local Postgres within 10s and pushes a notification. Create via MCP `create_event` → appears in Google.

---

## Phase 6 — Gmail MCP server + notification pipeline (3–4 days)

**Goal.** Gmail visible to agents under tight scope; new mail triggers notifications.

**Approach.**

- **Scope minimization** — start with `gmail.readonly` + `gmail.send` only (no modify/delete). Adding broader scope is an ADR.
- **MCP server `mcp/gmail/`** — tools: `list_threads`, `get_message`, `search`, `send_message`. No delete/modify in v1.
- **Watch** — Gmail push via Pub/Sub. We run a small Pub/Sub pull worker (no public HTTPS endpoint needed — pull, not push, for Gmail to avoid extra attack surface).
- **PII redaction** in audit log — by default, mail bodies are stored encrypted-at-rest with a per-message key in Vault; the audit viewer shows only headers + a "reveal" button that requires a fresh YubiKey touch.

**Exit criteria.** New mail in inbox → notification on Android in <30s. Audit viewer shows the MCP call but body is gated.

---

## Phase 7 — GitHub PR automator (3 days)

**Goal.** Any codebase mutation an agent makes lands as a reviewable PR, never a direct push.

**Design.**

- **GitHub App** (not PAT) named "openclaw-bot". Permissions: `contents:write`, `pull-requests:write`, `checks:write` — *only on repos you install it on*. Private key stored in Vault.
- **Workflow per change** — agent calls `mcp/github/propose_change(repo, branch_name, files_changed[], pr_title, pr_body)`. The MCP:
  1. Clones repo into `/tmp/oc-work/<conv_id>/` (ephemeral, tmpfs).
  2. Applies the diff.
  3. Runs the repo's own pre-commit hooks + a global `oc-precheck` (secret scan via gitleaks, SAST via semgrep).
  4. If clean, opens PR with a structured body that links to the audit conversation.
  5. Requests review from your personal account.
  6. Adds the audit `conversation_id` as a PR label.
- **CI gate** — a GitHub Action verifies the PR's body conversation_id exists in our immudb (via a webhook back to openclaw-edge). PRs without a valid audit anchor fail CI. This means even if someone steals the bot key, they can't open audit-less PRs.
- **No agent ever has main-branch push.** Branch protection enforces.

**Exit criteria.** Agent edits `Ashboard/foo.ts` → PR appears on github.com with conversation link → clicking it opens the audit viewer.

---

## Phase 8 — Notification fan-out + FCM bridge (2–3 days)

**Goal.** All notification-worthy events land on the Android app.

**Design.**

- **Notification rules engine** — `policy/notify.rego`. Inputs: event type, repo, severity, time-of-day, user "do-not-disturb" schedule. Outputs: `{deliver: bool, channel: "android|email|none", priority: 0–3}`.
- **FCM bridge** (`notifier/fcm/`) — Python service subscribing to `oc.notify.android.>`, building an FCM message (data-only payload — app composes the UI to keep payloads off Google servers as much as possible), signing with FCM server key from Vault.
- **End-to-end encryption** — payload body is encrypted with the app's public key (Curve25519) registered at device-pairing; FCM sees only `{cipher: "...", nonce: "..."}`. App decrypts locally.

**Exit criteria.** A push lands on the (still-stub) test device within 3s of source event, body unreadable to FCM.

---

## Phase 9 — Android app (React Native) (5–7 days)

**Goal.** Functional notification client with WebAuthn pairing.

**Stack.**

- React Native (latest stable), TypeScript, Expo bare or vanilla CLI.
- `react-native-firebase` for FCM.
- `react-native-keychain` for storing the private decryption key (Android Keystore hardware-backed).
- Pairing flow: open app → scan QR shown by openclaw web UI (you've logged in via YubiKey on a laptop) → app does Play Integrity attestation → server enrolls device → exchanges Curve25519 public keys.
- **Screens (v1):** Inbox (notifications), Detail, Settings (DND, channel toggles), Pair-new-device.

**Concrete tasks.**

1. RN scaffold + CI build (GitHub Actions).
2. Pairing flow end-to-end.
3. Foreground + background notification handling, deep links into Detail.
4. Local SQLite store for offline history.
5. Sign release APK with key in Vault (CI pulls at build time).

**Exit criteria.** APK installs, pairs in <60s, receives a push, decrypts, displays. Reboot survives; key never leaves Keystore.

---

## Phase 10 — Hardening to "raise the cost" (4–5 days)

**Goal.** Defense-in-depth pass. This is where we earn the "expensive to compromise" claim.

**Checklist.**

- **Network:** WireGuard mesh between you and the server; nothing public except `gh.openclaw` (GitHub webhook) and the FCM relay path. Both behind Caddy with rate limits + Crowdsec/fail2ban.
- **Filesystem:** LUKS full-disk; `/home/asher/openclaw/secrets/` on a separate dm-crypt volume that auto-locks after 5 min idle.
- **Containers:** Every MCP runs under gVisor (`runsc`) with seccomp profile + read-only root + no-new-privileges + dropped capabilities.
- **Supply chain:** All deps pinned with hashes (`pip-tools`, `npm --package-lock-only`). Renovate bot opens dep PRs which require manual review. SBOM generated per build (`syft`), signed with cosign (`sigstore`). CI rejects unsigned base images.
- **Runtime IDS:** Falco rules tuned for the box; alerts on unexpected exec, file writes outside expected dirs, outbound connections to non-allowlisted IPs.
- **Log integrity:** Loki + immudb. Daily Merkle root committed to a **private** Git repo (`rky-2023/openclaw-attestations`) — tampering becomes visible to the operator + invited read-only witnesses (see ADR-001 R2 for the visibility trade-off).
- **Backups:** Restic to a B2 bucket, encrypted with age key whose private half is on a YubiKey in safe. Monthly restore test.
- **Recovery:** Runbook for "I lost my primary YubiKey" — uses spare from safe. Runbook for "server is compromised" — wipe, restore from yesterday's backup, rotate every secret.
- **Disclosure honesty:** A `SECURITY.md` saying explicitly what threat tiers we resist and what we do *not* claim to resist.

**Exit criteria.** Internal pentest (you, with help from `code-review` + `security-review` skills + an actual security-focused agent run) finds no P0/P1.

---

## Phase 11 — Detection, response, observability (3 days)

**Goal.** See and react to what's happening.

**Components.**

- Grafana dashboards: events/min, MCP latency, policy denials, audit chain health, FCM delivery rate.
- Loki for log search across all services.
- Falco + Wazuh agent shipping to a SIEM-lite Grafana board.
- Weekly "audit replay" — a scheduled job (via the `schedule` skill) that picks a random hour from the past week, runs `oc audit verify`, and emails you a green/red.
- Anomaly alerts: spike in tool calls per conversation, off-hours activity, secret-fetch from Vault outside expected windows.

**Exit criteria.** Simulating an attack (e.g., `cat /etc/shadow` from inside a sandboxed MCP container) triggers a Falco alert into the Android app within 60s.

---

## Phase 12 — Session scoping + long-term ops (2–3 days)

**Goal.** Claude sessions are properly scoped to directories; everything is documented.

**Design.**

- Every Claude Code project dir gets a `.claude/session-manifest.json` automatically created by a `SessionStart` hook. It records: session id, cwd, start time, allowed tool list, allowed paths.
- A `Stop` / `SubagentStop` hook closes the manifest and writes a session summary (files touched, PRs opened, agents spawned) into `/home/asher/openclaw/sessions/<date>/<session_id>.md`.
- The audit viewer gets a "Sessions" tab that lists each session as a foldable card.
- `RUNBOOK.md` covers every break-glass operation: rotate YubiKey, rotate Vault root, recover from immudb corruption, regenerate FCM server key, rotate GitHub App key.

**Exit criteria.** You can hand someone (future-you) the runbook and they can operate the system cold.

---

## Cross-cutting: extensibility hooks

So the system is genuinely open to future change:

- **New MCP server = drop a directory under `mcp/`** + add a service to `docker-compose.openclaw.yml` + register an agent card + add a `policy/mcp/<name>.rego`. Core picks it up.
- **New event source = drop an ingester under `ingest/`** that publishes to `oc.event.<source>.>`. Core needs zero changes.
- **New notification channel** (email, Telegram, Discord) = subscribe to `oc.notify.<channel>.>`. The rules engine already routes by channel.
- **Plugin agents** = each agent is an HTTP service that registers its capability card. The orchestrator discovers by polling a known registry path.

---

## Out-of-band actions (can't be automated)

1. Buy 2× YubiKey 5C NFC + paper safe storage for shamir shares.
2. Create Google Cloud project + OAuth client (Calendar + Gmail scopes).
3. Create GitHub App "openclaw-bot" + install on relevant repos.
4. Domain name + public DNS — **deferred** until the public-ingress ADR lands. Internal addressing uses Tailscale MagicDNS in the interim.
5. Tailscale account + ACL set up; enroll the server, rky's laptop, and the Android device on the tailnet. (Cloudflare Tunnel deferred to a later ADR.)

---

## Rough total

**~40–55 working days** for a single person end-to-end, if the foundation phases (0–4) are done with care. Phases 5–8 can parallelize once 4 is solid. Phases 10–11 are continuous after launch, not one-shots.

---

## Recommended start order

Phase 0 → Phase 1, no skipping. Most security postures fail because identity is bolted on at the end; we bolt it on at the beginning.

---

## Open questions / next decisions

Resolved on 2026-05-23 (see ADR-001 R1–R4):

- ~~DNS / domain choice~~ → public ingress **deferred**; Tailscale + MagicDNS in the interim.
- ~~Tang server hardware~~ → **no Tang**; manual Shamir-only unseal across metal plate + paper + optional encrypted USB across ≥3 sites.
- ~~Attestation log host~~ → separate public repo **`rky-2023/openclaw-attestations`**, pushed by `openclaw-bot` with a credential scope limited to that repo.

Still open:

- Android app distribution: sideload-only vs. private Play track.
- Future ADR on public ingress (Cloudflare Tunnel vs. alternative) — required before Phase 3's GitHub-webhook receiver can be built.
- ADR-002 (auth) and ADR-003 (audit) — not yet drafted.

---

## Change log

- **2026-05-23 (v1)** — Draft authored.
- **2026-05-23 (v1.1)** — Four open questions resolved via ADR-001 R1–R4: Shamir-only Vault unseal (Phase 1 Vault description + concrete tasks); separate-repo attestation log (`rky-2023/openclaw-attestations`); MIT license confirmed; Tailscale-now-Cloudflare-later, with Phase 3 GitHub-webhook ingestion deferred and polling in the interim. Out-of-band actions and open-questions sections rewritten to match.
- **2026-05-23 (v1.2)** — Phase 1 implementation runbook linked at `docs/phases/phase-1.md`. Pattern: each phase will get its own runbook under `docs/phases/`.
- **2026-05-23 (v1.3)** — Phase 2 implementation runbook linked at `docs/phases/phase-2.md`.
- **2026-05-23 (v1.4)** — Phase 10 attestations note updated: private repo (per ADR-001 R2 amendment); ADR-002 D12 added (secret-vs-config split: Vault for high-value, Postgres `openclaw.lookup` for low-value config).
- **2026-05-23 (v1.5)** — Phase 3 implementation runbook linked at `docs/phases/phase-3.md`. ADR-004 (defer-and-poll public-ingress) referenced in Phase 3. ADR-002 D13 added (interim platform-authenticator auth mode pending YubiKey procurement).
- **2026-05-23 (v1.6)** — **Phase 1 + Phase 2 data plane online.** Phase 1 tasks 1.1–1.13 + Tailscale-HTTPS for `core` executed end-to-end (Vault unsealed with Shamir-3-of-5, internal CA + transit keys live, audit log on, AppRole-only admin, mTLS-listener cert issued, WebAuthn RP scaffold up). Phase 2 tasks 2.1 / 2.3 / 2.4 executed: `openclaw` Postgres schema + lookup table + `openclaw_app` role; immudb up with `openclaw_audit` DB + appender (R/W) + projector (R) users; NATS up with all 5 streams. All bootstrap secrets in Vault under `kv/openclaw/{postgres,immudb}/*`. Bootstrap-friction log captured in `docs/phases/phase-2.md` v1.4 changelog. Next: task 2.5 (core wiring to NATS + immudb).
