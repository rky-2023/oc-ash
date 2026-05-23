#!/usr/bin/env bash
# Phase 2 task 2.3 — bring up immudb + create audit database + users
# ----------------------------------------------------------------------
# Brings up the immudb container (declared in
# infra/docker-compose.openclaw.yml), rotates the default admin password,
# creates the openclaw_audit database, and creates two users:
#   - appender  (R/W on openclaw_audit)  — used by audit-appender
#   - projector (R-only on openclaw_audit) — used by audit-projector
# Passwords are random + stored in Vault under
# kv/openclaw/immudb/{admin,appender,projector}.
#
# Important: the codenotary/immudb image is DISTROLESS — no shell, no
# chown, no coreutils. All immuadmin invocations therefore use direct
# `docker exec` calls, not `sh -c '...'` wrappers. Readiness is checked
# via `docker inspect` (container State.Status + Health.Status).
#
# Bind-target ownership: we chown /mnt/openclaw/immudb to uid 3322
# (the immudb user inside the container) BEFORE compose-up. This must
# happen on the host as root because the container can't fix its own
# filesystem ownership on a distroless image (no chown binary).
#
# Idempotent: re-running rotates passwords and re-applies the schema.
# Failures terminate via `die` — no silent advance through broken state.

set -euo pipefail

log()  { printf '\033[1;36m[oc-immudb]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[oc-immudb WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[oc-immudb ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root (sudo)."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

mountpoint -q /mnt/openclaw/immudb || die "/mnt/openclaw/immudb not mounted. Run 00 first."

# ── Chown the bind target BEFORE container start ────────────────────
log "Setting /mnt/openclaw/immudb ownership to immudb uid (3322:3322, mode 700)"
chown -R 3322:3322 /mnt/openclaw/immudb
chmod 700 /mnt/openclaw/immudb

# ── Bring up immudb if not already running ──────────────────────────
if ! docker ps --format '{{.Names}}' | grep -qx oc-immudb; then
  log "Starting oc-immudb..."
  docker compose --env-file "$INFRA_DIR/.env.openclaw" \
    -f "$INFRA_DIR/docker-compose.openclaw.yml" up -d immudb
fi

# ── Wait for the container to be healthy ────────────────────────────
# Distroless image — can't shell-out a TCP probe. We use docker inspect:
# Status=="running" AND (Health.Status=="healthy" OR no healthcheck).
log "Waiting for oc-immudb to be running + healthy..."
ready=false
for i in $(seq 1 60); do
  state=$(docker inspect oc-immudb --format '{{.State.Status}}' 2>/dev/null || echo missing)
  if [[ "$state" != "running" ]]; then
    sleep 1; continue
  fi
  # Healthcheck may not have run yet; tolerate missing field
  health=$(docker inspect oc-immudb --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || echo none)
  if [[ "$health" == "healthy" || "$health" == "none" ]]; then
    ready=true
    break
  fi
  sleep 1
done
$ready || die "immudb didn't reach a healthy state in 60s — check 'docker logs oc-immudb'."
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

# ── Determine current admin password ────────────────────────────────
CUR_ADMIN_PW=$(vault_kv_get kv/openclaw/immudb/admin password)
if [[ -z "$CUR_ADMIN_PW" ]]; then
  CUR_ADMIN_PW="immudb"
  log "No admin password in Vault — assuming default ('immudb', first run)"
else
  log "Loaded current admin password from Vault."
fi

# ── Helpers that talk directly to immuadmin (no sh in image) ────────
# Pipe stdin via `docker exec -i`. immuadmin's login persists a token
# in /home/immudb/.immudb/token (inside the container) so subsequent
# exec calls reuse the session.
immuadmin() {
  docker exec -i oc-immudb immuadmin "$@"
}

immuadmin_login() {
  local pw="$1"
  printf '%s\n' "$pw" | immuadmin login immudb >/dev/null
}

# ── Login as admin (verify CUR_ADMIN_PW is correct) ─────────────────
log "Logging in as admin (verifying current password)"
immuadmin_login "$CUR_ADMIN_PW" \
  || die "Admin login failed with password from Vault. If you're certain you have the right one in Vault, the actual immudb password may have drifted (e.g., from a partial earlier run). Wipe /mnt/openclaw/immudb and the kv/openclaw/immudb/* Vault entries, then re-run."

# ── Generate new passwords ──────────────────────────────────────────
gen_pw() { openssl rand -base64 32 | tr -d '\n' | tr '/+' '_-'; }
NEW_ADMIN_PW=$(gen_pw)
APPENDER_PW=$(gen_pw)
PROJECTOR_PW=$(gen_pw)

# ── Rotate admin password ───────────────────────────────────────────
log "Rotating admin password"
# `immuadmin user changepassword <user>` prompts for: old, new, new-confirm
printf '%s\n%s\n%s\n' "$CUR_ADMIN_PW" "$NEW_ADMIN_PW" "$NEW_ADMIN_PW" \
  | immuadmin user changepassword immudb >/dev/null \
  || die "Admin password change failed. Old password possibly wrong; check Vault state vs. immudb state."
vault_kv_put kv/openclaw/immudb/admin password "$NEW_ADMIN_PW"
log "  ✓ admin password rotated + stored in Vault"

# Re-login with new password so subsequent commands use a fresh session.
immuadmin_login "$NEW_ADMIN_PW"

# ── Create openclaw_audit database (idempotent) ─────────────────────
log "Ensuring database 'openclaw_audit' exists"
if immuadmin database list 2>/dev/null | grep -q '\bopenclaw_audit\b'; then
  log "  database openclaw_audit already exists"
else
  immuadmin database create openclaw_audit >/dev/null \
    || die "Failed to create database openclaw_audit"
  log "  ✓ database openclaw_audit created"
fi

# ── Helper: create-or-rotate user ───────────────────────────────────
# `immuadmin user list` lists users including 'immudb', 'appender', 'projector'.
ensure_user() {
  local user="$1" perm="$2" pw="$3"
  if immuadmin user list 2>/dev/null | grep -q "\b$user\b"; then
    log "  user '$user' exists — rotating password"
    # `immuadmin user changepassword <user>` as admin: prompts for new + confirm.
    # (Admin doesn't need the user's current password.)
    printf '%s\n%s\n' "$pw" "$pw" \
      | immuadmin user changepassword "$user" >/dev/null \
      || die "Could not rotate password for user '$user'"
  else
    log "  creating user '$user' with $perm on openclaw_audit"
    printf '%s\n%s\n' "$pw" "$pw" \
      | immuadmin user create "$user" "$perm" openclaw_audit >/dev/null \
      || die "Could not create user '$user'"
  fi
}

ensure_user appender  readwrite "$APPENDER_PW"
vault_kv_put kv/openclaw/immudb/appender password "$APPENDER_PW"
log "  ✓ appender password stored in Vault"

ensure_user projector readonly "$PROJECTOR_PW"
vault_kv_put kv/openclaw/immudb/projector password "$PROJECTOR_PW"
log "  ✓ projector password stored in Vault"

unset NEW_ADMIN_PW APPENDER_PW PROJECTOR_PW CUR_ADMIN_PW ADMIN_TOKEN

log ""
log "✓ Phase 2 task 2.3 complete."
log "  database:  openclaw_audit"
log "  users:     appender (RW), projector (R)"
log "  passwords: rotated, stored in kv/openclaw/immudb/{admin,appender,projector}"
