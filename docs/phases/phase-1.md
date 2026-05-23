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

### 1.4 / 1.5 / 1.6 — Audit log, secret engines, admin AppRole, destroy root token

These three tasks are bundled into a single ceremony because the safer order — *enable audit before any other config, define all secret paths before any service runs, and destroy the root token only after the admin AppRole exists* — is awkward to break across multiple invocations.

**Script:** [`infra/scripts/04-vault-bootstrap.sh`](../../infra/scripts/04-vault-bootstrap.sh) — idempotent up until the final destroy step (which is gated by typing `DESTROY`).

**What it does:**

| Step | What |
|---|---|
| Pre-flight | Verifies oc-vault is running, initialized, and unsealed. Prompts for the root token (hidden input) and verifies it. |
| 1.4 Audit log | Enables file audit device writing to `/vault/data/audit/file-audit.log` (inside the dm-crypt-backed volume). |
| 1.5 KV-v2 | Mounts `kv-v2` at `kv/` (high-value secrets only — public-by-design config goes to Postgres `openclaw.lookup` per ADR-002 D12). |
| 1.5 PKI root | Mounts `pki_root/` (10y) and generates the internal root CA (Ed25519). |
| 1.5 PKI intermediate | Mounts `pki_int/` (1y per ADR-002 D14), generates an intermediate CSR, signs it with the root, sets the signed cert back on the intermediate. Configures `server` and `client` issuance roles (24h TTL, SPIFFE URI SAN). |
| 1.5 Transit | Mounts `transit/` and creates Ed25519 signing keys (`core-jwt`, `audit-service`, `audit-appender`, `attestation-2026-q2`, `webauthn-admin`) and the `audit-pii-v3` AES-256-GCM key. Monthly auto-rotation for the JWT/audit signers per ADR-002 D14; manual rotation for the attestation key (quarterly) and PII key (version-pinned per envelope). |
| 1.5 AppRole | Enables the AppRole auth method. |
| 1.6 Admin AppRole | Writes the `openclaw-admin` policy (near-root path access), creates the AppRole with `token_ttl=15m`, `token_max_ttl=1h`, generates a fresh secret_id, and prints the role_id + secret_id to the terminal **once**. Pauses for the operator to type `CAPTURED` after storing them in the password manager. |
| 1.6 Destroy root | After a `DESTROY` confirmation prompt, calls `vault token revoke -self` on the root token. From then on, all admin operations require the AppRole login flow. |

Path layout under `kv/` after the script runs is empty (no secrets stored yet). High-value material populates as later phases land:

```
kv/openclaw/
  oauth/google/calendar/   { client_secret, refresh_token }     (Phase 5)
  oauth/google/gmail/      { client_secret, refresh_token }     (Phase 6)
  github-app/openclaw-bot/ { private_key_ref → transit }        (Phase 2 task 2.12)
  fcm/                     { server_key_ref → transit }         (Phase 8)
```

**Steps to run:**

```sh
sudo ./infra/scripts/04-vault-bootstrap.sh
```

You'll be prompted for:
1. The root token (hidden).
2. `CAPTURED` after storing the role_id + secret_id printed in the middle of the run.
3. `DESTROY` to revoke the root token at the end.

If you abort at the `DESTROY` step, the root token survives. Earlier steps are idempotent, so you can re-run the script to retry the destroy.

**Verify:**

- `docker exec -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN=<old-root-token> oc-vault vault token lookup` returns `permission denied` or `token not found`.
- AppRole login works:
  ```sh
  docker exec -e VAULT_ADDR=http://127.0.0.1:8200 oc-vault \
    vault write auth/approle/login \
      role_id=<stored-role-id> secret_id=<stored-secret-id>
  ```
  Returns a 15-minute admin token.
- Audit log has entries:
  ```sh
  docker exec oc-vault tail -1 /vault/data/audit/file-audit.log | head -c 200
  ```

---

### 1.7 Internal CA + Vault listener TLS

The root + intermediate CA + server/client issuance roles were already created by `04-vault-bootstrap.sh` (1.5). This task closes the remaining gap: **swap Vault's own listener from plaintext HTTP to TLS** using a cert issued from `pki_int/`.

**Script:** [`infra/scripts/05-vault-tls-listener.sh`](../../infra/scripts/05-vault-tls-listener.sh).

**What it does:**

