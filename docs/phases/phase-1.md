# Phase 1 — Secrets foundation & identity

> **Goal:** No secret ever sits in an env file or in git. Every identity is hardware-backed (rky) or short-lived & Vault-issued (services).
> **Anchors:** ADR-001 R1 (Shamir-only), ADR-002 (auth lifecycle), `THREAT_MODEL.md` (T1–T4 mitigations).
> **Estimated wall-clock:** 3–4 working days, plus YubiKey shipping lead time.
> **Output:** A live Vault, two enrolled YubiKeys, an internal CA issuing 24-hour leaf certs, and a smoke test proving that an HTTP request without both mTLS + a YubiKey-signed JWT is refused.

---

## Prerequisites

These are not done by code — order/buy them now if you haven't:

| Item | Notes |
|---|---|
| 2× **YubiKey 5C NFC** | Two devices, not one. ADR-002 D3 refuses to bootstrap with only one. NFC is required for D9 Android pairing. Buy from Yubico direct — chain-of-custody matters. |
| 1× **metal seed plate** (Cryptosteel / Trezor Steel / Coldcard) | Holds the Shamir share that must survive fire/water. |
| 2× **paper** for Shamir shares + YubiKey FIDO2 PIN | Acid-free archival paper recommended. Pencil > ink (ink fades, pencil doesn't bleed if wet). |
| 1× **fireproof / waterproof small safe** | Holds the spare YubiKey + 1 paper Shamir share + the PIN paper. |
| **Tailnet ready** | rky's laptop and the server already on the same tailnet (ADR-001 R4). Android device added later in Phase 9. |
| **Tang server hardware** | **Not needed** — ADR-001 R1 chose Shamir-only. Skipped intentionally. |

**Geographic distribution of the 5 Shamir shares:**

| Share | Medium | Location |
|---|---|---|
| 1 | Metal plate | Fireproof safe at home |
| 2 | Paper | Fireproof safe at home (with metal plate) |
| 3 | Paper | Trusted offsite location A (e.g., parent's house) |
| 4 | Paper | Trusted offsite location B (e.g., bank safe deposit box) |
| 5 | (Optional) Encrypted USB | Anywhere convenient — this is the only one you can lose without consequence |

Threshold is 3-of-5, so any 3 of these must come together for recovery. Losing any 2 is recoverable; losing any 3 is total lockout (by design).

---

## Phase 1 task list

Each task is independently completable and verifiable. Tasks marked **[hands-on]** require physical action; everything else is software.

### 1.1 Prep the dm-crypt volume for Vault storage

**Why:** Vault's storage backend holds wrapped secrets at rest. We want it on a dedicated encrypted volume so that even a host backup tool that grabs `/var/lib/openclaw/` can't see the wrapped bytes without the LUKS passphrase.

**Steps:**
- Allocate a small partition or loop-mounted file (8 GB is plenty for a personal deployment).
- `cryptsetup luksFormat` with a long passphrase. Store the passphrase in your password manager (NOT alongside Shamir shares — Vault unseal and disk unlock are separate ceremonies on purpose).
- Mount at `/var/lib/openclaw/vault/` with `noexec,nosuid,nodev`.
- Add a `systemd` mount unit so reboots prompt for the passphrase before Vault tries to start.

**Verify:** `cryptsetup status vault-data` shows the mapping active and the cipher is `aes-xts-plain64` (default) or `aes-xts` family.

---

### 1.2 Install Vault

**Why:** Vault is the universal CA + KV store + transit signer + AppRole broker. Almost everything downstream needs it.

**Steps:**
- Pull the official Vault binary release (verify SHA256 from HashiCorp + Cosign signature if available).
- Place at `/usr/local/bin/vault`, owned root:root, mode 0755.
- Configure as a `systemd` unit running as a dedicated `vault` user.
- Storage stanza: `raft` (integrated storage), data dir `/var/lib/openclaw/vault/`.
- Listener: `tcp` bound to `127.0.0.1:8200` only — Vault is never directly internet-exposed; access is via openclaw-core which proxies needed operations.
- `disable_mlock = false` — keep mlock on (the default) so secrets don't swap.
- TLS: self-signed bootstrap cert for the listener (we replace it with a Vault-PKI-issued cert in 1.7).

**Verify:** `systemctl status vault` shows active; `vault status` returns "Sealed: true" (expected — we initialize next).

---

### 1.3 Initialize Vault with Shamir 3-of-5

**Why:** This is the irreversible ceremony that produces the recovery shares.

**Steps:**
- Run `vault operator init -key-shares=5 -key-threshold=3`.
- Vault prints 5 unseal keys + 1 root token. **Capture them once; do not lose this moment.**
- **[hands-on]** Immediately distribute the 5 shares per the table above. Metal plate first (it's the only one that matters in a fire). Paper second. Optional encrypted USB last.
- The root token is **destroyed** after step 1.5 — copy it to a temporary file only, and only for the next few minutes.

**Verify:**
- `vault operator unseal <share-1>`, `<share-2>`, `<share-3>` in sequence; after the third Vault is unsealed.
- `vault status` shows `Sealed: false`.
- **[hands-on]** Practice a cold unseal: stop Vault, start Vault, unseal from your stored shares. Time it. If it takes more than 5 minutes, your share storage is too inconvenient — fix it now while it's fresh.

---

### 1.4 Audit logging on

**Why:** Vault's own audit log lives outside immudb (which doesn't exist yet) but must exist before any other operations so we have a record of the bootstrap.

**Steps:**
- `vault audit enable file file_path=/var/log/openclaw/vault-audit.log`
- Set `mode=0600`, owned by the `vault` user.
- Rotate via `logrotate` daily, retain 30 days locally; later (Phase 11) shipped to Loki + mirrored into immudb.

**Verify:** Tail the file and run `vault token lookup`; the lookup operation must appear in the audit log within a second.

---

### 1.5 Enable secret engines and define paths

**Why:** Every downstream component fetches secrets from a known path. Fix the layout now.

**Engines to enable:**

| Path | Engine | Purpose |
|---|---|---|
| `kv/` | `kv-v2` | Generic key/value (e.g., OAuth refresh tokens, GitHub App key reference) |
| `pki_root/` | `pki` | Internal CA root (long-lived) |
| `pki_int/` | `pki` | Intermediate CA (signs leaf certs) — separate so we can rotate without re-rooting trust |
| `transit/` | `transit` | Signing keys for service JWTs, audit envelopes, attestation roots |
| `auth/approle/` | `auth approle` | Per-service AppRoles |
| `auth/userpass/` | (not enabled) | We explicitly do not use password auth |

Path layout convention (under `kv/`) — only **high-value** material per ADR-002 D12:

```
kv/openclaw/
  oauth/google/calendar/   { client_secret, refresh_token }
  oauth/google/gmail/      { client_secret, refresh_token }
  github-app/openclaw-bot/ { private_key_ref → transit }
  fcm/                     { server_key_ref → transit }
  webauthn/                { credential_admin_key_ref → transit }
```

**Low-value, public-by-design material goes in Postgres `openclaw.lookup`, NOT in Vault** (per ADR-002 D12). That includes:
- OAuth `client_id` for google/calendar and google/gmail
- GitHub `app_id` and `installation_ids`
- FCM `project_id` and topic names
- WebAuthn `rp_id`, `rp_name`
- `redaction-policy/v3/policy_sha256` (it's a hash; not a secret)

The Postgres lookup table is created in Phase 2 task 2.1 (it's part of the `openclaw` schema setup).

Each downstream service's AppRole has read access only to its own subtree under `kv/`. No service can read another service's secrets.

**Verify:** `vault kv list kv/openclaw/` shows the structure; an unauthenticated `curl` to `127.0.0.1:8200/v1/kv/data/openclaw/oauth/google/calendar` returns 403.

---

### 1.6 Destroy the root token

**Why:** A root token is a "skip all policy" pass. Holding it after bootstrap is the most common Vault footgun. ADR-002 D11 specifies that all post-bootstrap admin happens via short-lived tokens.

**Steps:**
- Create an admin AppRole `auth/approle/role/openclaw-admin` with the `root`-equivalent policy *but* with `token_ttl=15m`, `token_max_ttl=1h`, `bind_secret_id=true`.
- Save the `role_id` (non-secret) and a fresh `secret_id` to your password manager (NOT alongside Shamir).
- `vault token revoke <root-token>` — destroys the bootstrap root.
- From now on, admin operations: `vault write auth/approle/login role_id=... secret_id=...` → get a 15-minute admin token → do the thing → token expires.

**Verify:** `vault token lookup <root-token>` returns `error: token not found`.

---

### 1.7 Bootstrap the internal CA

**Why:** mTLS between every service is foundational. The CA must exist before any service starts.

**Steps:**
- Generate a root CA in `pki_root/` with `ttl=10y`, subject CN `openclaw root CA`, **only ever used to sign the intermediate**.
- Generate an intermediate in `pki_int/` with `ttl=1y` (rotated annually per ADR-002 D12).
- Sign the intermediate CSR using the root.
- Configure `pki_int/roles/server` and `pki_int/roles/client` with `max_ttl=24h`, `key_type=ed25519`, `allowed_uri_sans=spiffe://openclaw.local/*`.
- Replace Vault's own listener cert with one issued from `pki_int/` (so Vault itself is part of its own trust chain).

**Verify:** `vault write pki_int/issue/server common_name=test.openclaw.local uri_sans=spiffe://openclaw.local/test/test` returns a cert; `openssl x509 -in <that-cert> -text -noout` shows the SPIFFE URI SAN and 24-hour notAfter.

---

### 1.8 Set up the WebAuthn relying party

**Why:** Before enrolling YubiKeys we need a Relying Party (RP) configured. The RP is the openclaw web UI's tiny FastAPI stub, served only on the tailnet.

**Steps:**
- Stand up a minimal FastAPI service at `core/` (just the auth endpoints — no audit, no MCP yet). Run it with mTLS on the tailnet under a name like `oc.<tailnet>.ts.net`.
- Configure RP:
  - `RP ID = oc.<tailnet>.ts.net`
  - `RP Name = openclaw`
  - `pubKeyCredParams = [Ed25519 (alg -8)]`
  - `authenticatorSelection.residentKey = required`
  - `userVerification = required`
  - `attestation = direct`
- Store the credential admin key (used to revoke credentials administratively) in Vault `transit/keys/webauthn-admin`. **The key is generated inside Vault transit and never exported.**

**Verify:** `curl https://oc.<tailnet>.ts.net/auth/webauthn/options` returns valid registration options JSON.

---

### 1.9 Enroll the primary YubiKey [hands-on]

**Why:** The first hardware identity. Until this exists you can't admin anything past the AppRole shell.

**Steps:**
- **[hands-on]** Plug in YubiKey #1. Set the FIDO2 PIN using `ykman fido access change-pin`. Choose a high-entropy PIN; write it on paper now and put it in the safe with share #2.
- Browse to the openclaw web UI registration endpoint (in a browser on the laptop, on the tailnet).
- Complete WebAuthn registration. The browser will prompt for YubiKey touch + PIN.
- The server stores the credential ID, public key, and direct attestation in `kv/openclaw/webauthn/credentials/<credential-id>`.

**Verify:** `curl https://oc.<tailnet>.ts.net/auth/webauthn/credentials` (with a valid session) shows exactly one credential.

---

### 1.10 Enroll the spare YubiKey [hands-on]

**Why:** ADR-002 D3 refuses to proceed with only one. If you skip this step you've already broken the threat model.

**Steps:**
- **[hands-on]** Plug in YubiKey #2. Set its FIDO2 PIN (can be different — they don't need to match, but you'll need both PINs noted on the paper in the safe).
- While still logged in with #1, register #2 through the same enrollment flow.
- **[hands-on]** Put YubiKey #2 in the safe with the paper and the metal plate. Do not carry both keys at once.

**Verify:** `webauthn/credentials` lists two credentials. `oc auth list` (the new CLI you build alongside) shows both.

---

### 1.11 Test the full login + refresh + revoke loop

**Why:** Every claim in ADR-002 D5 must be falsifiable. We test them.

**Steps:**
- Log out. Log back in with #1: WebAuthn challenge → touch → access JWT (5-min) + refresh cookie (24-hr, HttpOnly+Secure+SameSite=Strict).
- Wait 5 minutes. Use the API; access fails. Trigger refresh; **the server must demand a fresh YubiKey touch.** If it doesn't, fix it before continuing — this is the single most important behavioral test in Phase 1.
- After successful refresh, the old refresh cookie is invalidated (single-use refresh). Replay it; must fail.
- `oc auth revoke-key <credential-id-of-#1>` while authenticated with #2. Subsequent logins with #1 are refused. Re-enroll #1 to recover.

**Verify all four behaviors** before declaring Phase 1 done.

---

### 1.12 vault-agent sidecar template

**Why:** Every downstream service needs an mTLS leaf cert that auto-rotates every 24 hours. vault-agent is the standard pattern.

**Steps:**
- Write a Jinja2-templated `vault-agent.hcl` that:
  - Authenticates with the service's AppRole (role_id baked, secret_id injected at start).
  - Caches the AppRole-derived token.
  - Renews the lease on a schedule.
  - Renders an mTLS cert + private key to a tmpfs path on a 24-hour cycle (with a `pre_renew_hook` that signals the service via SIGHUP for graceful reload).
- Add a `Dockerfile.vault-agent-sidecar` base image; each service's compose entry references it.

**Verify:** Run a dummy service with the sidecar. Confirm the cert at `/run/secrets/tls.crt` exists and is signed by `pki_int/`. Tail the sidecar logs; observe a renewal happen on schedule.

---

### 1.13 Transit signing keys for service JWTs

**Why:** ADR-002 D7 says per-MCP-call JWTs are minted by core, signed by Vault transit. Set up the keys now.

**Steps:**
- `vault write -f transit/keys/core-jwt` (Ed25519, `auto_rotate_period=720h` = monthly per ADR-002 D12).
- `vault write -f transit/keys/audit-appender` (separate key, same algo, same rotation).
- `vault write -f transit/keys/attestation-2026-q2` (the daily-Merkle-root signing key, rotates quarterly — see ADR-003 D7).
- Verify policy: `core-jwt-sign` policy allows `sign` on `transit/sign/core-jwt` but not `read` or `export`.

**Verify:** `vault write transit/sign/core-jwt input=<base64-of-test-payload>` returns a signature; `transit/export/core-jwt` returns 403.

---

### 1.14 First mTLS smoke test (the exit criterion)

**Why:** Phase 1 is done when this works.

**Setup:** Stand up a deliberately-stub service `core-echo` that:
- Listens on mTLS only.
- Refuses connections without a valid client cert from `pki_int/`.
- For requests that pass mTLS, additionally verifies an `Authorization: Bearer <JWT>` signed by `transit/keys/core-jwt`, with the operator's enrolled WebAuthn credential as the `sub`.
- For requests that pass both, returns 200 with `{"ok": true}`.

**Tests:**

| # | Request | Expected |
|---|---|---|
| 1 | `curl` from a host with no client cert | TLS handshake failure |
| 2 | `curl` with a valid client cert but no JWT | 401 `missing token` |
| 3 | `curl` with a valid client cert + a JWT signed by some other key | 401 `bad signature` |
| 4 | `curl` with a valid client cert + a JWT signed by `core-jwt` but issued > 5 min ago | 401 `expired` |
| 5 | `curl` with both, fresh | 200 |

All five tests must pass. Capture the test run in `tests/phase-1-smoke.sh` and commit it.

---

### 1.15 Document the bootstrap in the runbook

**Why:** Phase 12 produces `docs/RUNBOOK.md`. Start it now with the Phase 1 break-glass procedures while the steps are fresh.

**At minimum, capture:**
- How to cold-unseal Vault from Shamir shares (test it).
- How to revoke and re-enroll a YubiKey (test it).
- How to rotate the intermediate CA without downtime (read the steps; don't actually rotate yet).
- Where every share, paper backup, and PIN lives. **Do not write the actual share values in the runbook** — just locations and a checksum so you can verify they haven't degraded.

---

## Phase 1 exit criteria (all must be true)

- [ ] Vault unsealed, root token destroyed, admin via AppRole only.
- [ ] `pki_int/` issuing 24-hour Ed25519 leafs with SPIFFE URI SANs.
- [ ] Two YubiKeys enrolled. WebAuthn login + YubiKey-touch-gated refresh + revoke loop verified.
- [ ] vault-agent template renders mTLS certs to a dummy service and rotates them on schedule.
- [ ] Transit signing keys live for `core-jwt`, `audit-appender`, `attestation-2026-q2`.
- [ ] Five-test smoke (`tests/phase-1-smoke.sh`) all green.
- [ ] Cold-unseal rehearsal completed and timed. Result documented in the runbook.

When all seven boxes are ticked, mark Phase 1 done in PLAN.md and proceed to Phase 2.

---

## Rollback / panic procedures

If something goes wrong mid-Phase-1, options in order of preference:

1. **Re-run the affected sub-step.** Most steps are idempotent.
2. **`vault operator seal` and rebuild.** If Vault config is wrong, sealing it loses no data; correct the config and unseal again.
3. **Wipe and re-init Vault.** If the bootstrap genuinely went sideways (e.g., you lost the unseal shares before distributing them), `systemctl stop vault`, `rm -rf /var/lib/openclaw/vault/*`, restart, re-init. **You lose nothing because Phase 1 hasn't put anything irreplaceable in Vault yet.** This is *only* safe before downstream services start using the secrets — once Phase 2 starts wiring up immudb signing keys, Vault wipe means signing-key loss.

The point: keep Phase 1 **wipe-safe** until you're confident in the setup, then move forward.

---

## What goes into git, what doesn't

| Goes in git | Stays out of git |
|---|---|
| Vault `policy/*.hcl` files | Unseal shares (obviously) |
| AppRole policy definitions | role_id values? Yes (non-secret per design); but never commit secret_ids |
| Jinja2 templates for vault-agent | Rendered TLS certs |
| `tests/phase-1-smoke.sh` | The JWT samples it generates at runtime |
| WebAuthn relying-party config | Credential IDs (could be slightly identifying) |
| This runbook | Actual PIN values; actual share locations beyond the abstract table above |

`.gitignore` already covers most of this (see `secrets/`, `*.pem`, `*.key` entries).

---

## Change log

- **2026-05-23 (v1)** — Drafted alongside ADR-002 and ADR-003 acceptance. Will be revised once execution begins and real-world friction surfaces.
- **2026-05-23 (v1.1)** — Task 1.5 Vault path layout aligned with ADR-002 D12 (Secret-vs-config split): public-by-design lookups (client_id, app_id, installation_ids, rp_id, FCM project IDs, redaction policy hash) moved out of `kv/openclaw/` and into the Postgres `openclaw.lookup` schema (created in Phase 2 task 2.1).
