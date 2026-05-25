#!/usr/bin/env bash
# Create the two Vault transit keys used for audit envelope signing
# (ADR-002 D12, Phase 2 task 2.6).
#
#   transit/keys/audit-service   — Ed25519, signs sig_service slot
#   transit/keys/audit-appender  — Ed25519, signs sig_appender slot
#
# Separate keys so that a compromised appender credential cannot forge
# service signatures, and vice versa.
#
# Rotation: auto-rotation every 30 days. Min decryption version = 1
# so old envelopes are still verifiable after key rotation.
#
# Run once; idempotent — checks for key existence before creating.
# Requires the Vault container (oc-vault) to be unsealed and the
# openclaw-admin AppRole credentials to be available.

set -euo pipefail

VAULT_CONTAINER="${VAULT_CONTAINER:-oc-vault}"

log() { printf '\033[1;36m[09-transit-keys]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[09-transit-keys ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

vault_exec() { docker exec -e VAULT_TOKEN="$VAULT_TOKEN" "$VAULT_CONTAINER" vault "$@"; }

# ── Vault login ─────────────────────────────────────────────────────
echo
read -rp  "openclaw-admin Role ID: "                   ROLE_ID
read -srp "openclaw-admin Secret ID (hidden): " SECRET_ID; echo
[[ -n "$ROLE_ID" && -n "$SECRET_ID" ]] || die "Empty credentials."

VAULT_TOKEN=$(printf '%s' "$SECRET_ID" | \
  docker exec -i "$VAULT_CONTAINER" vault write -field=token auth/approle/login \
  role_id="$ROLE_ID" secret_id=-)
unset SECRET_ID ROLE_ID
[[ -n "$VAULT_TOKEN" ]] || die "Vault AppRole login failed."
log "Vault login OK."

# ── Create transit keys (idempotent) ────────────────────────────────
for KEY_NAME in audit-service audit-appender; do
  if vault_exec read transit/keys/"$KEY_NAME" >/dev/null 2>&1; then
    log "  transit/keys/$KEY_NAME already exists — skipping create."
  else
    log "  Creating transit/keys/$KEY_NAME (ed25519)..."
    vault_exec write -f transit/keys/"$KEY_NAME" type=ed25519
    log "  Created."
  fi

  # Enable auto-rotation (30 days) and set min decryption version
  log "  Tuning transit/keys/$KEY_NAME (auto-rotate=30d, min-decrypt=1)..."
  vault_exec write transit/keys/"$KEY_NAME"/config \
    auto_rotate_period=720h \
    min_decryption_version=1 \
    deletion_allowed=false
done

log "Transit keys ready."
log ""
log "Next step: export OC_VAULT_TOKEN when launching core, or run:"
log "  core/scripts/run-with-vault-creds.sh"
log "That script now exports OC_VAULT_TOKEN so core can call transit/sign."
