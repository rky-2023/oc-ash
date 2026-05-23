# Threat Model

> **Status:** Draft v1 — 2026-05-23
> **Owner:** rky
> **Review cadence:** quarterly, and on any new MCP server / event source / external integration.
> **Related ADRs:** ADR-001 (architecture), ADR-002 (auth — pending), ADR-003 (audit — pending).

This document is the source of truth for "what is openclaw defending, against whom, and to what depth." If the code disagrees with this doc, one of them is wrong — fix it.

---

## 1. System boundaries (what's in scope)

In scope (we own and defend):

- `openclaw-edge` (Caddy + Cloudflare Tunnel)
- `openclaw-core` (FastAPI, MCP proxy, A2A router, audit appender)
- MCP servers under `mcp/` (google-calendar, gmail, github, fs-asher, notifier-fcm)
- Agent processes under `agents/`
- Event ingesters under `ingest/` (fswatch, githooks, gh-webhook, claude-hooks)
- Notifier FCM bridge (`notifier/fcm/`)
- Android RN app (`mobile/`)
- Vault, immudb, NATS JetStream, MinIO (new)
- Postgres, Redis, Mosquitto (shared with Ashboard)
- Audit ledger and viewer
- GitHub App "openclaw-bot" private key
- Two YubiKeys (primary on rky, spare in safe)

Out of scope (we depend on but do not own):

- Google Cloud (Calendar, Gmail, Pub/Sub)
- Firebase Cloud Messaging
- GitHub.com
- Cloudflare (Tunnel, DNS)
- Backblaze B2 (encrypted backups)
- ISP, residential router, ISP DNS
- Host Linux kernel and firmware
- Android device firmware / Play Integrity provider

**Implication:** if any out-of-scope dependency is compromised, we lose at least the data flowing through it. Mitigation is scope minimization (smallest OAuth scopes, E2E-encrypted FCM payloads, smallest GitHub App permissions) so compromise of one does not cascade.

---

## 2. Adversary tiers

| Tier | Description | Capability | Posture |
|---|---|---|---|
| **T1** | Unauthenticated internet rando / commodity botnet | Scanning, default-creds, public CVEs, generic DDoS | **Fully resist.** Public surface is webhook stub + FCM relay; both behind Caddy + WAF + rate limits + fail2ban. No SSH on default port. No public admin UI. |
| **T2** | Phished session token / stolen cookie | Has a valid JWT or session cookie, no hardware key | **Fully resist.** Access JWTs are 5-minute TTL; refresh requires YubiKey touch; mTLS client cert pinned per device. Token alone is useless. |
| **T3** | Lost / stolen YubiKey (one of two) | Physical possession of primary YubiKey, no PIN | **Resist.** YubiKey FIDO2 PIN required; lockout after 8 wrong PINs. Spare YubiKey in safe can revoke primary. Each YubiKey is one of two registered authenticators; either can revoke the other. |
| **T4** | Single compromised non-root user on host | Code execution as a service account (e.g., compromised MCP) | **Resist.** Each MCP is in a gVisor sandbox with seccomp + RO root + no-new-privileges + dropped caps. Cannot reach other MCPs or the host filesystem outside its mount. mTLS prevents lateral movement. |
| **T5** | Targeted persistent adversary (state-level, physical access, 0-day, supply-chain leverage) | Everything T4 plus kernel exploit, hardware tampering, signed-update tampering, coercion | **Detect, don't promise to prevent.** Honest framing: a single home box cannot fully resist this. Investment is in *detection* (immutable audit log, externally-published Merkle root, Falco, Wazuh) and *recovery* (offline backups, rotation playbooks) rather than blanket prevention. |

---

## 3. STRIDE analysis per component

STRIDE = **S**poofing · **T**ampering · **R**epudiation · **I**nformation disclosure · **D**enial of service · **E**levation of privilege.

### 3.1 openclaw-edge (Caddy)

| Threat | Vector | Mitigation |
|---|---|---|
| S | Forged Host header to bypass routing | Caddy strict SNI match; per-vhost mTLS where applicable |
| T | TLS cert swap | Internal CA pinned; ACME for public certs with CAA records locking issuers |
| R | "I didn't send that webhook" | All inbound webhook HMACs logged with raw payload hash |
| I | TLS downgrade | TLS 1.3 only; HSTS preload; no fallback ciphers |
| D | L7 flood | Caddy rate-limit module; Crowdsec/fail2ban; Cloudflare Tunnel absorbs upstream |
| E | Caddy RCE via plugin | Pinned Caddy version with SBOM; no community plugins; runs as non-root in container |

### 3.2 openclaw-core (FastAPI)

