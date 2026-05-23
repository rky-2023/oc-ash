#!/usr/bin/env bash
# Phase 1 task 1.2
# ----------------------------------------------------------------------
# Bring up just the Vault container.
#
# Vault starts SEALED. The next steps are:
#   ./02-init-vault.sh    — Shamir 3-of-5 init (one-time)
#   ./03-unseal-vault.sh  — interactive unseal (every reboot)
#
# Prerequisites:
#   - ./00-create-luks-volumes.sh has been run (mount /mnt/openclaw/vault exists)
#   - docker + docker compose installed
#   - Run from the infra/scripts/ directory (or any other; we resolve paths)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_FILE="$INFRA_DIR/docker-compose.openclaw.yml"
ENV_FILE="$INFRA_DIR/.env.openclaw"
ENV_EXAMPLE="$INFRA_DIR/.env.openclaw.example"

log()  { printf '\033[1;36m[oc-vault-up]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[oc-vault-up WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[oc-vault-up ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# ── Pre-flight ────────────────────────────────────────────────────
[[ -f "$COMPOSE_FILE" ]] || die "compose file not found at $COMPOSE_FILE"
mountpoint -q /mnt/openclaw/vault \
  || die "/mnt/openclaw/vault is not mounted. Run 00-create-luks-volumes.sh first (or migrate-mount-paths-to-mnt.sh if you bootstrapped with the old /var/lib paths)."

# .env.openclaw is gitignored and holds bootstrap-only env values (per
# ADR-002 D12: real production secrets come from vault-agent at runtime).
# Auto-create from .example on first run so the operator isn't blocked,
# but flag it so they remember to rotate the placeholders before going
# anywhere near production.
if [[ ! -f "$ENV_FILE" ]]; then
  [[ -f "$ENV_EXAMPLE" ]] || die "Neither $ENV_FILE nor $ENV_EXAMPLE exists."
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  warn "Created $ENV_FILE from .example. The MinIO bootstrap credentials"
  warn "inside are placeholders. Rotate them via Vault (vault-agent) before"
  warn "treating this stack as production. See ADR-002 D12."
fi

command -v docker >/dev/null 2>&1 || die "docker not installed"
docker compose version >/dev/null 2>&1 || die "docker compose v2 not available"

# ── Set ownership on Vault's data dir ─────────────────────────────
# The hashicorp/vault container runs as user `vault` (uid 100, gid 1000).
# Our LUKS mount point is created root:root mode 700 by 00-create-luks-volumes.sh
# so the Vault process inside the container can't write to /vault/data.
# Chown the bind target to the container's vault user, keep mode 700.
log "Ensuring /mnt/openclaw/vault is owned by vault container user (uid 100)..."
chown -R 100:1000 /mnt/openclaw/vault
chmod 700 /mnt/openclaw/vault

# ── Bring up vault only ───────────────────────────────────────────
log "Starting Vault container..."
cd "$INFRA_DIR"
docker compose --env-file .env.openclaw -f docker-compose.openclaw.yml up -d vault

log "Waiting for Vault to become reachable..."
for i in $(seq 1 30); do
  if docker exec oc-vault vault status 2>/dev/null | grep -q "Initialized"; then
    break
  fi
  sleep 1
done

log ""
log "Vault current status:"
docker exec oc-vault vault status || true

log ""
log "Expected state:"
log "  Sealed: true   (we have not unsealed yet — that's normal on first run)"
log "  Initialized: false (we haven't run vault operator init yet — see 02-init-vault.sh)"
log ""
log "Next:"
log "  - First time only: ./02-init-vault.sh  (creates the Shamir shares and root token)"
log "  - Every reboot:    ./03-unseal-vault.sh  (provide 3 shares to unseal)"
