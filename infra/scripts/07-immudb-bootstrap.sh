#!/usr/bin/env bash
# Phase 2 task 2.3 — bring up immudb + create audit database + users
# ----------------------------------------------------------------------
# The codenotary/immudb image is distroless (no /bin/sh) AND immuadmin's
# `login` / `user create` / `user changepassword` commands accept passwords
# ONLY via interactive prompt (no --password flag, no env var). We
# therefore drive every immuadmin invocation through `expect`, which
# allocates a pty and sends the prompt responses.
#
# Operator prerequisites:
#   - Install expect once:  sudo apt install -y expect
#   - Vault unsealed; openclaw-admin AppRole credentials ready
#   - dm-crypt /mnt/openclaw/immudb mounted (from 00)
#
# Idempotent: re-running rotates the admin password (using the value
# already in Vault), re-applies the database / user state. First run
# (Vault empty for kv/openclaw/immudb/admin) uses the default
# `immudb`/`immudb` credentials; immudb 1.9+ forces a change on first
# login, which the expect script handles inline.

set -euo pipefail

log()  { printf '\033[1;36m[oc-immudb]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[oc-immudb WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[oc-immudb ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (sudo)."
command -v expect >/dev/null || die "expect not installed. sudo apt install -y expect"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

mountpoint -q /mnt/openclaw/immudb || die "/mnt/openclaw/immudb not mounted. Run 00 first."

# ── Chown bind target BEFORE container start ───────────────────────
log "Setting /mnt/openclaw/immudb ownership to 3322:3322"
chown -R 3322:3322 /mnt/openclaw/immudb
chmod 700 /mnt/openclaw/immudb

# ── Bring up immudb if not already running ──────────────────────────
if ! docker ps --format '{{.Names}}' | grep -qx oc-immudb; then
  log "Starting oc-immudb..."
  docker compose --env-file "$INFRA_DIR/.env.openclaw" \
    -f "$INFRA_DIR/docker-compose.openclaw.yml" up -d immudb
fi

log "Waiting for oc-immudb to be running + healthy..."
ready=false
for i in $(seq 1 60); do
  state=$(docker inspect oc-immudb --format '{{.State.Status}}' 2>/dev/null || echo missing)
  [[ "$state" == "running" ]] || { sleep 1; continue; }
  health=$(docker inspect oc-immudb --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')
  if [[ "$health" == "healthy" || "$health" == "none" ]]; then
    ready=true; break
  fi
  sleep 1
done
$ready || die "immudb didn't reach healthy state in 60s. Check 'docker logs oc-immudb'."
log "immudb is ready."

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
vault_kv_put() { vault_exec kv put "$1" "$2=$3" >/dev/null; }
vault_kv_get() { vault_exec kv get -field="$2" "$1" 2>/dev/null || true; }

CUR_ADMIN_PW=$(vault_kv_get kv/openclaw/immudb/admin password)
FIRST_RUN=false
if [[ -z "$CUR_ADMIN_PW" ]]; then
  CUR_ADMIN_PW="immudb"
  FIRST_RUN=true
  log "No admin password in Vault — assuming default ('immudb', first run)"
else
  log "Loaded current admin password from Vault."
fi

# ── expect-driven helpers ───────────────────────────────────────────
# Each helper returns 0 on success, non-zero on any prompt mismatch.

# Login. On first run with default creds, immudb 1.9 forces a password
# change. We provide CUR_PW twice (current + new) and reuse CUR_PW so
# the password isn't actually rotated by the forced flow — we control
# the rotation later in a deliberate step.
immu_login() {
  local pw="$1" force_change="$2"  # force_change="yes" or "no"
  if [[ "$force_change" == "yes" ]]; then
    expect <<EXPECT
log_user 0
set timeout 15
spawn docker exec -it oc-immudb immuadmin login immudb
expect "Password:" { send -- "$pw\r" }
expect {
  "Choose a password for immudb:" {
    send -- "$pw\r"
    expect "continue with your password instead"
    send -- "Y\r"
    expect "Confirm password:"
    send -- "$pw\r"
    expect eof
  }
  "logged in" { expect eof }
  timeout    { exit 1 }
  "invalid"  { exit 2 }
}
EXPECT
  else
    expect <<EXPECT
log_user 0
set timeout 15
spawn docker exec -it oc-immudb immuadmin login immudb
expect "Password:" { send -- "$pw\r" }
expect {
  "logged in" { expect eof }
  timeout    { exit 1 }
  "invalid"  { exit 2 }
}
EXPECT
  fi
}

# Lenient driver: matches any plausible prompt wording immuadmin might
# emit across versions and code-paths. exp_continue cycles back to the
# same alternatives until the success marker or eof is seen.
#
# Inputs via Tcl-set variables: $old (may be empty), $new
immu_change_pw_lenient() {
  local cmd="$1" old="$2" new="$3"
  expect <<EXPECT
log_user 0
set timeout 20
spawn $cmd
expect {
  -re "(Old|Current) password:" {
    send -- "$old\r"
    exp_continue
  }
  -re "(New password:|Choose a password for)" {
    send -- "$new\r"
    exp_continue
  }
  -re "(Confirm (new )?password:)" {
    send -- "$new\r"
    exp_continue
  }
  "continue with your password instead" {
    send -- "Y\r"
    exp_continue
  }
  -re "(has been changed|successfully|created)" {
    expect eof
  }
  eof    { }
  timeout { exit 1 }
}
EXPECT
}

immu_change_admin_pw() {
  immu_change_pw_lenient \
    "docker exec -it oc-immudb immuadmin user changepassword immudb" \
    "$1" "$2"
}

immu_create_user() {
  local user="$1" perm="$2" pw="$3" db="$4"
  immu_change_pw_lenient \
    "docker exec -it oc-immudb immuadmin user create $user $perm $db" \
    "" "$pw"
}

immu_change_user_pw() {
  local user="$1" new="$2"
  immu_change_pw_lenient \
    "docker exec -it oc-immudb immuadmin user changepassword $user" \
    "" "$new"
}

immu_db_list() { docker exec oc-immudb immuadmin database list 2>/dev/null || true; }
immu_db_create() { docker exec oc-immudb immuadmin database create "$1" >/dev/null; }
immu_user_list() { docker exec oc-immudb immuadmin user list 2>/dev/null || true; }

# ── Login as admin ──────────────────────────────────────────────────
log "Logging in as admin (forced password change handled inline if first run)"
if [[ "$FIRST_RUN" == "true" ]]; then
  immu_login "$CUR_ADMIN_PW" yes \
    || die "Admin login failed. Default password may not match; immudb may have been initialised previously."
  # immudb's "forced change" left us at the same password (we sent it back).
else
  immu_login "$CUR_ADMIN_PW" no \
    || die "Admin login failed with Vault password. Vault may be out of sync with immudb."
fi
log "  ✓ admin login OK"

# ── Rotate admin password ───────────────────────────────────────────
gen_pw() { openssl rand -base64 32 | tr -d '\n' | tr '/+' '_-'; }
NEW_ADMIN_PW=$(gen_pw)
APPENDER_PW=$(gen_pw)
PROJECTOR_PW=$(gen_pw)

log "Rotating admin password"
immu_change_admin_pw "$CUR_ADMIN_PW" "$NEW_ADMIN_PW" \
  || die "Admin password rotation failed."
vault_kv_put kv/openclaw/immudb/admin password "$NEW_ADMIN_PW"
log "  ✓ admin password rotated + stored in Vault"

# Re-login with the new password so the token cached in the container is fresh
immu_login "$NEW_ADMIN_PW" no || die "Re-login with new admin password failed."

# ── Create openclaw_audit database ──────────────────────────────────
log "Ensuring database 'openclaw_audit' exists"
if immu_db_list | grep -qw openclaw_audit; then
  log "  database openclaw_audit already exists"
else
  immu_db_create openclaw_audit || die "Failed to create database openclaw_audit"
  log "  ✓ database openclaw_audit created"
fi

# ── Create-or-rotate users ──────────────────────────────────────────
ensure_user() {
  local user="$1" perm="$2" pw="$3"
  if immu_user_list | grep -qw "$user"; then
    log "  user '$user' exists — rotating password"
    immu_change_user_pw "$user" "$pw" || die "Could not rotate password for '$user'"
  else
    log "  creating user '$user' with $perm on openclaw_audit"
    immu_create_user "$user" "$perm" "$pw" openclaw_audit || die "Could not create user '$user'"
  fi
}

ensure_user appender  readwrite "$APPENDER_PW"
vault_kv_put kv/openclaw/immudb/appender password "$APPENDER_PW"
log "  ✓ appender password stored in Vault"

ensure_user projector read "$PROJECTOR_PW"
vault_kv_put kv/openclaw/immudb/projector password "$PROJECTOR_PW"
log "  ✓ projector password stored in Vault"

unset NEW_ADMIN_PW APPENDER_PW PROJECTOR_PW CUR_ADMIN_PW ADMIN_TOKEN

log ""
log "✓ Phase 2 task 2.3 complete."
log "  database:  openclaw_audit"
log "  users:     appender (RW), projector (R)"
log "  passwords: rotated, stored in kv/openclaw/immudb/{admin,appender,projector}"
