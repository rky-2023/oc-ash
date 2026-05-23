# `infra/scripts/`

Helper scripts for executing the Phase 1 + Phase 2 runbooks. Each is numbered to indicate execution order.

Scripts are **not** invoked automatically. They are interactive (passphrases, Shamir shares) and require operator presence. Don't try to wrap them in a single "deploy" command — the design is to make destructive / one-way ceremonies visible.

## Index

| # | Script | Phase task | Purpose |
|---|---|---|---|
| 00 | `00-create-luks-volumes.sh` | 1.1 + 2.2 | LUKS-on-file volumes for vault / immudb / nats / minio, mounted under `/var/lib/openclaw/<name>` |
| 01 | `01-bring-up-vault.sh` | 1.2 | `docker compose up vault` with pre-flight checks |
| 02 | `02-init-vault.sh` | 1.3 part 1 | One-time `vault operator init -key-shares=5 -key-threshold=3` ceremony |
| 03 | `03-unseal-vault.sh` | 1.3 part 2 | Interactive 3-share unseal (also used on every reboot) |
| 04 | `04-vault-bootstrap.sh` | 1.4 + 1.5 + 1.6 + most of 1.7 | One-time post-unseal ceremony: enables audit log, mounts kv-v2 + PKI (root + intermediate + server/client roles) + transit signing keys + AppRole auth, creates the `openclaw-admin` AppRole, then destroys the root token after the operator captures the AppRole credentials. Idempotent until the gated DESTROY step. |
| 05 | `05-vault-tls-listener.sh` | 1.7 remainder | Issues a 30-day Vault listener cert from `pki_int/roles/vault-listener`, places it under `/vault/data/tls/`, publishes the CA cert to `/mnt/openclaw/shared/ca.crt`, restarts Vault with TLS-enabled `server.hcl`. Operator re-unseals afterwards via 03. |
| 06 | `06-apply-postgres-schema.sh` | 2.1 | Applies `infra/postgres/openclaw-schema.sql` to the existing Ashboard Postgres (auto-detects container name). Creates the `openclaw` schema + `openclaw_app` role + `openclaw.lookup` table with seed rows. Rotates the `openclaw_app` password and stores in Vault `kv/openclaw/postgres/app`. |
| 07 | `07-immudb-bootstrap.sh` | 2.3 | Brings up `oc-immudb`. Rotates the default admin password, creates `openclaw_audit` database, creates `appender` (R/W) and `projector` (R-only) users. All passwords random + stored in Vault `kv/openclaw/immudb/{admin,appender,projector}`. |
| 08 | `08-nats-bootstrap.sh` | 2.4 | Brings up `oc-nats`. Creates the five openclaw streams (OC_EVENT, OC_A2A, OC_MCP, OC_NOTIFY, OC_HEALTH) from `infra/nats/streams.yaml`. |

One-time migration script (only needed if you ran `00-create-luks-volumes.sh` before the `/mnt`-paths fix):

| Script | Purpose |
|---|---|
| `migrate-mount-paths-to-mnt.sh` | Unmount volumes from `/var/lib/openclaw/<svc>` and remount at `/mnt/openclaw/<svc>`. LUKS images stay where they are; no data loss. Run once, then remove. |

Future scripts:
- (Phase 1 task 1.8 ended up in `core/scripts/` rather than `infra/scripts/` because it's per-deployment, not infra-bringup.)
- Service-specific bootstrap scripts (audit-appender, audit-projector, attestation-publisher) land alongside the services they configure in Phase 2 tasks 2.8 / 2.9 / 2.13.

## Running them

All scripts must run as **root** (or via `sudo`). They check for required tools at startup and exit cleanly if anything is missing.

```sh
cd /home/asher/openclaw/infra/scripts

# Phase 1 task 1.1
sudo ./00-create-luks-volumes.sh

# Phase 1 task 1.2
sudo ./01-bring-up-vault.sh

# Phase 1 task 1.3 (one-time)
sudo ./02-init-vault.sh
# (Distribute Shamir shares per phase-1.md cost-free distribution table)

# Phase 1 task 1.3 (every reboot)
sudo ./03-unseal-vault.sh
```

## Idempotency

Each script is idempotent where it can be safely so:
- `00-create-luks-volumes.sh` skips volumes already mounted.
- `01-bring-up-vault.sh` runs `docker compose up -d` which is naturally idempotent.
- `02-init-vault.sh` **refuses to run** if Vault is already initialized — protects existing data.
- `03-unseal-vault.sh` reports "already unsealed" and exits cleanly if Vault is already open.
- `04-vault-bootstrap.sh` skips re-enabling already-mounted engines, already-created keys, etc. Re-runnable until the operator types DESTROY at the final root-revoke step.

## Safety properties

- **No script writes any secret to disk.** Shamir shares are printed to the terminal once by `02-init-vault.sh`; you record them; nothing persists.
- **No script keeps any secret in env or argv** of a sub-process. Passphrases and shares are piped via stdin to `cryptsetup` / `vault` to avoid `/proc/<pid>/cmdline` exposure.
- **All destructive operations require operator confirmation.** `02-init-vault.sh` requires typing `READY`. `04-vault-bootstrap.sh` requires `CAPTURED` after AppRole creation and `DESTROY` before root-token revocation. `migrate-mount-paths-to-mnt.sh` requires `MIGRATE`.

## When something goes wrong

See [`docs/phases/phase-1.md`](../../docs/phases/phase-1.md) "Rollback / panic procedures" for the canonical guidance. Briefly:

- If a LUKS volume can't be unlocked: check `/var/log/syslog` for `cryptsetup` errors; verify you have the right passphrase; the LUKS header has 8 key slots so you can add a new key with the recovery passphrase from a working machine via `cryptsetup luksAddKey`.
- If Vault refuses to unseal with a share: the share is wrong; try the next one. After 3 wrong shares Vault doesn't lock out (Shamir is stateless), so retries are safe.
- If Vault is sealed and you've lost some Shamir shares: as long as you have ≥ 3 of 5, you can unseal AND rotate the key shares with `vault operator rekey`.