| Threat | Vector | Mitigation |
|---|---|---|
| S | Forged JWT | Signed by Vault transit; 5-min TTL; refresh requires YubiKey touch; jti deny-list for revocation |
| T | Tampered request body | mTLS client cert; HMAC on Unix-socket variants; Pydantic strict-mode validation |
| R | "Core didn't do that" | Every state-changing endpoint envelopes request+response to immudb before responding |
| I | SQLi / log leakage | SQLModel parameterized queries only; redaction pass via `policy/redaction.rego` before any persistence |
| D | Slowloris / async pool exhaustion | uvicorn worker limits; per-IP concurrency caps at Caddy |
| E | Privilege escalation via deserialization | No pickle anywhere; JSON only; Pydantic strict types |

### 3.3 MCP proxy (inside core)

| Threat | Vector | Mitigation |
|---|---|---|
| S | Agent A pretending to be Agent B | mTLS client cert + per-agent SVID; cert thumbprint matched to agent-card registry |
| T | Modified tool arguments mid-flight | All MCP calls travel over Unix socket inside the container network; mTLS for cross-host |
| R | Agent denies calling a tool | Conversation_id + agent_id + tool args + result hash all in immudb before forwarding |
| I | Secret leakage through tool args | `policy/redaction.rego` runs *before* immudb append; secrets matched by regex + entropy heuristic |
| D | Agent calls a tool 10,000 times | `context_budget` per conversation: tool-call count, token count, wall-clock; exceed = halt + audit event |
| E | Tool call escapes sandbox | gVisor + seccomp; tools cannot fork shells; outbound network allowlisted per MCP |

### 3.4 MCP servers (each one)

| Threat | Vector | Mitigation |
|---|---|---|
| S | Forge identity to upstream (Google, GitHub) | OAuth refresh tokens only in Vault; fetched per-call with 15-min service JWT |
| T | Tamper with upstream response before forwarding | Response signed end-to-end by MCP using its service key; proxy validates |
| R | "MCP returned X but logged Y" | Both raw upstream response and MCP-emitted response logged; mismatch = alert |
| I | Cache file leaks tokens | tmpfs only; no on-disk cache; if disk needed, age-encrypted with key in Vault |
| D | Upstream rate-limit DOS | Per-MCP token bucket; backoff on 429; surface to caller with retry-after |
| E | MCP RCE via dependency | Pinned hashes; gVisor; non-root inside container; CAP_DROP=ALL |

### 3.5 Event ingesters

| Threat | Vector | Mitigation |
|---|---|---|
| S | Forged "git commit" event from non-asher user | Unix socket with SO_PEERCRED check; only `rky` uid accepted |
| T | Tampered GitHub webhook payload | HMAC-SHA256 with per-repo secret; secret rotated weekly via Vault |
| R | "We didn't see that PR" | All received events hashed and logged before policy filter |
| I | Webhook leaks sensitive PR diff to disk | Payloads encrypted-at-rest in NATS JetStream (KMS-wrapped DEK) |
| D | GitHub burst on a popular repo | NATS backpressure; ingester drops events past 1000/s with audit-counter increment |
| E | fswatch reading files it shouldn't | Runs as `oc-fswatch` user; only `/home/asher` mount visible; no exec capabilities |

> **Note (2026-05-23):** Per ADR-001 R4, GitHub-webhook ingestion is **deferred** until a public ingress ADR lands. The interim path is **polling** via the `openclaw-bot` GitHub App. While polling is in effect, the "Tampered GitHub webhook payload" and "GitHub burst" rows are not in scope — the relevant mitigations (HMAC, rate limits) will return when the webhook receiver is built.

### 3.6 Notifier FCM bridge → Android app

| Threat | Vector | Mitigation |
|---|---|---|
| S | Attacker registers their device as paired | Pairing requires logged-in WebUI session (YubiKey) + QR scan; Play Integrity attestation on enrollment |
| T | FCM modifies payload | Payload is Curve25519-encrypted client-side; FCM sees ciphertext only; HMAC over ciphertext checked on device |
| R | "I never got that notification" | Delivery receipts (acked decryption) logged to immudb |
| I | FCM reads notification body | E2E encrypted; FCM sees `{cipher, nonce}` plus message-id only |
| D | Notification spam | Per-channel rate cap in `policy/notify.rego` |
| E | Compromised Android app exfiltrates inbox | Private key in Android Keystore (StrongBox if available); never extractable; root detection = wipe |

### 3.7 Vault

