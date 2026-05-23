# Phase 1 — Secrets foundation & identity

> **Goal:** No secret ever sits in an env file or in git. Every identity is hardware-backed (rky) or short-lived & Vault-issued (services).
> **Anchors:** ADR-001 R1 (Shamir-only), ADR-002 (auth lifecycle), `THREAT_MODEL.md` (T1–T4 mitigations).
> **Estimated wall-clock:** 3–4 working days, plus YubiKey shipping lead time.
> **Output:** A live Vault, two enrolled YubiKeys, an internal CA issuing 24-hour leaf certs, and a smoke test proving that an HTTP request without both mTLS + a YubiKey-signed JWT is refused.

---

## Prerequisites

Two prerequisite tracks: **steady-state (ADR-002 D2)** and **cost-free interim (ADR-002 D13)**. Pick the interim track for now; migrate to steady-state when hardware arrives. The Vault setup itself is identical; only the authenticator changes.

### Steady-state hardware (when budget allows — ADR-002 D2):

| Item | Notes |
|---|---|
| 2× **YubiKey 5C NFC** | Two devices, not one. ADR-002 D3 refuses to bootstrap steady-state with only one. NFC is required for D9 Android pairing. Buy from Yubico direct — chain-of-custody matters. |
| 1× **metal seed plate** (Cryptosteel / Trezor Steel / Coldcard) | Optional but recommended: holds the Shamir share that must survive fire/water. |
| 1× **fireproof / waterproof small safe** | Optional but recommended: holds spare YubiKey + 1 paper Shamir share + PIN paper. |

### Cost-free interim (ADR-002 D13 active — use this **now**):

| Item | Notes |
|---|---|
| **Platform authenticator** on laptop | Touch ID (Mac) / Windows Hello (Win 10/11) / `libfido2`+TPM2 (Linux). No purchase required. |
| (Optional) **Second device** | Phone or second laptop, also enrolled as a platform authenticator. Recovers the "two authenticators" property of D3 in software form. |
| **Paper + pencil** | For Shamir shares (~5 small slips). |
| **Sealed envelopes** | Standard envelopes to protect paper shares from casual reading. |
| **Password manager** with encrypted-notes feature | Bitwarden free / KeePassXC / 1Password / etc. Holds 1 of the 5 Shamir shares as an encrypted text note. |
| **Tailnet ready** | rky's laptop and the server already on the same tailnet (ADR-001 R4). Android added later in Phase 9. |
| **Tang server hardware** | **Not needed** — ADR-001 R1 chose Shamir-only. Skipped intentionally. |

### Shamir distribution — what you actually do

When `vault operator init -key-shares=5 -key-threshold=3` runs (task 1.3), Vault prints 5 unseal keys and 1 root token to your terminal **once**. After you close the terminal, they're gone forever. You must capture them in the same session and immediately distribute them.

**Cost-free 5-location distribution:**

| Share # | Medium | Location | Rationale |
|---|---|---|---|
| 1 | Paper (pencil, sealed envelope) | **In your wallet** | Always on you; fastest recovery; survives house fire because it's with you |
| 2 | Paper (pencil, sealed envelope) | **Drawer at home** (kitchen / desk / bedside) | Most likely place you'll be when you need it |
| 3 | Paper (pencil, sealed envelope, mailed) | **Trusted family member's home** | Geographic separation from #1 and #2; house fire can't take all three |
| 4 | Encrypted note in your **password manager** | Digital, accessible from anywhere you can log in | Cloud-redundant; survives device loss |
| 5 | Paper (pencil, sealed envelope) | **Obscure on-site spot** (e.g., taped to back of picture frame, inside an old book on a high shelf) | Second on-site copy; hard for a casual breach to find |

Threshold is **3-of-5**: any 3 reconstruct the secret; fewer than 3 reveal mathematically nothing. So losing any 2 shares is fine. Losing 3 simultaneously (e.g., wallet stolen + house fire + password manager compromise on the same day) is total lockout — and that's the design.

