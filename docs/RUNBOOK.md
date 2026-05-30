# openclaw Operations Runbook

Operational procedures for the openclaw agent fabric. For architecture decisions see `docs/ADR/`. For phase implementation notes see `docs/phases/`.

---

## Vault

### Unseal Vault after a host reboot

Vault auto-seals on restart. Shamir-3-of-5 is required to unseal.

```bash
# Run the unseal script; enter three key shares from your password manager.
bash infra/scripts/02-unseal-vault.sh
```

Verify: `docker exec oc-vault vault status` → `Sealed: false`.

### Log in as openclaw-admin (AppRole)

```bash
bash core/scripts/oc-with-vault-creds.sh <command>
# or
bash core/scripts/run-with-vault-creds.sh   # for the FastAPI core process
```

The scripts prompt for the Role ID + Secret ID from your password manager and mint a 15-minute token. The root token was destroyed at bootstrap; AppRole is the only admin path.

### Skip re-entering the AppRole creds (GPG-encrypted file)

Re-typing the Role ID + Secret ID on every launch is tedious. `run-with-vault-creds.sh` will load them from a **GPG-symmetric-encrypted** file if one exists, prompting only for the GPG passphrase (which gpg-agent then caches for ~10 min):

```bash
# One-time: write the encrypted creds file (AES-256, passphrase-protected).
mkdir -p "$HOME/.config/openclaw"
gpg --symmetric --cipher-algo AES256 \
    -o "$HOME/.config/openclaw/admin.env.gpg" <<'EOF'
ROLE_ID=<role-id>
SECRET_ID=<secret-id>
EOF
chmod 600 "$HOME/.config/openclaw/admin.env.gpg"
```

After that, `run-with-vault-creds.sh` decrypts it automatically. Override the path with `OC_ADMIN_ENV_GPG=…`. If the file is absent the script falls back to interactive prompts.

**Why not a plaintext `.env`?** The Secret ID is a long-lived bearer credential for the `openclaw-admin` AppRole. A plaintext file (or pasting it into a chat/agent session) means anyone with read access — or any tool that ingests your files — holds admin. GPG-symmetric keeps it encrypted at rest; the passphrase lives only in your head + gpg-agent's short-lived cache. The decrypt path is also explicitly **denied** to Claude in `.claude/settings.local.json`.

> **TTY gotcha:** gpg needs a terminal for the passphrase prompt. If you see `Inappropriate ioctl for device`, export `GPG_TTY=$(tty)` first (the script does this for you, but it matters for manual `gpg -d`).

### Signing stopped after ~15 minutes? The token expired.

If audited requests stop appearing in Postgres after core has been up a while — and the logs show `vault.transit.sign_failed: permission denied / invalid token` — the 15-minute AppRole token has expired. The appender discards envelopes with empty signatures, silently, with no crash.

**Fix:** restart core via `run-with-vault-creds.sh` to mint a fresh token. A uvicorn `--reload` does **not** refresh it (it reloads code, not the launcher's exported token). The permanent fix is the vault-agent sidecar (task 2.5b), which auto-renews. See `docs/BOOTSTRAP_LESSONS.md § 17`.

---

## Postgres projection

### Rebuild the audit projection from immudb

The `openclaw.audit_entries` / `audit_conversations` / `audit_policy_decisions` / `audit_checkpoint` tables are a read-optimised projection of immudb. They are **not** the source of truth; immudb is. Rebuilding is safe and idempotent.

**When to rebuild:**
- After a failed migration or schema corruption
- After `oc audit verify` reports that Postgres diverges from immudb
- After restoring a Postgres backup to a known-good checkpoint

**Steps:**

```bash
# 1. Truncate all projection tables and reset the checkpoint.
psql -U rky-server -d postgres <<'SQL'
TRUNCATE openclaw.audit_entries, openclaw.audit_conversations,
         openclaw.audit_policy_decisions CASCADE;
UPDATE openclaw.audit_checkpoint SET last_ulid = NULL, last_ts = NULL,
       updated_at = now() WHERE id = 1;
SQL

# 2. Restart the audit projector process so it starts scanning from the
#    beginning of immudb (last_ulid = NULL means "from the start").
#    With oc-with-vault-creds.sh:
bash core/scripts/oc-with-vault-creds.sh python -m app.audit.projector

# 3. Monitor progress:
psql -U rky-server -d postgres -c \
  "SELECT COUNT(*) AS rows, MAX(ts) AS latest FROM openclaw.audit_entries"
```

The projector compares its running count against immudb. When it catches up the counts must match. See `docs/phases/phase-2.md § 2.9` for full details.

**Note:** `oc audit projection rebuild` (a single CLI command to automate this) is a Phase 2 deferred item; the manual procedure above is the current method.

---

## Attestations

### Publish a daily attestation manually

```bash
bash core/scripts/oc-with-vault-creds.sh \
  core/.venv/bin/oc attest publish --date YYYY-MM-DD
```

This reads `openclaw.audit_entries` for that date, computes a binary Merkle root over the raw envelopes, and commits `YYYY/MM/DD.json` to `rky-2023/openclaw-attestations` via the GitHub App.

### Verify an attestation offline

```bash
# Clone the attestations repo and run the standalone verifier.
git clone git@github.com:rky-2023/openclaw-attestations.git
python3 openclaw-attestations/verify.py openclaw-attestations/2026/05/25.json
```

The verifier has no openclaw dependencies — Python 3.11+ stdlib only.

---

## Smoke test

Run the Phase 2 spine smoke test to confirm infrastructure health:

```bash
# Infra checks only (no Vault credentials needed):
bash tests/phase-2-smoke.sh --infra-only

# Full run (Vault credentials required):
bash core/scripts/oc-with-vault-creds.sh bash tests/phase-2-smoke.sh
```

Expected output: T1–T7 PASS, T8–T13 SKIP (blocked on FastAPI audit middleware).

---

## NATS

### Check JetStream stream state

```bash
docker exec oc-nats nats stream ls
docker exec oc-nats nats stream info OC_AUDIT
```

Streams: `OC_AUDIT`, `OC_EVENTS`, `OC_CALENDAR`, `OC_GITHUB`, `OC_ANDROID_PUSH`.

### Drain and purge a stream (destructive)

```bash
docker exec oc-nats nats stream purge OC_AUDIT --force
```

**This deletes all messages in the stream.** Use only in dev/recovery contexts.

---

## immudb

### Check database state

```bash
# Connect as immudb admin (password in Vault kv/openclaw/immudb/admin):
docker exec -it oc-immudb immuadmin login immudb
```

### What to do if immudb is unreachable

1. Check the container: `docker inspect oc-immudb | grep Status`
2. Check the LUKS volume: `mountpoint /mnt/openclaw/immudb`
3. If the volume is unmounted: `sudo cryptsetup luksOpen /var/lib/openclaw/luks/immudb.img oc-immudb-luks && sudo mount /dev/mapper/oc-immudb-luks /mnt/openclaw/immudb`
4. Restart the container: `docker start oc-immudb`

The audit appender will resume from where it left off — NATS JetStream retains unacknowledged messages.
