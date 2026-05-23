#!/usr/bin/env bash
# Phase 1 tasks 1.4 + 1.5 + 1.6
# ----------------------------------------------------------------------
# Bootstrap a freshly-unsealed Vault: enable audit logging, enable all
# the secret engines openclaw needs, generate the internal CA hierarchy,
# create the operator admin AppRole, and then destroy the root token.
#
# Idempotent throughout — re-runnable until the final destroy step.
# The destroy step is gated behind typing the word DESTROY.
#
# Anchors:
#   Phase 1 runbook tasks 1.4 / 1.5 / 1.6
#   ADR-002 D8  — AppRole-based service auth, no long-lived tokens
#   ADR-002 D12 — Vault for high-value material only
#   ADR-002 D14 — annual mTLS CA root rotation
#
# Prerequisites:
#   - Vault container running + UNSEALED (./03-unseal-vault.sh succeeded)
#   - You have the root token captured locally (from ./02-init-vault.sh
#     or a generate-root rotation)

set -euo pipefail

# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────
log()  { printf '\033[1;36m[oc-bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[oc-bootstrap WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[oc-bootstrap ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# Root-token env state cleared on exit (success, error, or signal).
ROOT_TOKEN=""
cleanup() { unset ROOT_TOKEN; }
trap cleanup EXIT INT TERM

# Run a vault command inside the oc-vault container with the root
# token. Token never appears in argv — passed via -e (env), only briefly
# visible in /proc/<pid>/environ during the exec.
vault_exec() {
  docker exec \
    -e VAULT_ADDR=http://127.0.0.1:8200 \
    -e VAULT_TOKEN="$ROOT_TOKEN" \
    oc-vault vault "$@"
}

# Spaced/compact JSON tolerant regex (lesson from the 03 grep bug).
init_re='"initialized"[[:space:]]*:[[:space:]]*true'
sealed_false_re='"sealed"[[:space:]]*:[[:space:]]*false'

# ────────────────────────────────────────────────────────────────────
# Pre-flight
# ────────────────────────────────────────────────────────────────────
docker exec oc-vault true 2>/dev/null \
  || die "oc-vault container is not running. Run ./01-bring-up-vault.sh first."

STATUS_JSON=$(docker exec oc-vault vault status \
  -address=http://127.0.0.1:8200 -format=json 2>/dev/null || true)

echo "$STATUS_JSON" | grep -Eq "$init_re" \
  || die "Vault is not initialized. Run ./02-init-vault.sh first."

echo "$STATUS_JSON" | grep -Eq "$sealed_false_re" \
  || die "Vault is sealed. Run ./03-unseal-vault.sh first."

# Prompt for root token (hidden)
echo
read -srp "Root token (will be hidden): " ROOT_TOKEN
echo
[[ -n "$ROOT_TOKEN" ]] || die "Empty root token"

# Verify the token actually works
if ! vault_exec token lookup -format=json >/dev/null 2>&1; then
  die "Root token verification failed. Either the token is wrong, or it was already revoked."
fi
log "Root token verified."

# ────────────────────────────────────────────────────────────────────
# 1.4 — Audit logging
# ────────────────────────────────────────────────────────────────────
log ""
log "==== TASK 1.4: Audit logging ===="

# Create the audit log directory inside the container (in /vault/data,
# which is our dm-crypt-backed volume, owned by vault user).
docker exec -u vault oc-vault mkdir -p /vault/data/audit

if vault_exec audit list 2>/dev/null | grep -q '^file/'; then
  log "  Audit device 'file/' already enabled, skipping"
else
  log "  Enabling file audit device at /vault/data/audit/file-audit.log"
  vault_exec audit enable file file_path=/vault/data/audit/file-audit.log
fi

log "  ✓ Audit logging on"

# ────────────────────────────────────────────────────────────────────
# 1.5 — Secret engines
# ────────────────────────────────────────────────────────────────────
log ""
log "==== TASK 1.5: Secret engines ===="

# Helper: enable engine only if not already mounted at the given path
enable_if_missing() {
  local path="$1"; shift
  if vault_exec secrets list 2>/dev/null | awk '{print $1}' | grep -qx "${path}/"; then
    log "  Secrets engine at '$path/' already enabled, skipping"
  else
    log "  Enabling secrets engine at '$path/'"
    vault_exec secrets enable "$@"
  fi
}

# KV-v2 for low-volume KV (high-value paths only — per ADR-002 D12)
enable_if_missing kv -path=kv -version=2 kv

# PKI root (10y max, used only to sign the intermediate)
enable_if_missing pki_root -path=pki_root -max-lease-ttl=87600h pki
# Tune the engine after enable (idempotent)
vault_exec secrets tune -max-lease-ttl=87600h pki_root >/dev/null

# Generate root CA cert (idempotent — if a cert exists, skip)
ROOT_CA=$(vault_exec read -field=certificate pki_root/cert/ca 2>/dev/null || true)
if [[ -z "$ROOT_CA" ]]; then
  log "  Generating internal root CA (ed25519, 10y)"
  vault_exec write -format=json pki_root/root/generate/internal \
    common_name="openclaw internal root CA" \
    ttl=87600h \
    key_type=ed25519 >/dev/null
else
  log "  Root CA already exists, skipping"
fi
# Set issuing + CRL URLs (idempotent)
vault_exec write pki_root/config/urls \
  issuing_certificates="http://127.0.0.1:8200/v1/pki_root/ca" \
  crl_distribution_points="http://127.0.0.1:8200/v1/pki_root/crl" >/dev/null

# PKI intermediate (1y, rotated annually per ADR-002 D14)
enable_if_missing pki_int -path=pki_int -max-lease-ttl=8760h pki
vault_exec secrets tune -max-lease-ttl=8760h pki_int >/dev/null

# Generate intermediate (idempotent — check if it already has a cert)
INT_CA=$(vault_exec read -field=certificate pki_int/cert/ca 2>/dev/null || true)
if [[ -z "$INT_CA" ]]; then
  log "  Generating intermediate CSR + signing with root"

  # Use temporary files on both host and container. The host files are
  # cleaned by the EXIT trap registered below; the container files we
  # `rm` explicitly at the end of the block.
  TMP_CSR=$(mktemp /tmp/oc-int-csr.XXXXXX.pem)
  TMP_CRT=$(mktemp /tmp/oc-int-crt.XXXXXX.pem)
  # Extend the EXIT trap to also wipe these tmp files.
  trap 'rm -f "$TMP_CSR" "$TMP_CRT"; cleanup' EXIT INT TERM

  # Step 1: generate the intermediate CSR. `-field=csr` outputs raw PEM
  # to stdout (no JSON wrapping); redirect to file directly.
  vault_exec write -field=csr pki_int/intermediate/generate/internal \
    common_name="openclaw internal intermediate CA" \
    key_type=ed25519 > "$TMP_CSR"
  [[ -s "$TMP_CSR" ]] || die "Empty CSR from pki_int/intermediate/generate/internal"

  # Step 2: copy CSR into the container and ask the root CA to sign it.
  docker cp "$TMP_CSR" oc-vault:/tmp/int.csr >/dev/null
  vault_exec write -field=certificate pki_root/root/sign-intermediate \
    csr=@/tmp/int.csr \
    format=pem_bundle \
    ttl=8760h > "$TMP_CRT"
  [[ -s "$TMP_CRT" ]] || die "Empty signed cert from pki_root/root/sign-intermediate"

  # Step 3: copy the signed cert back into the container and set it on
  # the intermediate so pki_int now has a chain rooted at pki_root.
  docker cp "$TMP_CRT" oc-vault:/tmp/int.crt >/dev/null
  vault_exec write pki_int/intermediate/set-signed certificate=@/tmp/int.crt >/dev/null

  # Cleanup (container-side; host-side handled by EXIT trap)
  docker exec oc-vault rm -f /tmp/int.csr /tmp/int.crt
else
  log "  Intermediate CA already exists, skipping"
fi

# Configure intermediate URLs
vault_exec write pki_int/config/urls \
  issuing_certificates="http://127.0.0.1:8200/v1/pki_int/ca" \
  crl_distribution_points="http://127.0.0.1:8200/v1/pki_int/crl" >/dev/null

# Roles for issuing server + client certs (24h TTL per ADR-002 D14)
vault_exec write pki_int/roles/server \
  allowed_domains="openclaw.local" \
  allow_subdomains=true \
  allow_bare_domains=true \
  max_ttl=24h \
  ttl=24h \
  key_type=ed25519 \
  allowed_uri_sans="spiffe://openclaw.local/*" \
  server_flag=true client_flag=false >/dev/null

vault_exec write pki_int/roles/client \
  allowed_domains="openclaw.local" \
  allow_subdomains=true \
  allow_bare_domains=true \
  max_ttl=24h \
  ttl=24h \
  key_type=ed25519 \
  allowed_uri_sans="spiffe://openclaw.local/*" \
  server_flag=false client_flag=true >/dev/null

log "  ✓ PKI (root + intermediate + server/client roles) ready"

# Transit signing keys (per ADR-002 D12)
enable_if_missing transit transit

create_transit_key() {
  local name="$1" ktype="$2" rotate="$3"
  if vault_exec read transit/keys/"$name" >/dev/null 2>&1; then
    log "  transit/keys/$name already exists, skipping"
    return
  fi
  log "  Creating transit/keys/$name (type=$ktype, auto_rotate=${rotate:-none})"
  if [[ -n "$rotate" && "$rotate" != "none" ]]; then
    vault_exec write -f transit/keys/"$name" type="$ktype" auto_rotate_period="$rotate" >/dev/null
  else
    vault_exec write -f transit/keys/"$name" type="$ktype" >/dev/null
  fi
}

# Signing keys (Ed25519). Monthly auto-rotation per ADR-002 D14.
create_transit_key core-jwt          ed25519     720h
create_transit_key audit-service     ed25519     720h
create_transit_key audit-appender    ed25519     720h
# Attestation key rotates quarterly via manual ceremony, not auto.
create_transit_key attestation-2026-q2 ed25519   none
# Admin key for WebAuthn credential management.
create_transit_key webauthn-admin    ed25519     none
# PII encryption key (AES-GCM, version-pinned per envelope per ADR-003 D6).
create_transit_key audit-pii-v3      aes256-gcm96 none

log "  ✓ Transit signing/encryption keys ready"

# AppRole auth method
if vault_exec auth list 2>/dev/null | awk '{print $1}' | grep -qx 'approle/'; then
  log "  AppRole auth already enabled, skipping"
else
  log "  Enabling AppRole auth method"
  vault_exec auth enable approle >/dev/null
fi

# ────────────────────────────────────────────────────────────────────
# 1.6 — Admin AppRole + destroy root token
# ────────────────────────────────────────────────────────────────────
log ""
log "==== TASK 1.6: Admin AppRole + destroy root token ===="

# Write the openclaw-admin policy (overwrite-safe)
log "  Writing 'openclaw-admin' policy"
docker exec -i oc-vault sh -c 'cat > /tmp/openclaw-admin.hcl' <<'POLICY'
# openclaw-admin — operator admin policy used via AppRole, never as a
# long-lived token. token_ttl on the role keeps blast radius to 15 min.

# Full path access (this is "near-root" but with TTL via AppRole).
path "*" {
  capabilities = ["create", "read", "update", "delete", "list", "sudo"]
}

# Block self-revocation footguns: don't allow ANYONE to delete the
# openclaw-admin role or its policy (would lock the operator out).
path "auth/approle/role/openclaw-admin" {
  capabilities = ["read"]
}
path "sys/policies/acl/openclaw-admin" {
  capabilities = ["read"]
}
POLICY
vault_exec policy write openclaw-admin /tmp/openclaw-admin.hcl >/dev/null
docker exec oc-vault rm -f /tmp/openclaw-admin.hcl

# Create the AppRole (overwrite-safe)
log "  Creating openclaw-admin AppRole (token_ttl=15m, token_max_ttl=1h)"
vault_exec write auth/approle/role/openclaw-admin \
  token_policies="openclaw-admin" \
  token_ttl=15m \
  token_max_ttl=1h \
  bind_secret_id=true \
  secret_id_num_uses=0 \
  secret_id_ttl=0 >/dev/null

# Read the role_id (non-secret, identifies the role)
ROLE_ID=$(vault_exec read -field=role_id auth/approle/role/openclaw-admin/role-id)
# Generate a fresh secret_id (one-time printable; treat as sensitive)
SECRET_ID=$(vault_exec write -force -field=secret_id auth/approle/role/openclaw-admin/secret-id)

echo
echo "════════════════════════════════════════════════════════════════"
echo "   openclaw-admin AppRole credentials — CAPTURE NOW"
echo "════════════════════════════════════════════════════════════════"
echo "   Role ID:    $ROLE_ID"
echo "   Secret ID:  $SECRET_ID"
echo "════════════════════════════════════════════════════════════════"
echo
cat <<EOF
Store in your password manager:
  - Role ID (non-secret per Vault docs; identifies the role)
  - Secret ID (TREAT AS SECRET — combined with Role ID it grants
    near-root for a 15-minute token TTL)

DO NOT store these alongside your Shamir shares or LUKS passphrase.
A single physical breach should never yield the keys to all three.

To log in later as the openclaw admin:
  docker exec -e VAULT_ADDR=http://127.0.0.1:8200 oc-vault \\
    vault write auth/approle/login \\
      role_id=<role-id> secret_id=<secret-id>
  # Returns a 15-minute admin token.

EOF
read -rp "Have you captured both values? Type CAPTURED to continue: " confirm
[[ "$confirm" == "CAPTURED" ]] || die "Aborted by operator. Re-run when ready."

# Forget the AppRole creds in our shell now that the operator has them.
unset ROLE_ID SECRET_ID

# ────────────────────────────────────────────────────────────────────
# Destroy root token (irreversible)
# ────────────────────────────────────────────────────────────────────
echo
cat <<'EOF'

  =========================================================
  ABOUT TO REVOKE THE ROOT TOKEN
  =========================================================

  After this, the only way to administer Vault is via the
  openclaw-admin AppRole (above) OR a generate-root ceremony
  using 3 of your 5 Shamir shares.

  If you have anything else in flight that needs the root token,
  abort now (Ctrl+C). You can re-run this script later; the
  earlier steps were idempotent.

EOF
read -rp "Type DESTROY to revoke the root token: " confirm
[[ "$confirm" == "DESTROY" ]] || {
  warn "Did not revoke root token. Re-run when ready."
  exit 0
}

log "Revoking root token..."
vault_exec token revoke -self >/dev/null

# Confirm
if vault_exec token lookup >/dev/null 2>&1; then
  die "Root token still works after revoke. Something went wrong. Manually revoke before walking away."
fi
log "✓ Root token revoked."

# Clear it from our shell.
unset ROOT_TOKEN

echo
cat <<'EOF'
═════════════════════════════════════════════════════════════════
  PHASE 1 TASKS 1.4 / 1.5 / 1.6 COMPLETE
═════════════════════════════════════════════════════════════════

  Audit log:         enabled → /vault/data/audit/file-audit.log
  Secret engines:    kv-v2, pki_root, pki_int, transit, approle
  CA hierarchy:      pki_root (10y) → pki_int (1y) → server/client roles (24h)
  Transit keys:      core-jwt, audit-service, audit-appender,
                     attestation-2026-q2, webauthn-admin, audit-pii-v3
  Operator admin:    openclaw-admin AppRole (capture stored in PWM)
  Root token:        REVOKED — no long-lived all-powerful token exists

  Next admin operations: use the AppRole login pattern shown above.

  Phase 1 remaining:
    1.7  Bootstrap internal CA — DONE inline here (pki_int + roles)
    1.8  WebAuthn relying-party endpoints (core scaffold)
    1.9  Enroll primary authenticator (platform per ADR-002 D13 or YubiKey)
    1.10 Enroll spare authenticator
    1.11 Test login + refresh + revoke loop
    1.12 vault-agent sidecar template
    1.13 (Done — transit keys are live)
    1.14 5-test mTLS smoke
    1.15 Document in RUNBOOK
EOF
