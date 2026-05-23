#!/usr/bin/env bash
# Helper: upload immudb credentials from /tmp/oc-immudb-creds.txt into Vault
# under kv/openclaw/immudb/{admin,appender,projector}, then shred the file.
#
# Use this if you (or an automated step) bootstrapped immudb without
# running it through the AppRole-Vault flow in 07-immudb-bootstrap.sh.
# After this completes, kv/openclaw/immudb/* matches what the
# audit-appender / audit-projector services in Phase 2 will read.

set -euo pipefail

log()  { printf '\033[1;36m[oc-upload]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[oc-upload ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

CREDS_FILE="${OC_IMMUDB_CREDS_FILE:-/tmp/oc-immudb-creds.txt}"
[[ -f "$CREDS_FILE" ]] || die "$CREDS_FILE not found. Pass via OC_IMMUDB_CREDS_FILE if elsewhere."

ADMIN_PW=$(grep -E '^admin=' "$CREDS_FILE" | cut -d= -f2-)
APPENDER_PW=$(grep -E '^appender=' "$CREDS_FILE" | cut -d= -f2-)
PROJECTOR_PW=$(grep -E '^projector=' "$CREDS_FILE" | cut -d= -f2-)

[[ -n "$ADMIN_PW" && -n "$APPENDER_PW" && -n "$PROJECTOR_PW" ]] \
  || die "Missing one or more passwords in $CREDS_FILE"

# AppRole login
echo
read -rp  "openclaw-admin Role ID: "                  ROLE_ID
read -srp "openclaw-admin Secret ID (will be hidden): " SECRET_ID; echo
[[ -n "$ROLE_ID" && -n "$SECRET_ID" ]] || die "Empty role_id / secret_id."

TOK=$(printf '%s' "$SECRET_ID" | docker exec -i oc-vault \
  vault write -field=token auth/approle/login \
  role_id="$ROLE_ID" secret_id=-)
unset SECRET_ID ROLE_ID
[[ -n "$TOK" ]] || die "Vault AppRole login failed."
log "AppRole login OK."

# Upload each — pass passwords via stdin so they don't show in /proc/<pid>/cmdline.
upload() {
  local path="$1" pw="$2"
  printf 'password=%s\n' "$pw" \
    | docker exec -i -e VAULT_TOKEN="$TOK" oc-vault vault kv put "$path" - >/dev/null
  log "  ✓ uploaded $path"
}

upload kv/openclaw/immudb/admin     "$ADMIN_PW"
upload kv/openclaw/immudb/appender  "$APPENDER_PW"
upload kv/openclaw/immudb/projector "$PROJECTOR_PW"

unset TOK ADMIN_PW APPENDER_PW PROJECTOR_PW

log ""
log "Verifying:"
docker exec oc-vault vault kv list kv/openclaw/immudb 2>&1 | head -10 || true

log ""
log "Shredding $CREDS_FILE..."
shred -u "$CREDS_FILE" 2>/dev/null || rm -f "$CREDS_FILE"
log "✓ Done. Vault state now matches what 07 would have produced."