| Step | What |
|---|---|
| Pre-flight | Verifies Vault is unsealed. Prompts for `openclaw-admin` Role ID + Secret ID (secret-id is hidden, piped via stdin). |
| Role config | Creates / updates `pki_int/roles/vault-listener` — separate role from the general `server` role because the listener cert has 30-day TTL (no vault-agent self-renewal yet) vs. 24h for service leafs. |
| Issue | Calls `pki_int/issue/vault-listener` with `common_name=vault.openclaw.local`, `alt_names=localhost,vault,oc-vault`, `ip_sans=127.0.0.1`. |
| Place files | Writes `server.crt`, `server.key`, `ca.crt`, `fullchain.crt` into `/vault/data/tls/` inside the container (mode 0600 for key, 0644 for cert/CA, owned by uid 100). |
| Publish CA | Copies the issuing CA cert to `/mnt/openclaw/shared/ca.crt` on the host so other services can bind-mount it as their trust anchor. |
| Restart | `docker compose restart vault`. Vault picks up the TLS-enabled `server.hcl` (already shipped in this PR) and the new cert files. |

The TLS-enabled `server.hcl` is included in the PR; pulling it before running the script ensures the restart works. `docker-compose.openclaw.yml` is also updated: `VAULT_ADDR=https://...` and `VAULT_CACERT=/vault/data/tls/ca.crt` so the container's `vault` CLI verifies the listener cert. All existing scripts (01–04) drop their explicit `-address=http://...` flags and rely on the container env.

**Steps to run:**

```sh
sudo ./infra/scripts/05-vault-tls-listener.sh
# Provide openclaw-admin Role ID + Secret ID when prompted.
sudo ./infra/scripts/03-unseal-vault.sh
```

**Verify:**

```sh
docker exec oc-vault vault status                                  # should report Sealed: false over HTTPS
openssl s_client -connect 127.0.0.1:8200 -showcerts </dev/null     # cert chain rooted at pki_root CA
docker exec oc-vault vault list -format=json pki_int/issuers       # at least one issuer present
```

**Renewal:** the listener cert expires in 30 days. Re-run `05-vault-tls-listener.sh` monthly until vault-agent self-renewal is wired up (Phase 11 hardening or earlier if convenient).

---

### 1.12 vault-agent sidecar template

**Why:** Every downstream openclaw service needs an mTLS leaf cert that auto-rotates well before its 24h TTL expires. The standard pattern is a per-service `vault-agent` sidecar that authenticates via AppRole, renders the cert + key to tmpfs, and signals the service on rotation.

**Files:** [`infra/vault-agent/`](../../infra/vault-agent/) — base image + config template + README.

- `Dockerfile` — `hashicorp/vault:1.18.0` base, runs `vault agent`, non-root (uid 100).
- `vault-agent.hcl.template` — template config with `@@SERVICE_NAME@@` / `@@NAMESPACE@@` placeholders. Substitute with `sed` per service. Renders mTLS cert + key + CA + token to `/run/openclaw/` (tmpfs).
- `README.md` — 6-step per-service usage walk-through (create AppRole → mint credentials → render config → wire compose → service consumes).

**Status:** no service uses this template yet — `core/` is still scaffold and Phase 2 task 2.5 wires it up first. The deployment helper script that automates the 6 manual steps also lands in Phase 2.

**Design notes worth retaining:**

- Agent runs as a *separate container* (sidecar pattern), not in-process. A compromised service can read rendered files but never reads the AppRole secret-id.
- All rendered material lives on tmpfs (`/run/openclaw/`). Disk never sees it.
- 24h cert TTL, 12h render cadence — service never experiences a hard cut-off.
- `uri_sans` uses SPIFFE format so future SPIRE integration is a drop-in (ADR-002 A5).

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
- **2026-05-23 (v1.5)** — Tasks 1.4 / 1.5 / 1.6 collapsed into a single ceremony backed by `infra/scripts/04-vault-bootstrap.sh`. Script enables audit log, mounts kv-v2 + pki_root + pki_int + transit + approle, generates the internal CA hierarchy with 24h-TTL Ed25519 server/client roles, creates the named transit keys, mints the `openclaw-admin` AppRole, and (gated by typing DESTROY) revokes the root token. Task 1.7 mostly absorbed by 1.5 — only the "swap Vault's bootstrap listener to its own pki_int cert" step remains.
- **2026-05-23 (v1.6)** — Task 1.7 completed via `infra/scripts/05-vault-tls-listener.sh`. New `pki_int/roles/vault-listener` (30-day TTL); server.hcl, docker-compose.openclaw.yml updated to enable TLS + verify via `/vault/data/tls/ca.crt`. CA cert published to `/mnt/openclaw/shared/ca.crt` for downstream services. Scripts 01–04 stripped of explicit `-address=http://...` flags (env wins). Task 1.12 vault-agent sidecar template added at `infra/vault-agent/` (Dockerfile + template config + README) — not wired into any service yet; Phase 2 task 2.5 picks it up.
