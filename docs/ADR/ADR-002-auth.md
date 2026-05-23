# ADR-002: Authentication & identity — WebAuthn + YubiKey, mTLS service mesh, short-lived JWTs

- **Status:** Accepted
- **Date:** 2026-05-23
- **Deciders:** rky
- **Supersedes:** —
- **Superseded by:** —
- **Related:** ADR-001 (architecture, esp. R1 — Shamir-only Vault unseal), ADR-003 (audit ledger), `docs/THREAT_MODEL.md` (tiers T1–T5)

---

## Context

openclaw is single-user, hardware-key-gated, with a fully-sandboxed agent fabric. The threat model (`docs/THREAT_MODEL.md`) names five adversary tiers and explicitly resists T1–T4. Identity is the foundation: every other security control assumes the caller's identity can be trusted. If identity is soft, the rest is theatre.

This ADR fixes:

1. How **rky** authenticates to openclaw (user identity).
2. How services authenticate to each other (service identity).
3. How agents and MCP servers obtain capabilities for specific calls (request identity).
4. How the Android device pairs and re-authenticates.
5. How the `openclaw-bot` GitHub App authenticates outbound to GitHub.
6. How keys rotate; how break-glass works when a key is lost.

ADR-001 R1 already locked Vault unseal as **manual Shamir-only**, so any "auto-recovery" path here must respect that — break-glass requires hands on Shamir shares.

---

## Decision

### D1. User authentication: WebAuthn / FIDO2 with platform-bound resident keys, user-verification required.

- WebAuthn ceremony parameters:
  - `authenticatorSelection.residentKey = "required"` (passwordless username-less flow).
  - `userVerification = "required"` (PIN or biometric on the authenticator, in addition to user presence).
  - `attestation = "direct"` — we record attestation to know we're talking to a real YubiKey, not a software emulator.
  - `pubKeyCredParams = [{type: "public-key", alg: -8}]` (Ed25519 only).