**Hard rules (don't violate these):**

- Don't store all 5 in one location (defeats Shamir entirely).
- Don't store any share digitally without encryption — `~/shamir.txt` on the laptop is unacceptable.
- Don't email shares to yourself — Gmail compromise → openclaw compromise.
- Don't store the unseal shares with your password-manager **master password** (single point of failure).
- Don't store any share with your Vault dm-crypt **disk passphrase** — a single physical breach must never yield both.
- Don't store shares with the FIDO2 PIN paper — same reason.

**Upgrade path (when you have budget):**

Replace any paper share with a metal seed plate at the same location. The most important share to upgrade is #2 or #5 (the "fireproof safe at home" share), since paper at home is the most vulnerable to house fire. The metal plate is ~$30 and is a meaningful upgrade.

---

## Phase 1 task list

Each task is independently completable and verifiable. Tasks marked **[hands-on]** require physical action; everything else is software.

### 1.1 Prep the dm-crypt volumes for openclaw services

**Why:** Vault, immudb, NATS, and MinIO all hold sensitive data. We want them on dedicated encrypted volumes so that even a host backup tool that grabs the data directories can't see the wrapped bytes without the LUKS passphrase.

**Layout (post-fix for snap Docker):** the LUKS image files live at `/var/lib/openclaw/luks/<service>.img`, and the unlocked decrypted filesystems are mounted at `/mnt/openclaw/<service>/`. Mount points live under `/mnt/` because snap-installed Docker is confined and cannot see `/var/lib/`; the LUKS images stay under `/var/lib/` because Docker doesn't need access to them.

**Script:** [`infra/scripts/00-create-luks-volumes.sh`](../../infra/scripts/00-create-luks-volumes.sh) — creates 4 LUKS-on-file containers (vault 8 GiB / immudb 50 GiB / nats 20 GiB / minio 50 GiB), one shared bootstrap passphrase, mounts under `/mnt/openclaw/<name>/`. Idempotent.

**Steps:**
1. Run as root: `sudo ./infra/scripts/00-create-luks-volumes.sh`
2. The script prompts for a single bootstrap passphrase used across all 4 volumes. Save this passphrase in your password manager **separately** from your Shamir shares — disk unlock and Vault unseal are independent ceremonies on purpose.
3. After the script completes, set up `/etc/crypttab` + `/etc/fstab` entries (the script prints templates) so the volumes prompt-mount on boot.

**Verify:** `cryptsetup status oc-vault-luks` shows the mapping active and the cipher is `aes-xts-plain64`. `mountpoint /mnt/openclaw/vault` returns success.

**Per-volume passphrase rotation (optional, later):** if you want different passphrases per volume, use `cryptsetup luksAddKey <img>` + `luksRemoveKey` after bootstrap. The LUKS header has 8 key slots.

---

### 1.2 Bring up Vault

**Why:** Vault is the universal CA + KV store + transit signer + AppRole broker. Almost everything downstream needs it.

**Note on deployment model:** Vault runs as a container per the Phase 2 scaffold's `infra/docker-compose.openclaw.yml`. The container is built from `hashicorp/vault:1.18.0`, binds only `127.0.0.1:8200`, mounts the dm-crypt volume from 1.1, and uses Raft integrated storage with `disable_mlock = false` and TLS bootstrap-disabled (re-enabled in 1.7 with a Vault-PKI-issued cert).

**Script:** [`infra/scripts/01-bring-up-vault.sh`](../../infra/scripts/01-bring-up-vault.sh) — pre-flight checks (volume mounted, docker reachable) + `docker compose up -d vault` + waits for the container to become reachable + prints status.

**Steps:**
1. `sudo ./infra/scripts/01-bring-up-vault.sh`
2. Vault will be **running but sealed and uninitialized** — expected first-run state.

**Verify:** `docker exec oc-vault vault status` reports `Initialized: false, Sealed: true`.

---

### 1.3 Initialize Vault with Shamir 3-of-5

**Why:** This is the irreversible ceremony that produces the recovery shares.

**Scripts:**
- [`infra/scripts/02-init-vault.sh`](../../infra/scripts/02-init-vault.sh) — one-time `vault operator init -key-shares=5 -key-threshold=3`. Refuses to run if Vault is already initialized. Prints all 5 keys + the root token to your terminal once.
- [`infra/scripts/03-unseal-vault.sh`](../../infra/scripts/03-unseal-vault.sh) — interactive 3-share unseal. Run this both after init AND on every reboot (Vault is sealed by design at startup — ADR-001 R1).

**Steps (first time only):**
1. `sudo ./infra/scripts/02-init-vault.sh` — confirm by typing `READY`. Vault prints 5 unseal keys + 1 root token.
2. **[hands-on]** Immediately distribute the 5 shares per the cost-free distribution table in the prerequisites section (wallet / drawer / family / password manager / obscure on-site spot). Copy down the root token to a temporary location — you need it for task 1.6.
3. **Clear your terminal scrollback** before walking away: `reset; clear; history -c` (or just close + reopen the window).
4. `sudo ./infra/scripts/03-unseal-vault.sh` — prompts for any 3 of your 5 shares (hidden input). After the 3rd share Vault is unsealed.

**Steps (every reboot):**
- `sudo ./infra/scripts/03-unseal-vault.sh` — same prompt, same 3 shares.

**Verify:**
- `docker exec oc-vault vault status` shows `Sealed: false`.
- **[hands-on]** Practice a cold unseal: `docker compose -f infra/docker-compose.openclaw.yml restart vault` → run `03-unseal-vault.sh` from your stored shares. Time it. If it takes more than 5 minutes, your share storage is too inconvenient — fix it now while it's fresh.

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

### 1.9 Enroll the primary authenticator [hands-on]

**Why:** The first hardware identity. Until this exists you can't admin anything past the AppRole shell.

#### 1.9a — Cost-free interim path (ADR-002 D13 active):

Use the laptop's **platform authenticator** instead of a YubiKey:

- **macOS:** Touch ID (Secure Enclave). Works in Safari / Chrome / Firefox / Brave.
- **Windows 10/11:** Windows Hello with TPM 2.0. Requires a Microsoft account or local PIN setup.
- **Linux:** `libfido2` + TPM 2.0. Most laptops since ~2018 have TPM 2.0. In Chrome/Firefox the platform authenticator appears automatically once `libfido2` is installed (`sudo apt install libfido2-1 libfido2-dev` on Debian/Ubuntu).
- **ChromeOS:** Built-in WebAuthn, TPM-backed.

**Steps:**
- **[hands-on]** Browse to the openclaw web UI registration endpoint on the laptop (on the tailnet).
- When the WebAuthn prompt appears, **choose "this device"** (or equivalent) rather than "USB security key."
- Verify with Touch ID / Hello PIN / TPM PIN as prompted.
- The server stores the credential ID, public key, and direct attestation in `kv/openclaw/webauthn/credentials/<credential-id>`.

**Verify:** `curl https://oc.<tailnet>.ts.net/auth/webauthn/credentials` (with a valid session) shows exactly one credential with `attestation.aaguid` reporting a platform authenticator (not a YubiKey AAGUID).

#### 1.9b — Steady-state path (when YubiKeys arrive):

- **[hands-on]** Plug in YubiKey #1. Set the FIDO2 PIN using `ykman fido access change-pin`. Choose a high-entropy PIN; write it on paper now and put it in the safe.
- Log in with the platform authenticator from 1.9a, then enroll YubiKey #1 as an *additional* authenticator (do not displace the platform one until both YubiKeys are working).
- Test login from YubiKey #1 alone (no platform authenticator). Must succeed.

**Verify (steady-state):** `webauthn/credentials` lists at least one YubiKey + one platform authenticator. The YubiKey's `attestation.aaguid` matches Yubico's published GUIDs.

---

### 1.10 Enroll the spare authenticator [hands-on]

**Why:** ADR-002 D3 refuses to call the system "production-ready" with only one authenticator. ADR-002 D13 (interim) accepts a single platform authenticator at bootstrap *but* strongly recommends a second device be enrolled.

#### 1.10a — Cost-free interim path:

Use a **second device** (phone or second laptop) as a second platform authenticator:

- **Phone:** Browse to the web UI on the phone (it's on the tailnet — make sure Tailscale is installed and authenticated on the phone). The phone's platform authenticator (Touch ID / Face ID / Android biometric) acts as the second factor.
- **Second laptop:** Same as 1.9a but on the other machine.

**Steps:**
- While logged in on the primary device (from 1.9a), open the registration endpoint on the secondary device.
- Complete WebAuthn registration with the secondary device's biometric/PIN.
- Test login from each device independently. Both must work.

If you don't have a second device available, you may proceed with only one platform authenticator — but document this in `docs/RUNBOOK.md` as a known reduced-redundancy state and migrate to a second authenticator (device or YubiKey) as soon as practical.

#### 1.10b — Steady-state path (when YubiKeys arrive):

- **[hands-on]** Plug in YubiKey #2. Set its FIDO2 PIN (can be different from #1; both noted on PIN paper in safe).
- While logged in with YubiKey #1, register YubiKey #2.
- **[hands-on]** Put YubiKey #2 in the safe. Do not carry both keys at once.
- **Optionally** revoke the platform authenticators with `oc auth revoke-key <credential-id>` (returns to strict D2/D3 compliance), or leave one platform authenticator as a third backup (slight D3 deviation but more recovery margin).

**Verify (steady-state):** `webauthn/credentials` lists two YubiKey credentials. Login from each YubiKey independently must succeed.

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
3. **Wipe and re-init Vault.** If the bootstrap genuinely went sideways (e.g., you lost the unseal shares before distributing them), `docker compose stop vault`, `rm -rf /mnt/openclaw/vault/*`, `docker compose start vault`, re-init. **You lose nothing because Phase 1 hasn't put anything irreplaceable in Vault yet.** This is *only* safe before downstream services start using the secrets — once Phase 2 starts wiring up immudb signing keys, Vault wipe means signing-key loss.

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
- **2026-05-23 (v1.2)** — Prerequisites restructured into "steady-state hardware" (ADR-002 D2) and "cost-free interim" (ADR-002 D13) tracks. Cost-free Shamir distribution table added with concrete locations and hard rules. Tasks 1.9/1.10 split into 1.9a/1.9b/1.10a/1.10b reflecting interim platform-authenticator path and steady-state YubiKey path.
- **2026-05-23 (v1.3)** — Tasks 1.1, 1.2, 1.3 rewritten to reference the new `infra/scripts/` helpers (00-create-luks-volumes / 01-bring-up-vault / 02-init-vault / 03-unseal-vault). 1.1 expanded to cover all 4 dm-crypt volumes (vault + immudb + nats + minio) since Phase 2 needs them too. 1.2 reframed: Vault runs as a docker-compose container, not a host systemd unit, matching the Phase 2 scaffold.
- **2026-05-23 (v1.4)** — Task 1.1 mount points moved from `/var/lib/openclaw/<svc>/` to `/mnt/openclaw/<svc>/` because snap-installed Docker is confined and cannot see `/var/lib/`. LUKS image files remain at `/var/lib/openclaw/luks/<svc>.img` (Docker never accesses those). Docker-compose bind-mount paths and rollback procedure 3 updated accordingly.
