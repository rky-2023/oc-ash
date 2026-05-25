#!/usr/bin/env bash
# Wrapper: fetch the immudb appender password from Vault using the
# openclaw-admin AppRole, then exec uvicorn with the right env.
#
# This is the MVP stand-in for a vault-agent sidecar. Vault-agent
# (Phase 2 task 2.5b) will render secrets to a tmpfs file the
# service reads — for now we just inject env vars after a single
# AppRole login.
#
# After Phase 2 task 2.5b: this script can be deleted; vault-agent
# handles the same fetch + renewal automatically.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

log()  { printf '\033[1;36m[oc-core-run]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[oc-core-run ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# Tailscale hostname for the WebAuthn RP — same logic as run-tls.sh
command -v tailscale >/dev/null || die "tailscale not in PATH"
HOSTNAME=$(tailscale status --json \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))")
[[ -n "$HOSTNAME" ]] || die "Could not resolve tailnet hostname"

TLS_DIR="/var/lib/openclaw-core/tls"
CERT="$TLS_DIR/$HOSTNAME.crt"
KEY="$TLS_DIR/$HOSTNAME.key"

# ── Vault AppRole login → fetch immudb appender password ────────────
echo
read -rp  "openclaw-admin Role ID: "                  ROLE_ID
read -srp "openclaw-admin Secret ID (will be hidden): " SECRET_ID; echo
[[ -n "$ROLE_ID" && -n "$SECRET_ID" ]] || die "Empty role_id / secret_id."

TOK=$(printf '%s' "$SECRET_ID" | docker exec -i oc-vault \
  vault write -field=token auth/approle/login \
  role_id="$ROLE_ID" secret_id=-)
unset SECRET_ID ROLE_ID
[[ -n "$TOK" ]] || die "Vault AppRole login failed."
log "Vault login OK."

IMMUDB_PW=$(docker exec -e VAULT_TOKEN="$TOK" oc-vault \
  vault kv get -field=password kv/openclaw/immudb/appender)
[[ -n "$IMMUDB_PW" ]] || die "Could not fetch kv/openclaw/immudb/appender"
log "Fetched immudb appender password from Vault."

PG_PW=$(docker exec -e VAULT_TOKEN="$TOK" oc-vault \
  vault kv get -field=password kv/openclaw/postgres/app 2>/dev/null || true)
if [[ -n "$PG_PW" ]]; then
  PG_HOST="${OC_PG_HOST:-127.0.0.1}"
  PG_PORT="${OC_PG_PORT:-5432}"
  PG_DBNAME="${OC_PG_DBNAME:-ashboard}"
  export OC_POSTGRES_DSN="postgresql://openclaw_app:${PG_PW}@${PG_HOST}:${PG_PORT}/${PG_DBNAME}"
  PG_PW=""
  log "Fetched Postgres DSN from Vault."
else
  log "(kv/openclaw/postgres/app not set — projector will be disabled)"
fi

# Export the Vault token so the core service can call transit/sign.
# TTL is 15 min (AppRole token_ttl) — restart the service to refresh.
# Phase 2 task 2.5b (vault-agent sidecar) will handle auto-renewal.
export OC_VAULT_TOKEN="$TOK"
unset TOK
log "Vault token exported for transit signing (TTL 15 min)."

# ── Activate venv ───────────────────────────────────────────────────
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  if [[ -f "$CORE_DIR/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$CORE_DIR/.venv/bin/activate"
  else
    die ".venv not found. Create with: python3.14 -m venv .venv && source .venv/bin/activate && pip install -e .[dev]"
  fi
fi

# ── Export env + launch ─────────────────────────────────────────────
export OC_WEBAUTHN_RP_ID="$HOSTNAME"
export OC_WEBAUTHN_EXPECTED_ORIGINS_CSV="https://$HOSTNAME:8000"
export OC_IMMUDB_HOST="${OC_IMMUDB_HOST:-127.0.0.1}"
export OC_IMMUDB_PORT="${OC_IMMUDB_PORT:-3322}"
export OC_IMMUDB_USER="appender"
export OC_IMMUDB_PASSWORD="$IMMUDB_PW"
export OC_IMMUDB_DATABASE="openclaw_audit"
export OC_ENABLE_AUDIT_APPENDER="true"

# Wipe local password var after export
IMMUDB_PW=""

log "RP_ID:    $OC_WEBAUTHN_RP_ID"
log "Origin:   $OC_WEBAUTHN_EXPECTED_ORIGINS_CSV"
log "immudb:   $OC_IMMUDB_USER@$OC_IMMUDB_HOST:$OC_IMMUDB_PORT/$OC_IMMUDB_DATABASE"
log "URL:      https://$HOSTNAME:8000"
log "Launching uvicorn (Ctrl+C to stop)..."
echo

cd "$CORE_DIR"

# If TLS files exist, run with HTTPS. Otherwise plain HTTP on localhost.
if [[ -f "$CERT" && -f "$KEY" ]]; then
  exec uvicorn app.main:app \
    --host 0.0.0.0 --port 8000 \
    --ssl-certfile="$CERT" --ssl-keyfile="$KEY" \
    --reload
else
  log "(no TLS cert at $TLS_DIR/$HOSTNAME.crt — falling back to http://localhost:8000)"
  exec uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
fi
