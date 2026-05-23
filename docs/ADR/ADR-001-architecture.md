# ADR-001: Overall architecture — separate service, shared infra, async core, event-bus spine

- **Status:** Accepted
- **Date:** 2026-05-23
- **Deciders:** rky
- **Supersedes:** —
- **Superseded by:** —
- **Related:** PLAN.md, docs/THREAT_MODEL.md, ADR-002 (auth), ADR-003 (audit)

---

## Context

openclaw must do five things at once:

1. Bidirectionally sync Google Calendar and Gmail.
2. Push notifications to an Android app.
3. Watch four event sources (inotify, git hooks, GitHub webhooks, Claude Code hooks) on `/home/asher`.
4. Route work through MCP servers and an agent-to-agent (A2A) orchestrator, with every byte of context exchange logged and policy-checked.
5. Open GitHub PRs for any agent-authored codebase change.

Existing context on the box:

- An **Ashboard** stack (Django + Next.js + Postgres + Redis + Mosquitto) already lives in `/home/asher`. It owns its own domain (personal management). It is *not* an agent fabric.
- Grafana + Prometheus are already running for monitoring.
- The host is a single Linux server. There is no Kubernetes; everything is `docker compose`-driven today.

The decision is how to shape openclaw *given* this environment, with the constraints from initial scoping:

- Single user, hardware-key gated (WebAuthn + YubiKey FIDO2).
- Separate service, **shared infra** (reuse Postgres/Redis/Mosquitto/Grafana/Prometheus).
- All four event sources required.
- "Default deny, everything logged, everything signed" is non-negotiable.
- Architecture must be open to future change (new MCPs, new event sources, new channels).

---

## Decision

Adopt the following architecture (visualized in PLAN.md):

### D1. Codebase / runtime — separate repo, separate service, shared infra.

openclaw lives at `/home/asher/openclaw/` as its own repo with its own docker-compose file. It runs as a distinct set of containers from the Ashboard stack. It connects to the **already-running** Postgres (new schema `openclaw`), Redis (separate DB index), Mosquitto (separate topic prefix), Grafana, and Prometheus.

Rationale: a Django app inside `ashboard-backend` would couple two unrelated product domains (personal management vs. agent fabric) and force every release to ship both. A fully standalone stack with its own Postgres / Redis would duplicate ops for no isolation benefit (they share a host kernel anyway). Sharing infra at the *data plane* and isolating at the *application plane* is the right granularity.

### D2. Application framework — FastAPI (Python 3.13, async).

Rationale: async-first matters for openclaw because the workload is dominated by waiting on external I/O (Google APIs, GitHub, NATS, immudb, MCP subprocesses). Django's sync model would force a thread-per-request pattern that doesn't fit. FastAPI also has first-class Pydantic v2 typing, which we will use for every message envelope on the bus.

We accept the tradeoff that we don't get Django's admin and ORM ergonomics — we don't need them; SQLModel + alembic-equivalent is enough for the few tables we'll own.

### D3. Spine — NATS JetStream as the event bus.

Every message worth remembering crosses `oc.*` subjects on NATS JetStream:

- `oc.event.fs.>`, `oc.event.git.>`, `oc.event.gh.>`, `oc.event.claude.>` — ingest
- `oc.event.mail.>`, `oc.event.calendar.>` — Google sources
- `oc.a2a.>` — agent-to-agent messages
- `oc.mcp.>` — MCP request/response pairs
- `oc.notify.>` — outbound notifications

Rationale: we evaluated Redis Streams (already on the box), RabbitMQ, Kafka, and NATS JetStream. NATS JetStream wins on: (a) per-subject persistence with at-least-once delivery, (b) tiny ops footprint, (c) native subject-based routing matching our event taxonomy, (d) AES-at-rest in storage. Kafka is operationally heavy for one box. RabbitMQ's exchange/queue model is more ceremony than we need. Redis Streams is fine for ephemeral but weak on durability + replay.

This decision is the linchpin of D5 (extensibility): the bus is the contract; everything plugs into subjects.

### D4. Audit ledger — immudb as the WORM source of truth.

Every `oc.*` message is also appended to immudb with a Merkle proof. A nightly job exports the day's root hash, signs it offline, and commits it to a public `openclaw-attestations` Git repo. Tampering with the ledger becomes globally visible.

