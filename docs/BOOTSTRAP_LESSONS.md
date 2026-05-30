# Bootstrap lessons learned

Things that bit us during Phase 1 + Phase 2 bootstrap (2026-05-23). Captured here so future-you, or a fresh deploy of openclaw on different hardware, doesn't re-discover them. Each entry: the symptom, the root cause, and where the fix lives now.

---

## 1. Snap-installed Docker is filesystem-confined

**Symptom:**
```
docker: Error response from daemon: failed to mount volume:
mount /var/lib/openclaw/vault:...: no such file or directory
```
even though the path clearly exists on the host.

**Cause:** snap-installed Docker (the default `apt install docker` on Ubuntu) runs under snap confinement. Its filesystem view is restricted to a small set of host paths:
- `/home` (with `snap connect docker:home`)
- `/mnt`, `/media` (via `docker:removable-media`, connected by default)
- A few others

`/var/lib/openclaw/` is **not** visible. Neither is the host's `/tmp/` (snap has its own private `/tmp` namespace).

**Fix in the repo:**
- All bind-mount targets moved to `/mnt/openclaw/<service>/` (see ADR-001 R4 commentary and `infra/docker-compose.openclaw.yml`).
- LUKS image files stay at `/var/lib/openclaw/luks/<service>.img` (Docker never reads those).
- Bootstrap scripts that move files in/out of containers use `docker exec -i ... cat > /tmp/file` (inside the container's own `/tmp`) instead of `docker cp` from the host's `/tmp` — see `infra/scripts/04-vault-bootstrap.sh` intermediate CA generation, and `infra/scripts/05-vault-tls-listener.sh` cert placement.

**If you're not on snap Docker** (e.g., `apt install docker.io` or upstream Docker), `/var/lib/` paths work fine. But the `/mnt/` paths still work, so no change needed.

---

## 2. Container-uid ownership of bind-mount targets

**Symptom:**
- Vault: `Error initializing storage of type raft: ... permission denied`
- immudb: container exits 2 right after printing the banner, no error in logs
- NATS: similar early exit

**Cause:** the LUKS bootstrap script (`00-create-luks-volumes.sh`) creates each mount target as `root:root` mode `0700`. Inside their containers, services run as non-root users:
- Vault: `vault` (uid 100, gid 1000)
- immudb: `immudb` (uid 3322, gid 3322)
- NATS: `nats` (uid 1000, gid 1000)
- MinIO: varies (set via env)

A bind-mount target's underlying filesystem perms apply inside the container too. So a `root:root mode 700` host directory becomes `root:root mode 700` from the container's perspective. Non-root container processes can't write.

**Fix:** every bring-up script pre-chowns the bind target to the correct container uid **on the host** *before* running `docker compose up`. See:
- `infra/scripts/01-bring-up-vault.sh` (chown 100:1000)
- `infra/scripts/07-immudb-bootstrap.sh` (chown 3322:3322)
- `infra/scripts/08-nats-bootstrap.sh` (chown 1000:1000)

A previous (broken) approach tried `docker exec -u 0 oc-<svc> chown ...` *after* compose-up — by then the container was crash-looping and `docker exec` failed.

---

## 3. Distroless container images: no `sh`, no `chown`, no coreutils

**Symptom:**
```
OCI runtime exec failed: exec failed: unable to start container process:
exec: "sh": executable file not found in $PATH
```

**Cause:** modern security-focused images (codenotary/immudb, hashicorp/vault non-debug variants, etc.) ship only the service binary. No `/bin/sh`, no `chown`, no `cat`, no shell wrappers.

**Fix in the repo:**
- Bootstrap scripts call binaries **directly** via `docker exec` rather than wrapping in `sh -c '...'`.
- File round-trips use `docker exec -i ... <binary> < input.file` patterns.
- Readiness probes use `docker inspect <container> --format '{{.State.Status}}'` instead of `docker exec ... sh -c 'check'`.

---

## 4. `immuadmin` password input requires a TTY inside the container

**Symptom:** scripted login fails with `invalid user name or password` even with correct credentials. Manual interactive login from `docker exec -it` works.

**Cause:** `immuadmin login` reads passwords via `getpass()`, which reads from `/dev/tty` in the *container's* namespace. With `docker exec -i` (no `-t`), the container has no controlling TTY and the password isn't received.

`immuadmin` also has no `--password` flag, no `IMMUADMIN_PASSWORD` env var, and no stdin-piping mode — confirmed via `immuadmin login --help`.

**Fix:** drive every `immuadmin` invocation through `expect` (one-time `sudo apt install -y expect`), which allocates a pty pair on both sides. `docker exec -it` allocates the container-side TTY. See `infra/scripts/07-immudb-bootstrap.sh` — `immu_change_pw_lenient` and friends.

---

## 5. immudb 1.9.5 force-changes admin password on first login

**Symptom:** the first login with the default `immudb`/`immudb` credentials triggers an extra prompt sequence: *Choose a password* → *(Do you want to continue with your password instead? [Y/n])* → *Confirm password*.

**Cause:** security feature added in immudb 1.9 — operators can no longer leave the default admin password in place. The forced-change happens once, then never again.

**Fix:** the `immu_login` helper in `07-immudb-bootstrap.sh` has a `force_change="yes"` branch that handles this prompt chain inline. On first-run detection (Vault empty for `kv/openclaw/immudb/admin`), it sends the same password as both old and new, leaving the actual password rotation for a subsequent deliberate step.

---

## 6. immudb permission names: `read` not `readonly`

**Symptom:**
```
Permission readonly not recognized: allowed permissions are
read, readwrite, admin
```

**Cause:** version-specific CLI vocabulary. In immudb 1.9.5, the valid permission names for `immuadmin user create <user> <perm> <db>` are exactly `read`, `readwrite`, `admin`. Not `readonly`.

**Fix:** the script uses `read` (see `ensure_user projector read ...`).

---

## 7. `nats:2.10-alpine` server image does NOT include the `nats` client CLI

**Symptom:**
```
docker exec oc-nats nats stream add ... → "exec: nats: executable file not found"
```

**Cause:** the official NATS server image bundles only `nats-server`. The user-facing CLI tools (`nats`, `natscli`) live in a separate companion image: `natsio/nats-box`.

**Fix:** stream-management commands in `infra/scripts/08-nats-bootstrap.sh` run via:
```
docker run --rm --network=oc-internal natsio/nats-box \
  nats -s nats://oc-nats:4222 stream add <name> ...
```

`natsio/nats-box` is ~30 MB, pulled on first use.

---

## 8. `vault kv put PATH -` is NOT stdin

**Symptom:**
```
Failed to parse K=V data: invalid key/value pair "-":
invalid character 'p' looking for beginning of value
```

**Cause:** `vault kv put` treats a bare `-` as a positional K=V argument. To actually read from stdin you need `@-`, AND the stdin content must be JSON (`{"key":"value"}`), not the `key=value` line format used elsewhere.

**Fix:** scripts pass values as direct `key=value` positional args (e.g., `vault kv put kv/foo password="$pw"`). The password is briefly visible in `/proc/<pid>/cmdline` — acceptable for a single-operator host where root already sees everything, and the alternative requires JSON formatting + shell-escaping that's noise for these one-shot bootstraps.

---

## 9. `docker compose restart <svc>` does NOT pick up env-var changes

**Symptom:** updated `docker-compose.yml` (e.g., changed `VAULT_ADDR=http://...` to `https://...`); container restarted; new env not applied; cli inside the container still using the old value; HTTP→HTTPS mismatch errors.

**Cause:** `restart` reboots the existing container in place. Env vars from compose are baked in at container *creation*, not on restart. To pick up env changes you need to **recreate**:
```
docker compose up -d --force-recreate <svc>
```
or `docker compose down <svc> && docker compose up -d <svc>`.

**Fix:** `infra/scripts/05-vault-tls-listener.sh` and similar use `--force-recreate`. This is a general docker-compose footgun, not specific to openclaw.

---

## 10. Vault default healthcheck exits 2 when sealed → docker marks "unhealthy"

**Symptom:** after `docker compose up -d vault` (which always leaves Vault sealed by design), `docker ps` reports `oc-vault   Up X minutes (unhealthy)`. Operator panics thinking something's wrong.

**Cause:** the compose healthcheck was `vault status`, which exits:
- 0 if unsealed
- 1 on real error
- 2 if sealed/uninitialized

Docker maps any non-zero exit to "unhealthy".

**Fix:** the healthcheck now accepts exit-2 as healthy:
```yaml
test: ["CMD-SHELL", "vault status > /dev/null 2>&1 || [ $? -eq 2 ]"]
```
Sealed Vault is the expected post-restart state (Shamir-only unseal is hands-on per ADR-001 R1) — not a problem.

---

## 11. immudb-py `login()` defaults to `defaultdb` → PERMISSION_DENIED

**Symptom:** core connects to immudb as the `appender` (or `projector`) user and immediately fails with:
```
PERMISSION_DENIED: Logged in user does not have permission on this database
```
even though the user clearly has `readwrite`/`read` on `openclaw_audit`.

**Cause:** immudb-py's `client.login(user, password)` logs into `database=b"defaultdb"` unless told otherwise. The `appender`/`projector` users are granted permission **only** on `openclaw_audit`, so the login itself (against `defaultdb`) is denied before `useDatabase()` is ever reached.

**Fix:** pass the target DB to `login()` directly, then `useDatabase()`. See `core/app/audit/immudb_writer.py` `_do_connect()`:
```python
db_bytes = db.encode() if isinstance(db, str) else db
c = ImmudbClient(f"{host}:{port}")
c.login(user, password, database=db_bytes)
c.useDatabase(db_bytes)
```

---

## 12. immudb `user create <user> <perm> <db>` does NOT persist the per-DB grant

**Symptom:** `07-immudb-bootstrap.sh` creates the `appender`/`projector` users successfully, but core still gets `PERMISSION_DENIED` on `openclaw_audit` — even after the `login(database=…)` fix above.

**Cause:** in immudb 1.9.5, the `<perm> <db>` arguments to `immuadmin user create` set the password/initial state but do **not** durably grant the per-database permission. The grant has to be issued as a separate, explicit `user permission grant` call.

**Fix:** `07-immudb-bootstrap.sh` now calls an `immu_grant` helper after every create/rotate:
```
docker exec oc-immudb immuadmin user permission grant <user> <perm> openclaw_audit
```
Look for the `✓ <perm> permission granted to '<user>'` log lines on a clean bootstrap.

---

## 13. Host-MVP path must use loopback URLs, not Docker service names

**Symptom:**
```
socket.gaierror: [Errno -3] Temporary failure in name resolution   (nats:4222)
```
when launching core on the host (outside the compose network) via `run-with-vault-creds.sh`.

**Cause:** the config defaults (`OC_NATS_URL=nats://nats:4222`, `OC_IMMUDB_HOST=immudb`, `OC_VAULT_ADDR=http://vault:…`) are the **in-compose** Docker DNS names. A host process can't resolve them — and the services weren't published to the host at all.

**Fix (two parts):**
- `infra/docker-compose.openclaw.yml` publishes immudb and NATS to loopback only: `127.0.0.1:3322:3322` and `127.0.0.1:4222:4222` (Vault's TLS listener was already on `127.0.0.1:8200`).
- `core/scripts/run-with-vault-creds.sh` exports loopback overrides before exec'ing uvicorn: `OC_NATS_URL=nats://127.0.0.1:4222`, `OC_VAULT_ADDR=https://127.0.0.1:8200`.

The vault-agent sidecar (task 2.5b) runs core back inside the compose network where the service names resolve, so this is host-MVP-only.

---

## 14. Vault's host listener is HTTPS — core's client must verify accordingly

**Symptom:** transit signing fails; plain-HTTP requests to `127.0.0.1:8200` get `HTTP 400`. Or, after switching to `https://`, an SSL CERT_VERIFY failure.

**Cause:** the host-published Vault listener (task 05) is TLS, with a cert signed by the **internal** CA — not in the system trust store. The hvac client either talks plain HTTP (400) or verifies against a CA it doesn't have (verify failure).

**Fix:** `core/app/config.py` adds `vault_cacert` (CA bundle path, takes precedence) and `vault_verify` (bool toggle). `signer._get_vault_client()` passes `verify = settings.vault_cacert or settings.vault_verify` to `hvac.Client`. The host MVP exports `OC_VAULT_VERIFY=false` (acceptable: same-host loopback, no MITM surface); production points `OC_VAULT_CACERT` at the internal CA bundle.

---

## 15. `sig_service` must NOT cover `prev_hash`

**Symptom:** every projected entry shows `sig_service_valid=false` (`svc_ok=1/10`) while `sig_appender_valid=true`.

**Cause:** the originating service signs the envelope **before** the appender assigns its chain position (`prev_hash`). The appender then mutates `prev_hash` and signs its own slot. If `sig_service` covered `prev_hash`, that later mutation invalidates it.

**Fix:** `envelope.to_canonical_bytes(exclude_prev_hash=…)` drops `prev_hash` from the canonical body when `True`. `signer.sign_envelope`/`verify_envelope` pass `exclude_prev_hash=(slot == "service")`, so the service signature covers everything except `prev_hash`; the appender signature and the chain hash still cover it. (Entries written before this fix keep `svc_ok=1` forever — they're immutable in the WORM ledger; only test traffic was affected.)

---

## 16. `python-ulid` is not `ulid-py` — different API

**Symptom:** `AttributeError: module 'ulid' has no attribute 'new'` → HTTP 500 on every audited endpoint.

**Cause:** two PyPI packages both import as `ulid`. The installed one is **`python-ulid`** (3.1.0), whose API is `ulid.ULID()` / `ulid.ULID.from_datetime(dt)`. The `ulid.new()` form belongs to the *other* package, `ulid-py`.

**Fix:** `envelope.AuditEnvelope.new()` uses `str(ulid.ULID.from_datetime(now))`, which also keeps the ULID's encoded timestamp consistent with the envelope's `ts` field.

---

## 17. Vault AppRole token has a 15-minute TTL — signing silently stops after expiry

**Symptom:** core runs fine for ~15 minutes, then every envelope starts logging `vault.transit.sign_failed: permission denied / invalid token`, carries an empty `sig_service`, and the appender **discards** it (ACK-without-write). Nothing reaches Postgres; no crash.

**Cause:** `run-with-vault-creds.sh` does an AppRole login whose token TTL is 15 min. Once it expires, transit/sign is denied. uvicorn `--reload` reloads *code* but not the launching script's exported token, so a code reload does **not** refresh it — only a full process restart (re-running the wrapper) does.

**Fix / workaround:** restart core via `run-with-vault-creds.sh` to mint a fresh token. The permanent fix is the **vault-agent sidecar (task 2.5b)**, which auto-renews the token and renders it to a file core re-reads. Until then, treat "entries stopped appearing after a while" as "the token expired — restart."

---

## 18. Appender-side redaction widened the sig_service canonical basis

**Symptom:** after task 2.7 shipped, the ~22 pre-2.7 test entries in immudb read `sig_service_valid=false` in the projection, even though they verified fine before.

**Cause (by design, not a regression):** ADR-003 D6 redaction runs in the *appender* — it rewrites `redacted_payload`, fills `encrypted_blobs`, and stamps `policy.redaction_version` *after* the originating service has already signed. So `sig_service` must NOT cover those fields (same reasoning as the earlier `prev_hash` exclusion, §15). `envelope.to_canonical_bytes(slot="service")` now excludes `{prev_hash, redacted_payload, encrypted_blobs, policy}`; the appender signature + chain hash cover the full final form. Entries signed under the *old* (narrower) service basis no longer re-verify under the new one.

**Fix / expectation:** this is a one-time ledger-format change. Pre-2.7 entries are immutable test traffic — the projector flags them and that's correct. All entries written from 2.7 onward verify on both slots. Don't "fix" the old entries (you can't — WORM); just re-verify with fresh traffic. The slot→excluded-fields mapping lives in `_APPENDER_OWNED_FIELDS` in `core/app/audit/envelope.py`.

---

## Meta: how to add to this list

Anytime you hit something non-obvious during ops:
1. Add a new numbered section here (symptom → cause → fix-location).
2. Keep entries short — link out to the actual script / ADR for detail.
3. The goal is "future-you can search this doc by symptom and find the answer in 30 seconds."

This file is **operations-facing**. ADRs are *what and why*; runbooks are *how, in what order*; this is *what went wrong and how we worked around it*.
