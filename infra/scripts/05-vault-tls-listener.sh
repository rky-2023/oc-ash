#!/usr/bin/env bash
# Phase 1 task 1.7 — swap Vault's bootstrap listener to TLS
# ----------------------------------------------------------------------
# Issues a 30-day server cert from pki_int/, places it inside the Vault
# container, also writes the CA cert to /mnt/openclaw/shared/ for other
# services to bind-mount, restarts Vault, then prompts the operator to
# re-unseal.
#
# Anchors: Phase 1 runbook task 1.7, ADR-002 D6 (mTLS via Vault PKI),
#          ADR-002 D14 (annual root CA, 24h-equivalent leaves; listener
#          cert is intentionally a longer 30d TTL because no vault-agent
#          renews it yet — see Future Work in this script's tail).
#
# Prerequisites:
#   - Phase 1 tasks 1.1–1.6 complete (Vault running, unsealed, AppRole
#     'openclaw-admin' exists, root token already destroyed).
#   - You have the openclaw-admin role_id + secret_id captured (PWM).
#
# After this runs:
#   - Vault listens with TLS at https://127.0.0.1:8200 + SANs for
#     vault.openclaw.local, localhost, vault, oc-vault, 127.0.0.1.
#   - /vault/data/tls/{server.crt,server.key,ca.crt,fullchain.crt}
#     populated inside the container.
#   - /mnt/openclaw/shared/ca.crt populated on the host for downstream
#     services to bind-mount.
#   - infra/vault/config/server.hcl already in TLS-enabled form via the
#     PR; the container restart picks it up.
#   - All openclaw scripts (03 / 04 / future) use HTTPS via env vars.

set -euo pipefail