| Threat | Vector | Mitigation |
|---|---|---|
| S | Forged unseal | **Manual Shamir-only unseal**, no Tang. 3-of-5 shares held on durable media (metal seed plate + paper backups) in geographically separated locations. Reboot is hands-on by design. Rationale captured in ADR-001 R1: co-locating Tang with Vault defeats T4. |
| T | Tampered backend | Integrated storage (Raft) on dm-crypt volume; consensus checks per write |
| R | "Vault gave out a secret I didn't ask for" | Audit device logs every operation; mirrored to immudb |
| I | Backup leaks secrets | Backups age-encrypted with key on YubiKey; B2 bucket access scoped to write-only credential |
| D | Vault unsealed but unresponsive | Health-check + auto-restart; secondary read-only replica (Phase 11) |
| E | Root token misuse | Root token destroyed after bootstrap; admin via short-lived tokens issued by AppRole + YubiKey |

### 3.8 immudb (audit ledger)

| Threat | Vector | Mitigation |
|---|---|---|
| S | Forged entry appearing in ledger | All writes signed by core's service key; signature verified on read |
| T | Silent ledger rewrite | Nightly Merkle root committed to public `openclaw-attestations` Git repo; any divergence visible globally |
| R | "I never wrote that audit entry" | Writes themselves are signed by the originating service |
| I | Audit log contains sensitive payloads | Redaction pass before append; PII fields stored as Vault-key-encrypted blobs |
| D | Ledger fills disk | Cold-tier rotation: roots stay forever, payloads >90d move to MinIO (still verifiable) |
| E | immudb RCE | Pinned version; gVisor; non-root |

### 3.9 GitHub App ("openclaw-bot") private key

| Threat | Vector | Mitigation |
|---|---|---|
| S | Stolen key opens PRs as the bot | Audit-anchor CI gate: PR body must reference a real immudb conversation_id |
| T | Key swapped in Vault | Vault transit key wrap; rotation triggers audit event |
| R | "Bot opened a PR I didn't authorize" | Every bot action ties to an A2A conversation that traces back to a user-initiated task |
| I | Key disclosed via container env | Never in env; fetched per-call with short-lived JWT |
| D | Bot rate-limited by GitHub | Per-bot token bucket; backoff |
| E | Bot used to bypass branch protection | Bot is never a CODEOWNER; never can self-approve |

### 3.10 Android device

| Threat | Vector | Mitigation |
|---|---|---|
| S | Attacker re-registers paired device | Re-pair always requires fresh WebUI YubiKey session |
| T | Modified APK | Release APK signed with key in Vault; F-Droid-style verify-on-update |
| R | "I never opened that notification" | App reports decryption + open events back via mTLS |
| I | Lock-screen previews leak content | Default to "Sensitive" — preview hidden unless device unlocked |
| D | Notification bomb drains battery | Per-app rate cap at notifier policy layer |
| E | Root / Magisk exfiltration | Play Integrity attested on every register/refresh; downgrade = revoke + wipe device entry |

---

## 4. Trust assumptions (made explicit)

These are things we *assume true* — if any becomes false, the model breaks:

1. The Android Keystore / StrongBox is not extractable by a logical adversary.
2. The YubiKey 5C firmware has no remote-exploitable bug at time of use.
3. The Vault binary we run was the one we built (verified by cosign signature).
4. The host kernel has no in-the-wild 0-day at time of operation.
5. The operator (rky) does not voluntarily install unsigned MCP servers or agents.
6. The clock is roughly correct (within 5 min) — needed for JWT exp and TOTP-style nonces.
7. Google, GitHub, and Firebase do not actively collude to attack the operator.

Assumption 7 is the weakest one. The architecture accepts that compromise of an upstream provider loses the data flowing through that provider — and limits blast radius by scope minimization.

---

## 5. Residual risks (acknowledged, not mitigated)

- **Coerced operator.** If someone has rky in a room, they have openclaw. No software mitigation.
- **Targeted supply-chain attack on a pinned dependency.** Mitigated only partially by SBOM + signing; a malicious cosigned release would still be accepted.
- **Side channels on the host (Spectre-class)**. gVisor mitigates the common cases but is not a kernel.
- **Traffic analysis.** An on-path observer can see "openclaw exists at this IP" even if all content is encrypted. We do not hide existence.
- **Loss of both YubiKeys + Shamir shares**. Designed-in recovery requires at least one of: spare YubiKey OR 3 of 5 Shamir shares. Losing all of these = total lockout, by design.

---

## 6. Review and change process

- This document is reviewed every quarter (next: 2026-08-23).
- Any new MCP server, event source, or external integration triggers an out-of-cycle review *before* it goes live.
- Changes to threat tiers, mitigations, or trust assumptions require an ADR.
- The `THREAT_MODEL.md` history is itself in the audit ledger via Git commit hash → immudb anchor.

---

## 7. Change log

- **2026-05-23** — v1 draft authored alongside scaffolding.
- **2026-05-23 (v1.1)** — Vault unseal row in §3.7 updated to Shamir-only (per ADR-001 R1). §3.5 annotated with webhook-deferral note (per ADR-001 R4).