A denormalized Postgres projection feeds the audit viewer for fast reads, but immudb is the source of truth.

Rationale: a regular Postgres table with `INSERT-only` permission is not WORM — DBAs and DB-level RCEs can rewrite it. immudb provides cryptographic verification per entry. The external Merkle-root publishing closes the "what if the ledger itself is replaced" gap.

### D5. Agent boundaries — MCP servers in gVisor sandboxes, fronted by a central proxy.

Agents never speak to MCP servers directly. They speak to `openclaw-core`'s MCP proxy, which:

1. Authenticates the caller (mTLS client cert + short-lived JWT).
2. Asks OPA whether this tool/args combination is allowed.
3. Appends the request to immudb.
4. Forwards to the MCP server over a Unix socket inside a gVisor sandbox.
5. Appends the response (with `policy/redaction.rego` applied) to immudb.
6. Returns to the caller.

Rationale: this is the only place in the system where untrusted-ish code talks to trusted upstream APIs (Google, GitHub). Centralizing the choke point means there is exactly one piece of code to audit for that interaction, and exactly one place where context-exchange logging is enforced.

### D6. Identity — WebAuthn + YubiKey FIDO2, no password fallback.

User-facing auth is hardware-only. Two YubiKeys enrolled; either can revoke the other. Service-to-service auth is mTLS + Vault-minted JWTs (≤15 min). Details in ADR-002.

### D7. Notification fan-out — pure subscribers on `oc.notify.<channel>.>`.

Each notification channel (Android FCM first; email/Telegram/Discord later) is a separate process that subscribes to its subject and converts to the channel-specific format. Adding a channel requires zero core changes.

Payloads are E2E-encrypted with the device's Curve25519 public key registered at pairing. Third-party push providers see ciphertext only.

### D8. Code-change automation — GitHub App, never PAT, audit-anchored PRs.

Any agent-authored change goes through a GitHub App (`openclaw-bot`) whose private key lives in Vault. The agent never has direct push to `main`. Every PR body carries a `conversation_id` resolvable to an immudb entry; a required CI job verifies the anchor.

---

## Consequences

### Positive

- Clean failure-domain split: Ashboard issues do not page openclaw, and vice versa, despite shared storage.
- The event bus contract is the public API of the system. New MCPs / channels / event sources plug in without touching core.
- Every action — agent decision, tool call, filesystem write, PR opened — is traceable end-to-end in one immutable ledger.
- gVisor sandboxes mean a compromised MCP can't pivot to the host or to other MCPs.
- FastAPI async fits the IO-bound workload and keeps p95 latency low even on a single core.

### Negative

- Two stacks to operate on one host: Ashboard + openclaw. Doubles the runbook surface even if the infra is shared.
- NATS JetStream is one more piece of mission-critical infra. If it goes down, the whole agent fabric stops (intentionally — silent failure is worse).
- immudb is less commodity than Postgres; smaller community, fewer pre-built integrations. We invest in tooling to compensate.
- Python is not the fastest language; async helps but if we ever hit a CPU-bound workload (e.g., heavy embedding work) we will need to push it out of process.

### Neutral

- This architecture is consciously over-engineered for a single user. The investment buys two things: (a) honest defense posture per `THREAT_MODEL.md`, (b) cheap addition of future capabilities.

---

## Alternatives considered

### A1. Add openclaw as a Django app inside `ashboard-backend`.

Cheapest to start. Rejected because:
- Couples two unrelated domains into one release cycle.
- Django's sync model is wrong for IO-bound agent work.
- Hardening Django's request/response pipeline to "log + sign every message" is fighting the framework; FastAPI middleware is the natural place for it.

### A2. Fully standalone (separate VM, separate Postgres, separate everything).

