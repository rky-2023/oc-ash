#!/usr/bin/env bash
# Phase 2 task 2.3 — bring up immudb + create audit database + users
# ----------------------------------------------------------------------
# Brings up the immudb container (already declared in
# infra/docker-compose.openclaw.yml), rotates the default admin password,
# creates the openclaw_audit database, and creates two users:
#   - appender  (R/W on openclaw_audit) — used by the audit-appender service
#   - projector (R-only on openclaw_audit) — used by the audit-projector service
# All three passwords are random + stored in Vault under
# kv/openclaw/immudb/{admin,appender,projector}.
#
# Idempotent: if accounts already exist, the script rotates their
# passwords. If the database exists, no-op.
#
# Prerequisites:
#   - Vault unsealed; openclaw-admin AppRole credentials available
#   - dm-crypt-backed /mnt/openclaw/immudb already mounted (from 00)
#   - docker compose can bring services up

set -euo pipefail

log()  { printf '\033[1;36m[oc-immudb]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[oc-immudb WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[oc-immudb ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (sudo)."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

mountpoint -q /mnt/openclaw/immudb || die "/mnt/openclaw/immudb not mounted. Run 00 first."

# ── Bring up immudb if not already running ──────────────────────────
if ! docker ps --format '{{.Names}}' | grep -qx oc-immudb; then
  log "Starting oc-immudb..."
  docker compose --env-file "$INFRA_DIR/.env.openclaw" \
    -f "$INFRA_DIR/docker-compose.openclaw.yml" up -d immudb
  sleep 5
fi

# Wait for immuadmin to be reachable
log "Waiting for immudb to be reachable..."
for i in $(seq 1 30); do
  if docker exec oc-immudb sh -c 'echo q | immuadmin login immudb 2>&1' \
       | grep -q -E '(password|Password)'; then
    break
  fi
  sleep 1
done

# Fix ownership of mount point if needed (immudb container's default uid)
docker exec -u 0 oc-immudb chown -R 3322:3322 /var/lib/immudb 2>/dev/null || true

# ── AppRole login to Vault ──────────────────────────────────────────
echo
read -rp  "openclaw-admin Role ID: "                  ROLE_ID
read -srp "openclaw-admin Secret ID (will be hidden): " SECRET_ID; echo
[[ -n "$ROLE_ID" && -n "$SECRET_ID" ]] || die "Empty role_id / secret_id."

ADMIN_TOKEN=$(printf '%s' "$SECRET_ID" | docker exec -i oc-vault \
  vault write -field=token auth/approle/login \
  role_id="$ROLE_ID" secret_id=-)
unset SECRET_ID
[[ -n "$ADMIN_TOKEN" ]] || die "Vault AppRole login failed."
log "Vault AppRole login OK."

vault_exec() { docker exec -e VAULT_TOKEN="$ADMIN_TOKEN" oc-vault vault "$@"; }

# Helper: store a secret in Vault kv-v2
vault_kv_put() {
  local path="$1" key="$2" val="$3"
  vault_exec kv put "$path" "$key=$val" >/dev/null
}

# Helper: get current admin password from Vault if previously rotated
get_kv() {
  vault_exec kv get -field="$2" "$1" 2>/dev/null || echo ""
}

# ── Determine current immudb admin password ─────────────────────────
# First run: default is immudb/immudb. Subsequent: Vault has the rotated one.
CUR_ADMIN_PW=$(get_kv kv/openclaw/immudb/admin password)
if [[ -z "$CUR_ADMIN_PW" ]]; then
  CUR_ADMIN_PW="immudb"
  log "Using default admin/immudb password (first run)"
else
  log "Using current admin password from Vault"
fi

# ── Login as immudb admin ───────────────────────────────────────────
# immuadmin needs an interactive password prompt; pipe via heredoc.
immu_admin() {
  docker exec -i oc-immudb sh -c "
    echo '$CUR_ADMIN_PW' | immuadmin login immudb 2>&1 | tail -1
    $1
  " 2>&1
}

# ── Generate new passwords ──────────────────────────────────────────
gen_pw() { openssl rand -base64 32 | tr -d '\n' | tr '/+' '_-'; }
NEW_ADMIN_PW=$(gen_pw)
APPENDER_PW=$(gen_pw)
PROJECTOR_PW=$(gen_pw)

# ── Rotate admin password ───────────────────────────────────────────
log "Rotating admin password"
docker exec -i oc-immudb sh -c "
  immuadmin login immudb <<< '$CUR_ADMIN_PW' >/dev/null 2>&1
  immuadmin user changepassword immudb <<EOF >/dev/null 2>&1
$CUR_ADMIN_PW
$NEW_ADMIN_PW
$NEW_ADMIN_PW
EOF
" || warn "Admin password rotation may have already happened — proceeding with $NEW_ADMIN_PW"

vault_kv_put kv/openclaw/immudb/admin password "$NEW_ADMIN_PW"
log "  ✓ admin password rotated + stored in Vault"

# ── Create openclaw_audit database ──────────────────────────────────
log "Creating database 'openclaw_audit' (if not exists)"
docker exec -i oc-immudb sh -c "
  immuadmin login immudb <<< '$NEW_ADMIN_PW' >/dev/null 2>&1
  immuadmin database list 2>/dev/null | grep -q openclaw_audit \
    || immuadmin database create openclaw_audit >/dev/null
" || warn "database creation step had a soft error"
log "  ✓ database openclaw_audit ready"

# ── Create appender user (R/W) ──────────────────────────────────────
log "Creating user 'appender' (RW on openclaw_audit)"
docker exec -i oc-immudb sh -c "
  immuadmin login immudb <<< '$NEW_ADMIN_PW' >/dev/null 2>&1
  immuadmin user create appender readwrite openclaw_audit <<EOF >/dev/null 2>&1
$APPENDER_PW
$APPENDER_PW
EOF
" || {
  log "  user 'appender' may already exist — rotating password"
  docker exec -i oc-immudb sh -c "
    immuadmin login immudb <<< '$NEW_ADMIN_PW' >/dev/null 2>&1
    immuadmin user changepassword appender <<EOF >/dev/null 2>&1
$APPENDER_PW
$APPENDER_PW
EOF
  " || warn "Could not rotate appender password — Vault value may be stale"
}
vault_kv_put kv/openclaw/immudb/appender password "$APPENDER_PW"
log "  ✓ appender password stored in Vault"

