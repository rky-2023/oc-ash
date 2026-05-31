#!/usr/bin/env bash
# Wrapper: fetch immudb + Postgres + GitHub App credentials from Vault
# using the openclaw-admin AppRole, then exec `oc` with the right env.
#
# Pattern mirrors run-with-vault-creds.sh but uses the read-only
# `projector` immudb credentials and also fetches GitHub App creds for
# `oc attest publish`.
#
# Usage:
#   ./scripts/oc-with-vault-creds.sh audit tail
#   ./scripts/oc-with-vault-creds.sh audit replay <conv_id>
#   ./scripts/oc-with-vault-creds.sh audit verify --date 2026-05-23
#   ./scripts/oc-with-vault-creds.sh attest publish --date 2026-05-24

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
    vault kv get -field=password kv/openclaw/immudb/projector 2>/dev/null || true)
  if [[ -n "$OC_IMMUDB_PASSWORD" ]]; then
    log "Fetched immudb projector password from Vault."
  else
    log "kv/openclaw/immudb/projector not found — immudb commands (audit tail/replay/verify) will be unavailable."
  fi

  # Postgres DSN (for audit projection reads + oc attest publish)
  PG_PW=$(docker exec -e VAULT_TOKEN="$TOK" oc-vault \
    vault kv get -field=password kv/openclaw/postgres/app)
  if [[ -n "$PG_PW" ]]; then
    export OC_POSTGRES_DSN="postgresql://openclaw_app:${PG_PW}@${OC_PG_HOST:-127.0.0.1}:${OC_PG_PORT:-5432}/${OC_PG_DBNAME:-postgres}"
    log "Fetched Postgres DSN from Vault."
  fi
  PG_PW=""

  # GitHub App credentials (for oc attest publish)
  GH_APP_ID=$(docker exec -e VAULT_TOKEN="$TOK" oc-vault \
    vault kv get -field=value kv/openclaw/github-app/openclaw-bot/app-id 2>/dev/null || true)
  GH_INSTALL_ID=$(docker exec -e VAULT_TOKEN="$TOK" oc-vault \
    vault kv get -field=value kv/openclaw/github-app/openclaw-bot/installation-id 2>/dev/null || true)
  GH_PEM=$(docker exec -e VAULT_TOKEN="$TOK" oc-vault \
    vault kv get -field=value kv/openclaw/github-app/openclaw-bot/private-key-pem 2>/dev/null || true)
  if [[ -n "$GH_APP_ID" && -n "$GH_PEM" ]]; then
    export OC_GITHUB_APP_ID="$GH_APP_ID"
    export OC_GITHUB_INSTALLATION_ID="$GH_INSTALL_ID"
    export OC_GITHUB_APP_PRIVATE_KEY="$GH_PEM"
    log "Fetched GitHub App credentials from Vault."
  fi
  GH_APP_ID="" GH_INSTALL_ID="" GH_PEM=""

  # Export token for transit/verify calls in `oc audit verify`.
  export OC_VAULT_TOKEN="$TOK"
  unset TOK
  # The host-published Vault listener is TLS on 127.0.0.1:8200 (the default
  # http://… gets "Client sent an HTTP request to an HTTPS server"). Its cert
  # is signed by the internal CA, so skip verification for this loopback
  # connection unless OC_VAULT_CACERT is provided. Mirrors run-with-vault-creds.sh
  # so standalone tooling (oc audit verify, redaction-live-check.py) can sign/verify.
  export OC_VAULT_ADDR="${OC_VAULT_ADDR:-https://127.0.0.1:8200}"
  export OC_VAULT_VERIFY="${OC_VAULT_VERIFY:-false}"
  log "Vault token + addr exported for transit verify (TTL 15 min, addr=$OC_VAULT_ADDR)."
fi

export OC_IMMUDB_HOST="${OC_IMMUDB_HOST:-127.0.0.1}"
export OC_IMMUDB_PORT="${OC_IMMUDB_PORT:-3322}"
export OC_IMMUDB_USER="projector"
export OC_IMMUDB_PASSWORD
export OC_IMMUDB_DATABASE="${OC_IMMUDB_DATABASE:-openclaw_audit}"
export OC_NATS_URL="${OC_NATS_URL:-nats://127.0.0.1:4222}"

# If sourced (to export the creds above into the caller's shell, e.g. before
# running tests/phase-2-smoke.sh or redaction-live-check.py), return here
# instead of exec'ing — exec would otherwise replace the interactive shell.
(return 0 2>/dev/null) && return 0
exec oc "$@"