Maximum isolation. Rejected because:
- Two Postgres instances on the same host is duplicate ops with no real isolation against a host compromise.
- We already have Grafana/Prometheus/Mosquitto for ops; rebuilding them gains nothing.
- Cost (the operator's time) outweighs benefit at single-user scale.

### A3. Kafka instead of NATS JetStream.

Rejected: operational weight (Zookeeper-or-KRaft, JVM tuning, brokers, schema registry) wildly overshoots a single-box deployment. Kafka makes sense at >10 producers/consumers; we'll have a dozen subjects.

### A4. Redis Streams instead of NATS JetStream.

Tempting because Redis is already on the box. Rejected because:
- Streams persistence + replay semantics are weaker than JetStream.
- No native AES-at-rest.
- Subject-based pub/sub fits our taxonomy better than Streams' single-key model.

### A5. Postgres logical replication slot as the "bus".

Rejected: turns Postgres into a queue, which it tolerates but doesn't love. Couples our message model to schema migrations. Also no replay-by-subject.

### A6. Run MCP servers as in-process Python plugins inside core.

Rejected: defeats the sandboxing. The whole point of MCP-as-process is so that a compromised tool runtime can't pivot. We accept the IPC cost.

### A7. S3-with-Object-Lock instead of immudb for the audit log.

Workable for raw retention, but Object Lock doesn't give us per-entry cryptographic verification. immudb does. The external Merkle-root publishing makes immudb's claim auditable from outside.

---

## Resolved decisions (2026-05-23)

The four open questions raised in the initial draft are resolved as follows. Each resolution is reflected in the body of this ADR and, where relevant, in `THREAT_MODEL.md` and `PLAN.md`.

### R1. Vault unseal — manual Shamir-only, no Tang.

3-of-5 Shamir shares held on durable, geographically separated media. Recommended layout: 1× metal seed plate (Cryptosteel/Trezor Steel/Coldcard) in a fireproof safe; 2× paper backups in separate physical locations; remaining 2 shares discretionary (encrypted USB acceptable but not as a primary, given NAND degradation and silent-clone risk). Reboot requires hands-on unseal.

**Rationale.** Co-locating Tang with Vault on the same host defeats the T4 (root-on-box) mitigation: an attacker who roots the host reads Tang's binding and auto-unseals Vault, so the auto-unseal stops contributing security and becomes a convenience feature impersonating one. Shamir-only is the honest choice for a single-server deployment. A separate Tang machine can be revisited in a future ADR if the operator acquires hardware on a different network segment.

### R2. Public attestation log — separate public GitHub repo `rky-2023/openclaw-attestations`.

Only the daily Merkle root (a few hundred bytes) is published. Full immudb payloads stay local; immudb remains the source of truth for the audit ledger. The attestation push is performed by `openclaw-bot` with a credential scope limited to the attestations repo (separate installation, separate App permission grant from the main `oc-ash` install).

**Rationale.** External witness is the entire point of attestation: a local "attestation" file can be rewritten by the same attacker who rewrites immudb, so it witnesses nothing. A separate repo (rather than a branch on `oc-ash`) gives credential-scope isolation: compromise of the main bot install does not let an attacker also rewrite the attestation history.

### R3. License — MIT.

Already in place from GitHub's auto-init.

### R4. Network edge — Tailscale now, Cloudflare Tunnel deferred.

Tailscale provides the mesh between rky's devices and the server, including the Android device. Tailscale ACLs gate per-device access. The Cloudflare Tunnel decision is **deferred** to a future ADR (a separate "ADR-00N: public ingress") rather than locked in this one.

**Until Cloudflare Tunnel lands, GitHub-webhook ingestion is deferred** — Phase 3's webhook receiver is not built. The interim path is GitHub *polling* via the `openclaw-bot` App (~30–60 s latency). This keeps the public attack surface at exactly zero (no public-reachable openclaw service of any kind). FCM also rides Google's network and does not require a public openclaw endpoint.

**Rationale.** A single-user, hardware-key-gated system has no need for public ingress beyond the one webhook source. Deferring that one source until a properly trusted edge exists is cheaper than building a temporary public path we'd then retire. Tailscale Funnel was considered and rejected — it would add Tailscale itself to the trusted public-ingress path before that decision is fully thought through.

---

## Open questions (still pending)

- Android app distribution: sideload-only vs. a private Play track.
- Future ADR on public ingress (Cloudflare Tunnel vs. alternative) — required before Phase 3's GitHub-webhook receiver can be built.

---

## Change log

- **2026-05-23 (v1)** — Accepted as drafted.
- **2026-05-23 (v1.1)** — Four open questions resolved: Shamir-only unseal (no Tang), separate-repo attestation log, MIT license confirmed, Tailscale-now-Cloudflare-later with webhook ingestion deferred. Body and PLAN.md updated to match.
- **2026-05-23 (v1.2)** — ADR-002 and ADR-003 drafted and Accepted; "pending" markers removed from Related and Open Questions sections.
