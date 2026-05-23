#!/usr/bin/env bash
# Phase 2 task 2.1 — apply openclaw schema to the Ashboard Postgres
# ----------------------------------------------------------------------
# Reads infra/postgres/openclaw-schema.sql, applies it to the existing
# Postgres instance from the Ashboard stack, then sets the openclaw_app
# role's password (random + stored in Vault under kv/openclaw/postgres/app).
#
# Idempotent. Re-run to rotate the openclaw_app password (the schema
# itself uses IF NOT EXISTS / ON CONFLICT throughout).
#
# Prerequisites:
#   - Vault unsealed; openclaw-admin AppRole credentials available
#   - Ashboard Postgres container running (script auto-detects common
#     container names; override with OC_PG_CONTAINER env)
#   - kv-v2 mounted at kv/ in Vault (from 04-vault-bootstrap.sh)

set -euo pipefail

log()  { printf '\033[1;36m[oc-pg]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[oc-pg WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[oc-pg ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (sudo)."
command -v docker >/dev/null || die "docker not in PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SCHEMA_FILE="$REPO_DIR/infra/postgres/openclaw-schema.sql"
[[ -f "$SCHEMA_FILE" ]] || die "schema file not found: $SCHEMA_FILE"

# ── Pick connection mode: docker container OR system service ────────
# Operators can force one or the other via env:
#   OC_PG_MODE=docker    + OC_PG_CONTAINER=<name>
#   OC_PG_MODE=system    (uses `sudo -u postgres psql` via peer auth)
# Otherwise we auto-detect: docker first, system as fallback.

PG_MODE="${OC_PG_MODE:-}"
PG_CONTAINER="${OC_PG_CONTAINER:-}"
PG_SUPER_USER="${OC_PG_SUPER_USER:-}"

if [[ -z "$PG_MODE" ]]; then
  # Try docker first
  for candidate in postgres asher-postgres-1 ashboard-postgres-1 ashboard-postgres ashboard_postgres oc-postgres; do
    if docker ps --format '{{.Names}}' | grep -qx "$candidate"; then
      PG_MODE="docker"
      PG_CONTAINER="$candidate"
      PG_SUPER_USER="${PG_SUPER_USER:-ashboard}"   # Ashboard's compose creates 'ashboard' as superuser
      break
    fi
  done
  # Fall back to system if no docker postgres
  if [[ -z "$PG_MODE" ]] && systemctl is-active --quiet "postgresql@*-main.service" 2>/dev/null \
       || systemctl is-active --quiet postgresql 2>/dev/null \
       || pgrep -x postgres >/dev/null 2>&1; then
    PG_MODE="system"
    PG_SUPER_USER="${PG_SUPER_USER:-postgres}"
  fi
fi

[[ -n "$PG_MODE" ]] || die \
  "No Postgres found (neither a docker container nor a system service).
   To use Ashboard's docker postgres:  cd /home/asher && docker compose up -d postgres
   To use system postgres:             sudo systemctl start postgresql"

# Wrapper: run psql via whichever path applies. Reads SQL from stdin.
psql_super() {
  case "$PG_MODE" in
    docker)
      docker exec -i "$PG_CONTAINER" psql -U "$PG_SUPER_USER" -v ON_ERROR_STOP=1 "$@"
      ;;
    system)
      sudo -u postgres psql -v ON_ERROR_STOP=1 "$@"
      ;;
  esac
}

log "Postgres mode:       $PG_MODE"
case "$PG_MODE" in
  docker) log "Postgres container:  $PG_CONTAINER" ;;
  system) log "Postgres connection: system (sudo -u postgres psql via peer auth)" ;;
esac
log "Postgres superuser:  $PG_SUPER_USER"

# ── Apply schema as the postgres superuser ──────────────────────────
log "Applying openclaw schema (idempotent)..."
psql_super < "$SCHEMA_FILE" \
  | grep -vE '^(GRANT|CREATE|INSERT|ALTER|DO|COMMENT|SET)$' || true

# ── AppRole login to Vault ──────────────────────────────────────────
echo
read -rp  "openclaw-admin Role ID: "                  ROLE_ID
read -srp "openclaw-admin Secret ID (will be hidden): " SECRET_ID; echo
[[ -n "$ROLE_ID" && -n "$SECRET_ID" ]] || die "Empty role_id / secret_id."

ADMIN_TOKEN=$(printf '%s' "$SECRET_ID" | docker exec -i oc-vault \
  vault write -field=token auth/approle/login \
  role_id="$ROLE_ID" secret_id=-)
unset SECRET_ID
[[ -n "$ADMIN_TOKEN" ]] || die "AppRole login failed."
log "AppRole login OK."

vault_exec() { docker exec -e VAULT_TOKEN="$ADMIN_TOKEN" oc-vault vault "$@"; }

# ── Generate + store + apply a fresh openclaw_app password ──────────
NEW_PW=$(openssl rand -base64 32 | tr -d '\n' | tr '/+' '_-')
log "Storing new openclaw_app password in Vault at kv/openclaw/postgres/app"
vault_exec kv put kv/openclaw/postgres/app password="$NEW_PW" >/dev/null

log "Setting openclaw_app password in Postgres"
psql_super -c "ALTER ROLE openclaw_app WITH LOGIN PASSWORD '$NEW_PW';" >/dev/null

unset NEW_PW ADMIN_TOKEN

log ""
log "✓ Phase 2 task 2.1 complete."
log "  schema:      openclaw"
log "  role:        openclaw_app  (password rotated, stored in kv/openclaw/postgres/app)"
log "  lookup rows: see openclaw.lookup"
log ""
log "Verify:"
case "$PG_MODE" in
  docker) log "  docker exec $PG_CONTAINER psql -U openclaw_app -c '\\dt openclaw.*'" ;;
  system) log "  sudo -u postgres psql -c '\\dt openclaw.*'" ;;
esac