# ── Create projector user (R-only) ──────────────────────────────────
log "Creating user 'projector' (R-only on openclaw_audit)"
docker exec -i oc-immudb sh -c "
  immuadmin login immudb <<< '$NEW_ADMIN_PW' >/dev/null 2>&1
  immuadmin user create projector readonly openclaw_audit <<EOF >/dev/null 2>&1
$PROJECTOR_PW
$PROJECTOR_PW
EOF
" || {
  log "  user 'projector' may already exist — rotating password"
  docker exec -i oc-immudb sh -c "
    immuadmin login immudb <<< '$NEW_ADMIN_PW' >/dev/null 2>&1
    immuadmin user changepassword projector <<EOF >/dev/null 2>&1
$PROJECTOR_PW
$PROJECTOR_PW
EOF
  " || warn "Could not rotate projector password"
}
vault_kv_put kv/openclaw/immudb/projector password "$PROJECTOR_PW"
log "  ✓ projector password stored in Vault"

unset NEW_ADMIN_PW APPENDER_PW PROJECTOR_PW CUR_ADMIN_PW ADMIN_TOKEN

log ""
log "✓ Phase 2 task 2.3 complete."
log "  database:  openclaw_audit"
log "  users:     appender (RW), projector (R)"
log "  passwords: rotated, stored in kv/openclaw/immudb/{admin,appender,projector}"