- Relying Party ID: `oc.<tailnet-magic-domain>` (resolves only inside the tailnet — see ADR-001 R4).
- Library: `webauthn` (Duo's Python lib) on the server side; `@simplewebauthn/browser` on the front end.

### D2. Hardware: two **YubiKey 5C NFC** devices.

- **Primary** lives on rky's person.
- **Spare** lives in a fireproof safe.
- Both are enrolled at bootstrap. Either can revoke the other.
- NFC is required because the Android app uses the same key for re-pairing via NFC tap (see D9).
- FIPS variants not required for this threat model; FIPS adds cost without raising the achievable posture on this host.

### D3. Enrollment policy.

- **At bootstrap:** both YubiKeys must be registered before any other openclaw component starts. The bootstrap script refuses to proceed with only one key.
- **YubiKey FIDO2 PIN:** required, set to a high-entropy PIN known only to rky and written *only* on the same paper backup that holds Shamir shares. PIN lockout: 8 wrong attempts → device-level wipe of the FIDO2 credential.
- **Re-enrollment** (e.g., adding a third key, replacing a lost one) requires authentication with a *surviving* enrolled key.

### D4. Fallbacks: none. Recovery is via Shamir, not by lowering identity strength.

- **No** password fallback.
- **No** TOTP, SMS, or email-based recovery.
- **No** "I lost my key, mail me a link" path.
- **The only recovery path** for total YubiKey loss is: provide 3-of-5 Shamir shares to unseal Vault → extract the WebAuthn credential store admin key → register a freshly-purchased YubiKey from the surviving credentials. This is intentionally inconvenient and is documented in `docs/RUNBOOK.md` (Phase 12).

### D5. Session model: 5-minute access JWT + 24-hour refresh JWT, refresh requires YubiKey touch.

- **Access JWT:** 300 s TTL. Carries `sub`, `iat`, `exp`, `jti`, and a `capabilities` claim listing capabilities granted to this session. Signed by core with Vault transit (Ed25519 key, rotated monthly).
- **Refresh JWT:** 86_400 s TTL. Stored only as an `HttpOnly; Secure; SameSite=Strict` cookie. Each use:
  1. Verifies `jti` is not in the Redis deny-list.
  2. Triggers a WebAuthn `userVerification = "required"` challenge — i.e., a YubiKey touch. Refresh without touch is rejected.
  3. Issues a new access JWT *and* rotates the refresh JWT (single-use refresh tokens).
- **Revocation:** any session's `jti` can be added to the Redis deny-list (`auth.jti.revoked` set) with a TTL matching the JWT's `exp`. The `oc auth revoke` CLI exposes this.

### D6. Service-to-service authentication: mTLS, certs issued by Vault's internal PKI, 24-hour TTL.

- Internal CA root lives in Vault's PKI v2 engine, autounseal not applicable (we hand-unseal per ADR-001 R1).
- Every service runs `vault-agent` as a sidecar (or in the same image as a process under tini); vault-agent rotates the cert every 24 hours and writes it to a tmpfs path.
- Cert SAN format: **URI SAN with `spiffe://openclaw.local/<namespace>/<service>` scheme.** This is SPIFFE-style without committing to SPIRE; if we ever adopt SPIRE we can do so without changing the identity format.
- TLS minimum version: 1.3. Cipher suite list pinned in Caddy and in the FastAPI/uvicorn config.

### D7. Per-MCP-call service JWTs: 15-minute TTL, minted by core.

- When an agent invokes the MCP proxy, the proxy mints a short-lived JWT scoped to that single tool call (target MCP, tool name, args hash, `conv_id`, `exp`).
- The MCP server validates the JWT against core's Vault-transit verify key on every call. This means a leaked long-lived MCP credential is impossible — there is no long-lived MCP credential.
- This JWT is also written to the audit envelope (ADR-003) so the request-identity provenance is preserved end-to-end.

### D8. Vault access from services.

- Every service has its own AppRole in Vault. The role-id is baked into the image (non-secret); the secret-id is injected at runtime by an `init` container that itself authenticates with a token derived from the host's TPM (where present) or from a one-time bootstrap token (otherwise).
- Each AppRole's policy is the **minimum** set of paths required (read on `kv/<service>/*`, possibly sign on `transit/sign/<service>`). No service has cross-namespace access.

### D9. Android device pairing.

1. rky logs into the openclaw web UI on a laptop (laptop is on the tailnet, WebAuthn ceremony with the primary YubiKey completes).
2. Web UI shows a QR code containing: a one-time pairing token (90 s TTL), the relying-party ID, and a fingerprint of the server's mTLS cert.
3. Android app scans QR. App:
   - Submits a **Play Integrity** attestation to the server. The server rejects rooted devices, emulators, devices with verdicts other than `MEETS_DEVICE_INTEGRITY` and `MEETS_STRONG_INTEGRITY`.
   - Generates a Curve25519 keypair inside Android Keystore (StrongBox preferred). Private key never extractable.
   - Sends the public key to the server.
4. Server enrolls the device in `auth.devices`. The pairing token is consumed.
5. Server returns: a long-lived device JWT (30 days, refresh-only — cannot access user APIs directly), plus the server's public Curve25519 key used for notification payload encryption (see ADR-001 D7).
6. The device JWT is rotated every 30 days; rotation requires the YubiKey-bound laptop session to re-authorize. NFC tap of the YubiKey to the phone is the alternative path for rotation when the laptop is unavailable.

### D10. GitHub App `openclaw-bot` authentication.

- App private key (PEM, RSA-2048 — GitHub still requires RSA) is stored in Vault's `transit` engine as an imported key. Never readable; sign-only.
- Per-call flow: core requests a JWT signed by the App key from Vault transit (10-minute TTL per GitHub's spec) → exchanges that JWT for an installation access token from GitHub (1-hour TTL) → uses the installation token for the actual API call.
- Installation access tokens are scoped to the minimum permissions per repo (contents:write, pull-requests:write, checks:write on `oc-ash`; contents:write on `openclaw-attestations`). No other permissions are requested.
- The bot is **never** a CODEOWNER and **never** has main-branch push (branch protection enforces).

### D11. Break-glass procedures.

- **One YubiKey lost / stolen:** Authenticate with the surviving key → `oc auth revoke-key <credential-id>` → purchase a new YubiKey → `oc auth enroll-key`. The lost key's `credential-id` is added to a permanent deny-list.
- **Both YubiKeys lost:** Hands-on cold-unseal Vault with 3-of-5 Shamir shares → extract the WebAuthn admin credential → register a freshly-purchased YubiKey via the bootstrap re-run path → re-bootstrap the spare. This requires physical access to the server and at least three share-holders cooperating.
- **YubiKey PIN forgotten:** Same as "lost" — the YubiKey itself locks out after 8 wrong PINs and cannot be reset without device wipe.
- **Vault sealed, no Shamir available:** Total lockout. By design. This is the rubber-hose-resistance backstop: even rky cannot bypass it under coercion if the shares are geographically separated.

### D12. Rotation cadence.

| Material | Cadence | Trigger |
|---|---|---|
| WebAuthn credentials | Event-driven (lost, suspected compromise) | Manual `oc auth revoke-key`+`enroll-key` |
| mTLS internal CA root | Annual | Scheduled ceremony (calendared) |
| mTLS leaf certs | 24 h | `vault-agent` automatic |
| Service signing keys (Vault transit Ed25519) | Monthly | Vault transit auto-rotate |
| GitHub App private key | Quarterly | Manual: generate on GitHub UI, import to Vault transit, retire old version after one-week overlap |
| Android device JWT | 30 days | YubiKey-gated refresh |
| Per-call MCP JWT | Per-call (≤15 min) | Each tool invocation |
| Refresh JWT | Per-use (single-use, rotated every refresh) | Each refresh |

---

## Consequences

### Positive

- T1–T3 are fully resisted. T4 is meaningfully harder: a root-on-box attacker can read mTLS material from tmpfs but cannot forge a YubiKey touch, so any *new* session past the next refresh window requires the operator's physical key.
- No long-lived credentials anywhere in the data path. The longest-lived secrets are the YubiKey credentials themselves (years), the GitHub App key (quarter), and the mTLS root (year).
- The "auth admin" surface is small enough to fully audit: WebAuthn enroll/revoke + AppRole policies + transit key rotation. Every change is in `docs/ADR/` or in the audit log.
- Per-call MCP JWTs eliminate "stolen API key in container env" as a class of vulnerability — there is no such key.

### Negative

- Reboot is hands-on (Shamir) **and** every fresh login is hands-on (YubiKey touch). Convenience cost is real. We accept it.
- vault-agent sidecars add some operational complexity (and a process-supervision burden) compared to "secrets in env at startup".
- YubiKey 5C NFC stock can be flaky from official channels; budget 2–6 weeks lead time.
- If GitHub deprecates RSA-2048 for App keys in favor of EdDSA, we have to migrate. Likely on the order of years.

### Neutral

- We are committing to webauthn-as-only-auth and to Vault as the universal CA + key custodian. Both decisions are reversible in principle but expensive to reverse in practice.

---

## Alternatives considered

### A1. Passkeys synced via iCloud / Google Password Manager.

Rejected. Synced passkeys are bound to a cloud account rather than a device, which weakens the "physical key required" property and expands the trust boundary to cover Apple/Google account security. Device-bound resident keys on YubiKeys are strictly stronger for this threat model.

### A2. Single YubiKey + TOTP / recovery codes as backup.

Rejected. TOTP and recovery codes are something-you-know; the threat model resists T2 (phished session token), which would extend to "phished recovery codes". The Shamir + spare-YubiKey path is harder to phish because it requires physical share holders.

### A3. SSH keys + commit signing only (no WebAuthn web UI).

Rejected. An SSH key has no user-presence proof — a compromised laptop with the key unlocked acts as the operator. WebAuthn forces a fresh touch per session.

### A4. OAuth (e.g., Google as IdP) for the openclaw web UI.

Rejected. Adds Google to the trust boundary for *local* authentication, which is an unnecessary expansion. Google OAuth is still used for the Google Calendar / Gmail integrations themselves — that's unavoidable — but adding it to *internal* auth multiplies the consequences of a Google account compromise.

### A5. SPIRE for service identity.

Considered, deferred. SPIRE is the right answer at multi-host / multi-tenant scale. At single-host scale, Vault PKI with a SPIFFE-shaped URI SAN gives us 90% of the value with 20% of the operational weight. If openclaw ever crosses hosts, we adopt SPIRE without changing the identity format.

### A6. mTLS with self-signed certs (no Vault PKI).

Rejected. Self-signed leaf certs work for a small system, but rotation becomes a manual ritual, and adding a new service requires touching every other service's trust store. Vault PKI's 24-hour automatic rotation is the cheap path to a strong posture.

### A7. Long-lived MCP API keys with rate limits.

Rejected. The "stolen API key in container env" failure mode is the most common cause of post-mortems in self-hosted setups. Eliminating it by minting per-call JWTs is worth the small extra latency.

### A8. Biometric (FaceID / fingerprint) as primary user auth.

Rejected. Biometric prompts on a laptop browser are tied to the platform's biometric subsystem, not to a portable device, so they don't work across rky's devices and don't survive device loss the way a YubiKey does. Biometric *as a YubiKey PIN replacement* is supported by the FIDO2 spec but is a per-device convenience, not a primary mechanism.

---

## Open questions

- Whether to add a third YubiKey (offsite second spare) once the system is in steady state. Marginal cost low; marginal security gain real but small.
- Whether to participate in a YubiKey firmware update channel — current firmware has no known critical issues, but a published vulnerability would force a fast-rotation playbook we haven't drafted.
- The TPM-vs-bootstrap-token decision for AppRole secret-id delivery (D8) depends on whether the host has a usable TPM 2.0; to be validated during Phase 1.

---

## Change log

- **2026-05-23 (v1)** — Accepted as drafted.
