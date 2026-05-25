#!/usr/bin/env bash
# Wrapper: fetch the immudb projector password from Vault using the
# openclaw-admin AppRole, then exec `oc` with the right env.
#
# Pattern mirrors run-with-vault-creds.sh but uses the read-only
# `projector` immudb credentials, suitable for CLI inspection
# commands (audit tail / replay / verify).
#
# Usage:
#   ./scripts/oc-with-vault-creds.sh audit tail
#   ./scripts/oc-with-vault-creds.sh audit replay <conv_id>
#   ./scripts/oc-with-vault-creds.sh audit verify --date 2026-05-23

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

log()  { printf '\033[1;36m[oc-cli]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[oc-cli ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# ── Activate venv ───────────────────────────────────────────────────
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  if [[ -f "$CORE_DIR/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$CORE_DIR/.venv/bin/activate"
  else
    die ".venv not found at $CORE_DIR/.venv. Create with: python3.14 -m venv .venv && pip install -e .[dev]"
  fi
fi

command -v oc >/dev/null \
  || die "'oc' not on PATH. Did you run 'pip install -e .' inside the venv?"

# ── Vault AppRole login → fetch immudb projector password ───────────
if [[ -z "${OC_IMMUDB_PASSWORD:-}" ]]; then
  echo >&2
  read -rp  "openclaw-admin Role ID: "                  ROLE_ID
  read -srp "openclaw-admin Secret ID (will be hidden): " SECRET_ID; echo >&2
  [[ -n "$ROLE_ID" && -n "$SECRET_ID" ]] || die "Empty role_id / secret_id."

  TOK=$(printf '%s' "$SECRET_ID" | docker exec -i oc-vault \
    vault write -field=token auth/approle/login \
    role_id="$ROLE_ID" secret_id=-)
  unset SECRET_ID ROLE_ID
  [[ -n "$TOK" ]] || die "Vault AppRole login failed."

  OC_IMMUDB_PASSWORD=$(docker exec -e VAULT_TOKEN="$TOK" oc-vault \
    vault kv get -field=password kv/openclaw/immudb/projector)
  [[ -n "$OC_IMMUDB_PASSWORD" ]] || die "Could not fetch kv/openclaw/immudb/projector"
  log "Fetched immudb projector password from Vault."

  # Export token for transit/verify calls in `oc audit verify`.
  export OC_VAULT_TOKEN="$TOK"
  unset TOK
  log "Vault token exported for transit verify (TTL 15 min)."
fi

export OC_IMMUDB_HOST="${OC_IMMUDB_HOST:-127.0.0.1}"
export OC_IMMUDB_PORT="${OC_IMMUDB_PORT:-3322}"
export OC_IMMUDB_USER="projector"
export OC_IMMUDB_PASSWORD
export OC_IMMUDB_DATABASE="${OC_IMMUDB_DATABASE:-openclaw_audit}"
export OC_NATS_URL="${OC_NATS_URL:-nats://127.0.0.1:4222}"

exec oc "$@"
