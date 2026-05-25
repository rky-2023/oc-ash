#!/usr/bin/env bash
# Store GitHub App credentials for openclaw-bot in Vault KV.
# Run AFTER creating the GitHub App via the web UI (see docs/github-app-setup.md).
#
# What this stores:
#   kv/openclaw/github-app/openclaw-bot/app-id           → App ID (integer as string)
#   kv/openclaw/github-app/openclaw-bot/installation-id  → Installation ID
#   kv/openclaw/github-app/openclaw-bot/private-key-pem  → RSA-2048 private key PEM
#
# After running, shred the local .pem file — the key now lives in Vault only.
# The oc-with-vault-creds.sh wrapper fetches these at CLI invocation time.

set -euo pipefail

log() { printf '\033[1;36m[11-gh-app]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[11-gh-app ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# ── Prompt for values ────────────────────────────────────────────────────────
echo
log "GitHub App bootstrap for openclaw-bot"
log "See docs/github-app-setup.md for how to get these values."
echo

read -rp  "GitHub App ID (integer):                    "  APP_ID
read -rp  "GitHub Installation ID:                     "  INSTALL_ID
read -rp  "Path to downloaded .pem file:               "  PEM_PATH
echo
read -rp  "openclaw-admin Role ID:                     "  ROLE_ID
read -srp "openclaw-admin Secret ID (will be hidden):  "  SECRET_ID; echo

[[ -n "$APP_ID" && -n "$INSTALL_ID" && -n "$PEM_PATH" ]] \
  || die "App ID, Installation ID, and PEM path are required."
[[ -n "$ROLE_ID" && -n "$SECRET_ID" ]] \
  || die "Vault Role ID and Secret ID are required."
[[ -f "$PEM_PATH" ]] || die "PEM file not found: $PEM_PATH"

# ── Vault AppRole login ──────────────────────────────────────────────────────
log "Checking Vault connectivity..."
docker exec oc-vault vault status > /dev/null 2>&1 || die "oc-vault container not running or Vault sealed."

log "Logging in to Vault via AppRole..."
TOK=$(printf '%s' "$SECRET_ID" | docker exec -i oc-vault \
  vault write -field=token auth/approle/login \
  role_id="$ROLE_ID" secret_id=-)
unset SECRET_ID ROLE_ID
[[ -n "$TOK" ]] || die "Vault AppRole login failed."
log "Vault login OK."

VAULT_CMD="docker exec -e VAULT_TOKEN=$TOK -i oc-vault vault"

# ── Vault write ──────────────────────────────────────────────────────────────
log "Writing app-id..."
$VAULT_CMD kv put kv/openclaw/github-app/openclaw-bot/app-id \
  value="$APP_ID"

log "Writing installation-id..."
$VAULT_CMD kv put kv/openclaw/github-app/openclaw-bot/installation-id \
  value="$INSTALL_ID"

log "Writing private-key-pem (reading from $PEM_PATH)..."
PEM_CONTENT=$(<"$PEM_PATH")
printf '%s' "$PEM_CONTENT" | docker exec -e VAULT_TOKEN="$TOK" -i oc-vault \
  vault kv put kv/openclaw/github-app/openclaw-bot/private-key-pem value=-

log "Verifying stored app-id..."
STORED=$($VAULT_CMD kv get -field=value kv/openclaw/github-app/openclaw-bot/app-id)
[[ "$STORED" == "$APP_ID" ]] || die "Stored app-id mismatch!"

unset TOK

log "Credentials stored. Shredding local PEM..."
if command -v shred &>/dev/null; then
  shred -u "$PEM_PATH"
  log "Shredded: $PEM_PATH"
else
  rm -f "$PEM_PATH"
  log "Deleted: $PEM_PATH (shred not available; consider secure-delete on macOS)"
fi

unset APP_ID INSTALL_ID PEM_CONTENT

echo
log "Done. Verify with:"
log "  docker exec -i oc-vault vault kv get kv/openclaw/github-app/openclaw-bot/app-id"
log "  docker exec -i oc-vault vault kv get kv/openclaw/github-app/openclaw-bot/installation-id"
log "  (private-key-pem is intentionally not echoed)"
