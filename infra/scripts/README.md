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

Future scripts (PR #4):
- `04-vault-bootstrap.sh` — Phase 1 tasks 1.4 (audit log) + 1.5 (secret engines) + 1.6 (admin AppRole + destroy root token)
- `05-issue-internal-ca.sh` — Phase 1 task 1.7
- `06-webauthn-rp.sh` — Phase 1 task 1.8 (stub server + relying-party config)

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

## Safety properties

- **No script writes any secret to disk.** Shamir shares are printed to the terminal once by `02-init-vault.sh`; you record them; nothing persists.
- **No script keeps any secret in env or argv** of a sub-process. Passphrases and shares are piped via stdin to `cryptsetup` / `vault` to avoid `/proc/<pid>/cmdline` exposure.
- **All destructive operations require operator confirmation.** `02-init-vault.sh` requires typing the word `READY` before proceeding.

## When something goes wrong

See [`docs/phases/phase-1.md`](../../docs/phases/phase-1.md) "Rollback / panic procedures" for the canonical guidance. Briefly:

- If a LUKS volume can't be unlocked: check `/var/log/syslog` for `cryptsetup` errors; verify you have the right passphrase; the LUKS header has 8 key slots so you can add a new key with the recovery passphrase from a working machine via `cryptsetup luksAddKey`.
- If Vault refuses to unseal with a share: the share is wrong; try the next one. After 3 wrong shares Vault doesn't lock out (Shamir is stateless), so retries are safe.
- If Vault is sealed and you've lost some Shamir shares: as long as you have ≥ 3 of 5, you can unseal AND rotate the key shares with `vault operator rekey`.