# ────────────────────────────────────────────────────────────────────
log()  { printf '\033[1;36m[oc-tls]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[oc-tls WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[oc-tls ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

ADMIN_TOKEN=""
cleanup() { unset ADMIN_TOKEN; }
trap cleanup EXIT INT TERM

# ────────────────────────────────────────────────────────────────────
# Pre-flight
# ────────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run as root (sudo)."
docker exec oc-vault true 2>/dev/null || die "oc-vault container is not running."

# Currently HTTP — we authenticate before changing anything.
ADDR_NOW="http://127.0.0.1:8200"

# Verify Vault is unsealed
STATUS=$(docker exec oc-vault vault status -address="$ADDR_NOW" -format=json 2>/dev/null || true)
echo "$STATUS" | grep -Eq '"sealed"[[:space:]]*:[[:space:]]*false' \
  || die "Vault is sealed. Run ./03-unseal-vault.sh first."

command -v python3 >/dev/null || die "python3 is required to parse Vault JSON output."

# ────────────────────────────────────────────────────────────────────
# AppRole login
# ────────────────────────────────────────────────────────────────────
echo
read -rp  "openclaw-admin Role ID: "                  ROLE_ID
read -srp "openclaw-admin Secret ID (will be hidden): " SECRET_ID; echo
[[ -n "$ROLE_ID"   ]] || die "Empty role_id."
[[ -n "$SECRET_ID" ]] || die "Empty secret_id."

# Login via AppRole. secret_id over stdin so it's not in argv.
ADMIN_TOKEN=$(printf '%s' "$SECRET_ID" | docker exec -i \
  -e VAULT_ADDR="$ADDR_NOW" oc-vault \
  vault write -field=token auth/approle/login \
  role_id="$ROLE_ID" secret_id=-)
unset SECRET_ID
[[ -n "$ADMIN_TOKEN" ]] || die "AppRole login failed."
log "AppRole login OK."

vault_exec() {
  docker exec -e VAULT_ADDR="$ADDR_NOW" -e VAULT_TOKEN="$ADMIN_TOKEN" \
    oc-vault vault "$@"
}

# ────────────────────────────────────────────────────────────────────
# Create / update the vault-listener role (idempotent — write is overwrite-safe)
# ────────────────────────────────────────────────────────────────────
log "Configuring pki_int/roles/vault-listener (max_ttl=30d, IP SANs allowed)"
vault_exec write pki_int/roles/vault-listener \
  allowed_domains="openclaw.local,localhost,vault,oc-vault" \
  allow_bare_domains=true \
  allow_subdomains=true \
  allow_glob_domains=false \
  allow_ip_sans=true \
  enforce_hostnames=false \
  max_ttl=720h \
  ttl=720h \
  key_type=ed25519 \
  server_flag=true \
  client_flag=false >/dev/null

# ────────────────────────────────────────────────────────────────────
# Issue the listener cert
# ────────────────────────────────────────────────────────────────────
log "Issuing 30-day Vault listener cert"
RESP=$(vault_exec write -format=json pki_int/issue/vault-listener \
  common_name="vault.openclaw.local" \
  alt_names="localhost,vault,oc-vault" \
  ip_sans="127.0.0.1" \
  ttl=720h)

# Parse fields with python3 (always present on Ubuntu base).
extract() {
  python3 -c "import json,sys; print(json.loads(sys.stdin.read())['data']['$1'])" <<<"$RESP"
}

SERVER_CRT=$(extract certificate)
SERVER_KEY=$(extract private_key)
ISSUING_CA=$(extract issuing_ca)
SERIAL=$(extract serial_number)

# ────────────────────────────────────────────────────────────────────
# Place files inside the container at /vault/data/tls/
# ────────────────────────────────────────────────────────────────────
log "Writing TLS files to /vault/data/tls/ inside oc-vault"
docker exec -u 0 oc-vault sh -c '
  mkdir -p /vault/data/tls &&
  chown -R 100:1000 /vault/data/tls &&
  chmod 750 /vault/data/tls'

# Write each file via docker exec stdin (avoids host /tmp / snap confinement)
printf '%s\n' "$SERVER_CRT" | docker exec -i oc-vault sh -c 'cat > /vault/data/tls/server.crt'
printf '%s\n' "$SERVER_KEY" | docker exec -i oc-vault sh -c 'cat > /vault/data/tls/server.key'
printf '%s\n' "$ISSUING_CA" | docker exec -i oc-vault sh -c 'cat > /vault/data/tls/ca.crt'
# Concatenated chain (handy for some clients):
printf '%s\n%s\n' "$SERVER_CRT" "$ISSUING_CA" \
  | docker exec -i oc-vault sh -c 'cat > /vault/data/tls/fullchain.crt'

# Lock down perms inside the container
docker exec -u 0 oc-vault sh -c '
  chown 100:1000 /vault/data/tls/* &&
  chmod 600 /vault/data/tls/server.key &&
  chmod 644 /vault/data/tls/server.crt /vault/data/tls/ca.crt /vault/data/tls/fullchain.crt'

log "  ✓ Issued cert serial $SERIAL"

# ────────────────────────────────────────────────────────────────────
# Copy CA cert to /mnt/openclaw/shared/ for other services
# ────────────────────────────────────────────────────────────────────
log "Publishing CA cert to /mnt/openclaw/shared/ca.crt for downstream services"
mkdir -p /mnt/openclaw/shared
chmod 755 /mnt/openclaw/shared
printf '%s\n' "$ISSUING_CA" > /mnt/openclaw/shared/ca.crt
chmod 644 /mnt/openclaw/shared/ca.crt
log "  ✓ /mnt/openclaw/shared/ca.crt placed"

# ────────────────────────────────────────────────────────────────────
# Restart Vault (with TLS-enabled server.hcl from this PR)
# ────────────────────────────────────────────────────────────────────
echo
cat <<'EOF'
═════════════════════════════════════════════════════════════════
  ABOUT TO RESTART VAULT WITH TLS ENABLED
═════════════════════════════════════════════════════════════════

  Restart will leave Vault sealed (as always). After restart, run:

    sudo ./infra/scripts/03-unseal-vault.sh

  Provide any 3 of your 5 Shamir shares to unseal. Vault will
  then accept connections at https://127.0.0.1:8200.

EOF
read -rp "Press ENTER to restart Vault, or Ctrl+C to abort: " _

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# IMPORTANT: --force-recreate (not `restart`). Compose env changes only
# apply to a NEW container, not to a restart of the existing one. Without
# recreate the CLI inside the container still has VAULT_ADDR=http://...
# from when it was first created, and trying to talk to the now-HTTPS
# listener returns "Client sent an HTTP request to an HTTPS server."
docker compose --env-file "$INFRA_DIR/.env.openclaw" \
  -f "$INFRA_DIR/docker-compose.openclaw.yml" up -d --force-recreate vault

sleep 5
log "Vault recreated. Current status:"
docker exec oc-vault vault status 2>&1 | head -15 || true

cat <<'EOF'

═════════════════════════════════════════════════════════════════
  PHASE 1 TASK 1.7 — TLS LISTENER ENABLED
═════════════════════════════════════════════════════════════════

  Cert:       /vault/data/tls/server.crt (30-day TTL)
  Key:        /vault/data/tls/server.key
  CA:         /vault/data/tls/ca.crt   (and /mnt/openclaw/shared/ca.crt)
  Listener:   https://127.0.0.1:8200

  Next steps:
    1. sudo ./infra/scripts/03-unseal-vault.sh   (provide 3 shares)
    2. Verify TLS: docker exec oc-vault vault status

  Renewal: this cert expires in 30 days. Re-run this script monthly
  until vault-agent self-renewal is wired up (Phase 11 hardening).
EOF
